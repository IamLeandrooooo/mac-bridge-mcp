#!/usr/bin/env python3
"""
macOS Bridge MCP
================
A Model Context Protocol server that runs on a macOS machine and exposes a small
set of tools (shell, file transfer, binary execution, screenshots) over a
network port, protected by a bearer token. An AI client running anywhere, e.g.
on your Windows box, connects to it and can then drive the Mac.

Primary use case: building/testing binaries that must work on both Windows and
macOS, driven from a single AI session.

Transport: Streamable HTTP (the network transport for MCP).
Auth:      Static bearer token checked on every HTTP request.

Run:
    export MCP_BRIDGE_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    export MCP_BRIDGE_HOST=127.0.0.1     # see SECURITY notes before changing
    export MCP_BRIDGE_PORT=8765
    python3 macos_bridge_mcp.py

The client then connects to:  http://<mac-ip>:8765/mcp
sending header:               Authorization: Bearer <token>
"""

from __future__ import annotations

import base64
import ipaddress
import os
import secrets
import shlex
import subprocess
import sys
import tempfile

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import uvicorn

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

HOST = os.environ.get("MCP_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_BRIDGE_PORT", "8765"))
PATH = os.environ.get("MCP_BRIDGE_PATH", "/mcp")

TOKEN = os.environ.get("MCP_BRIDGE_TOKEN")
if not TOKEN:
    # No token supplied -> generate an ephemeral one and print it. The server is
    # useless to anyone who doesn't have this string.
    TOKEN = secrets.token_urlsafe(32)
    print(f"[mac-bridge] No MCP_BRIDGE_TOKEN set. Generated one for this run:\n"
          f"               {TOKEN}\n"
          f"               Set MCP_BRIDGE_TOKEN to keep it stable across restarts.",
          file=sys.stderr)

# Default shell used for run_command. zsh is the macOS default since Catalina.
SHELL = os.environ.get("MCP_BRIDGE_SHELL", "/bin/zsh")

# Optional: restrict all file/command operations to live under this directory.
# Leave unset for full machine access (the point of the tool), set it to a
# sandbox path if you want a safety jail.
ROOT_JAIL = os.environ.get("MCP_BRIDGE_ROOT")  # e.g. "/Users/me/mcp-sandbox"

# Optional: only accept connections from these IPs / CIDR ranges. Comma
# separated. Supports single addresses and subnets, IPv4 and IPv6. If unset,
# any IP may connect (the token is still required).
#   MCP_BRIDGE_ALLOW_IPS="127.0.0.1,100.64.0.0/10,192.168.1.0/24,::1"
def _parse_allowlist(raw: str | None):
    networks = []
    if not raw:
        return networks
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        # ip_network with strict=False accepts both "1.2.3.4" and "1.2.3.0/24".
        networks.append(ipaddress.ip_network(item, strict=False))
    return networks

ALLOW_NETWORKS = _parse_allowlist(os.environ.get("MCP_BRIDGE_ALLOW_IPS"))

# Trust the X-Forwarded-For header to determine the client IP. ONLY enable this
# if the server sits behind a reverse proxy you control, since clients can forge
# this header otherwise. Default: off (use the real socket peer address).
TRUST_FORWARDED = os.environ.get("MCP_BRIDGE_TRUST_FORWARDED", "").lower() in (
    "1", "true", "yes")

# Optional TLS. Point these at a PEM certificate and its private key to serve
# HTTPS directly (no reverse proxy needed). If only one is set, TLS is skipped
# with a warning. See the README for easy ways to obtain a cert:
#   - Tailscale:  tailscale cert <machine>.<tailnet>.ts.net   (real, trusted)
#   - mkcert:     mkcert <host>                                (trusted on your LAN)
#   - self-signed: examples/gen_self_signed_cert.sh           (clients must trust it)
TLS_CERT = os.environ.get("MCP_BRIDGE_TLS_CERT")          # path to cert .pem
TLS_KEY = os.environ.get("MCP_BRIDGE_TLS_KEY")            # path to private key .pem
TLS_KEY_PASSWORD = os.environ.get("MCP_BRIDGE_TLS_KEY_PASSWORD")  # if key is encrypted


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _check_jail(path: str) -> str:
    """Resolve a path and, if a jail is configured, ensure it stays inside it."""
    real = os.path.realpath(os.path.expanduser(path))
    if ROOT_JAIL:
        jail = os.path.realpath(os.path.expanduser(ROOT_JAIL))
        if not (real == jail or real.startswith(jail + os.sep)):
            raise ValueError(f"Path {real!r} is outside the allowed root {jail!r}")
    return real


def _run(argv_or_cmd, *, shell: bool, cwd: str | None, timeout: int) -> dict:
    """Run a process and return a structured result."""
    if cwd:
        cwd = _check_jail(cwd)
    try:
        proc = subprocess.run(
            argv_or_cmd,
            shell=shell,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            executable=SHELL if shell else None,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "exit_code": None,
            "stdout": e.stdout or "",
            "stderr": (e.stderr or "") + f"\n[timed out after {timeout}s]",
            "timed_out": True,
        }


# --------------------------------------------------------------------------- #
# Auth middleware - gates EVERY request before it reaches the MCP layer
# --------------------------------------------------------------------------- #

class SecurityMiddleware(BaseHTTPMiddleware):
    """Two gates on every request, evaluated in order:

      1. IP allowlist (if MCP_BRIDGE_ALLOW_IPS is set) -> 403 if not allowed.
      2. Bearer token                                  -> 401 if missing/wrong.

    The IP check runs first so unrecognised peers are rejected before any token
    comparison happens.
    """

    def __init__(self, app, token: str, allow_networks):
        super().__init__(app)
        self._token = token
        self._allow_networks = allow_networks

    # --- IP allowlist -------------------------------------------------------- #

    def _client_ip(self, request: Request) -> str | None:
        if TRUST_FORWARDED:
            xff = request.headers.get("x-forwarded-for")
            if xff:
                # First entry is the original client when set by a trusted proxy.
                return xff.split(",")[0].strip()
        return request.client.host if request.client else None

    def _ip_allowed(self, ip_str: str | None) -> bool:
        if not self._allow_networks:
            return True  # no allowlist configured -> all IPs permitted
        if ip_str is None:
            return False
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return any(ip in net for net in self._allow_networks)

    # --- Bearer token -------------------------------------------------------- #

    def _extract_token(self, request: Request) -> str | None:
        # 1) Authorization: Bearer <token>
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        # 2) X-Bridge-Token header (for clients that can't set Authorization)
        if request.headers.get("x-bridge-token"):
            return request.headers["x-bridge-token"].strip()
        # 3) ?token= query param (last resort; avoid if you can, it can land in logs)
        if request.query_params.get("token"):
            return request.query_params["token"].strip()
        return None

    # --- Combined gate ------------------------------------------------------- #

    async def dispatch(self, request: Request, call_next):
        client_ip = self._client_ip(request)
        if not self._ip_allowed(client_ip):
            return JSONResponse(
                {"error": "forbidden", "reason": "ip not allowed"},
                status_code=403,
            )
        supplied = self._extract_token(request) or ""
        # Constant-time comparison to avoid timing attacks.
        if not secrets.compare_digest(supplied, self._token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


# --------------------------------------------------------------------------- #
# MCP server + tools
# --------------------------------------------------------------------------- #

mcp = FastMCP(
    name="mac-bridge",
    instructions=(
        "Tools to control a macOS machine: run shell commands, transfer files, "
        "execute binaries, inspect the system, and capture the screen. Use "
        "system_info first to confirm the OS and CPU architecture (arm64 vs "
        "x86_64) before testing native binaries."
    ),
)


@mcp.tool
def system_info() -> dict:
    """Return macOS version, CPU architecture, hostname and current user.

    Call this first to confirm you are on macOS and to learn the architecture
    (arm64 for Apple Silicon, x86_64 for Intel) before running native binaries.
    """
    def out(cmd):
        return _run(cmd, shell=False, cwd=None, timeout=10)["stdout"].strip()
    return {
        "os": out(["sw_vers", "-productName"]),
        "os_version": out(["sw_vers", "-productVersion"]),
        "build": out(["sw_vers", "-buildVersion"]),
        "arch": out(["uname", "-m"]),
        "hostname": out(["hostname"]),
        "user": out(["whoami"]),
        "cwd": os.getcwd(),
    }


@mcp.tool
def run_command(command: str, cwd: str | None = None, timeout: int = 60) -> dict:
    """Run a shell command on the Mac and return stdout, stderr and exit code.

    Args:
        command: The command line to execute (interpreted by the shell).
        cwd: Working directory. Defaults to the server's current directory.
        timeout: Max seconds before the command is killed.
    """
    return _run(command, shell=True, cwd=cwd, timeout=timeout)


@mcp.tool
def run_binary(path: str, args: list[str] | None = None,
               cwd: str | None = None, timeout: int = 60) -> dict:
    """Execute a binary directly (no shell interpretation of args).

    Safer than run_command for invoking a built artifact, since arguments are
    passed verbatim. Use this to test a compiled binary's behaviour.

    Args:
        path: Path to the executable.
        args: List of arguments to pass.
        cwd: Working directory.
        timeout: Max seconds before the process is killed.
    """
    real = _check_jail(path)
    argv = [real] + list(args or [])
    return _run(argv, shell=False, cwd=cwd, timeout=timeout)


@mcp.tool
def list_dir(path: str = ".") -> dict:
    """List a directory. Returns entries with type, size and mode."""
    real = _check_jail(path)
    entries = []
    for name in sorted(os.listdir(real)):
        full = os.path.join(real, name)
        try:
            st = os.lstat(full)
            entries.append({
                "name": name,
                "is_dir": os.path.isdir(full),
                "size": st.st_size,
                "mode": oct(st.st_mode & 0o777),
            })
        except OSError as e:
            entries.append({"name": name, "error": str(e)})
    return {"path": real, "entries": entries}


@mcp.tool
def read_file(path: str, binary: bool = False, max_bytes: int = 2_000_000) -> dict:
    """Read a file from the Mac.

    Args:
        path: File to read.
        binary: If True, return base64-encoded bytes (use for non-text files).
        max_bytes: Refuse to read files larger than this.
    """
    real = _check_jail(path)
    size = os.path.getsize(real)
    if size > max_bytes:
        return {"error": f"file is {size} bytes, exceeds max_bytes={max_bytes}"}
    with open(real, "rb") as f:
        data = f.read()
    if binary:
        return {"path": real, "size": size, "base64": base64.b64encode(data).decode()}
    try:
        return {"path": real, "size": size, "text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"path": real, "size": size,
                "base64": base64.b64encode(data).decode(),
                "note": "not valid UTF-8; returned as base64"}


@mcp.tool
def write_file(path: str, content: str, base64_encoded: bool = False,
               make_executable: bool = False) -> dict:
    """Write a file on the Mac. Use this to push a binary built on Windows.

    Args:
        path: Destination path (parent dirs are created).
        content: File contents. Plain text, or base64 if base64_encoded=True.
        base64_encoded: Treat `content` as base64 (required for binaries).
        make_executable: chmod +x the file after writing (for binaries/scripts).
    """
    real = _check_jail(path)
    os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
    data = base64.b64decode(content) if base64_encoded else content.encode("utf-8")
    with open(real, "wb") as f:
        f.write(data)
    if make_executable:
        os.chmod(real, 0o755)
    return {"path": real, "bytes_written": len(data), "executable": make_executable}


@mcp.tool
def screenshot() -> dict:
    """Capture the screen and return it as a base64 PNG.

    Requires Screen Recording permission for the process running this server
    (System Settings > Privacy & Security > Screen Recording).
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        r = _run(["screencapture", "-x", tmp_path], shell=False, cwd=None, timeout=15)
        if r["exit_code"] != 0:
            return {"error": "screencapture failed", **r}
        with open(tmp_path, "rb") as f:
            return {"format": "png", "base64": base64.b64encode(f.read()).decode()}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    if HOST == "0.0.0.0":
        print("[mac-bridge] WARNING: binding to 0.0.0.0 exposes this RCE-capable "
              "server on every network interface. Prefer 127.0.0.1 + an SSH tunnel "
              "or a private VPN (e.g. Tailscale). See the README.", file=sys.stderr)

    if ALLOW_NETWORKS:
        print("[mac-bridge] IP allowlist active: "
              + ", ".join(str(n) for n in ALLOW_NETWORKS), file=sys.stderr)
    else:
        print("[mac-bridge] IP allowlist OFF (any IP may connect; token still "
              "required). Set MCP_BRIDGE_ALLOW_IPS to restrict.", file=sys.stderr)

    # Build the Streamable HTTP ASGI app and wrap it with the security gate.
    app = mcp.http_app(path=PATH)
    app.add_middleware(SecurityMiddleware, token=TOKEN, allow_networks=ALLOW_NETWORKS)

    # Configure TLS if a cert + key were provided.
    ssl_kwargs = {}
    scheme = "http"
    if TLS_CERT and TLS_KEY:
        ssl_kwargs = {"ssl_certfile": TLS_CERT, "ssl_keyfile": TLS_KEY}
        if TLS_KEY_PASSWORD:
            ssl_kwargs["ssl_keyfile_password"] = TLS_KEY_PASSWORD
        scheme = "https"
        print(f"[mac-bridge] TLS enabled (cert: {TLS_CERT})", file=sys.stderr)
    elif TLS_CERT or TLS_KEY:
        print("[mac-bridge] WARNING: TLS needs BOTH MCP_BRIDGE_TLS_CERT and "
              "MCP_BRIDGE_TLS_KEY. Only one was set, serving plain HTTP.",
              file=sys.stderr)
    elif HOST not in ("127.0.0.1", "localhost", "::1"):
        print("[mac-bridge] NOTE: serving plain HTTP on a non-loopback address. "
              "The token would travel unencrypted. Enable TLS (MCP_BRIDGE_TLS_CERT/"
              "KEY) or use an SSH tunnel / VPN. See the README.", file=sys.stderr)

    print(f"[mac-bridge] listening on {scheme}://{HOST}:{PORT}{PATH}", file=sys.stderr)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", **ssl_kwargs)


if __name__ == "__main__":
    main()
