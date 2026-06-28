# 🌉 mac-bridge-mcp

**Let an AI securely drive a macOS machine, from anywhere.**

An [MCP](https://modelcontextprotocol.io) server that runs on a Mac and exposes a
small, sharp set of tools (shell, file transfer, binary execution, screenshots)
over a token-protected network port. Point your AI client at it and it can build,
test, and automate on macOS, even when your AI is running on Windows or Linux.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![MCP](https://img.shields.io/badge/protocol-MCP-6E56CF.svg)

```
   your machine (Windows / Linux / Mac)            the Mac you want to control
 ┌────────────────────────────────────┐           ┌────────────────────────────┐
 │  AI client (MCP host)              │  HTTP +   │      mac-bridge-mcp        │
 │  e.g. Claude Desktop, an agent     │ ───────►  │  ┌──────────────────────┐  │
 │                                    │  bearer   │  │ IP allowlist → token │  │
 │  "build & test my binary on macOS" │  token    │  └──────────┬───────────┘  │
 │                                    │ ◄───────  │     shell · files · exec   │
 └────────────────────────────────────┘  results  └────────────────────────────┘
```

> **⚠️ Warning - this is remote code execution on the host Mac, by design.**
> Anyone who can reach the port *and* has the token gets the equivalent of a
> shell. Run it only on machines you own, keep the port off the public internet,
> and read the [Security model](#-security-model) before exposing it anywhere.
> The token is as sensitive as an SSH key.

## Table of contents

- [Why](#why)
- [Features](#features)
- [Quickstart](#quickstart)
- [Installation](#installation)
- [Configuration](#configuration)
- [Connecting your AI client](#connecting-your-ai-client)
- [Tools](#tools)
- [Example: cross-platform binary test loop](#example-cross-platform-binary-test-loop)
- [Security model](#-security-model)
- [macOS permissions](#macos-permissions)
- [License](#license)

## Why

If you ship software that has to run on both Windows and macOS, you constantly
need "the other OS" in the loop, to compile a native binary, run the test
suite, reproduce a platform-specific bug, or grab a screenshot of how something
renders. `mac-bridge-mcp` puts a Mac one tool-call away from whatever AI you're
already working with, so a single conversation can drive both platforms.

The original motivation was **cross-platform security research.** When an AI
reviews source code that compiles on Windows, Linux, and macOS, it will sometimes
surface a vulnerability that only manifests on macOS, and then have no way to
confirm it. Lacking access to a macOS system, it can't compile the affected code,
run a proof-of-concept, and check whether the finding is genuine or a false
positive. `mac-bridge-mcp` closes that gap: it gives the AI a real Mac to
**compile, test, and verify** on, so macOS-specific findings can be triaged on
the actual operating system instead of guessed at.

It speaks MCP's **Streamable HTTP** transport, so any MCP-capable client can use
it, locally or across the network.

## Features

- **Real macOS control** - shell commands, direct binary execution, file
  read/write, directory listing, and screen capture.
- **Two security gates** - an optional source-IP allowlist (with CIDR support)
  in front of a constant-time bearer-token check.
- **Built-in TLS** - serve HTTPS directly from a cert + key, or rely on an SSH
  tunnel / VPN. No reverse proxy required.
- **Optional filesystem jail** - confine every operation under one directory.
- **Works across machines** - drive a Mac from Windows or Linux over an SSH
  tunnel or a private VPN.
- **Tiny and readable** - one dependency-light Python module, easy to audit and
  extend.
- **Client-agnostic** - works with any MCP host, with a drop-in `mcp-remote`
  config for stdio-only clients.

## Quickstart

On the **Mac** (the machine you want to control):

```bash
# 1. Get the code and install the two dependencies
git clone https://github.com/YOUR_USERNAME/mac-bridge-mcp
cd mac-bridge-mcp
pip install fastmcp uvicorn        # or: pip install -r requirements.txt

# 2. Generate a token and run the server
export MCP_BRIDGE_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
echo "Token: $MCP_BRIDGE_TOKEN"    # copy this
python3 src/mac_bridge_mcp/server.py
```

With the default `MCP_BRIDGE_HOST=127.0.0.1`, the server is reachable **only from
the Mac itself**. To connect from another machine, open an SSH tunnel and point
your client at the local end of it:

```bash
ssh -L 8765:127.0.0.1:8765 you@your-mac     # then connect to http://127.0.0.1:8765/mcp
```

**If you bind to an address your other machine can already reach**, for example
the Mac's Tailscale or LAN IP via `MCP_BRIDGE_HOST`, **you don't need the
tunnel.** Just connect straight to `http://<that-ip>:8765/mcp`. In that case
encrypt the connection by enabling [TLS / HTTPS](#tls--https) (or keep it on a
trusted private network/VPN), and never bind to a public address (see the
[Security model](#-security-model)).

## Installation

From source:

```bash
git clone https://github.com/YOUR_USERNAME/mac-bridge-mcp
cd mac-bridge-mcp
pip install -e .
```

Requires **Python 3.10+** on **macOS**.

## Configuration

Everything is configured through environment variables (or a local `.env`, see
[`.env.example`](.env.example)).

| Variable | Default | Purpose |
|---|---|---|
| `MCP_BRIDGE_TOKEN` | random | Shared secret ("password"). Set it to keep it stable across restarts. |
| `MCP_BRIDGE_HOST` | `127.0.0.1` | Bind address. **Do not use `0.0.0.0` on an untrusted network.** |
| `MCP_BRIDGE_PORT` | `8765` | Listen port. |
| `MCP_BRIDGE_PATH` | `/mcp` | URL path for the MCP endpoint. |
| `MCP_BRIDGE_SHELL` | `/bin/zsh` | Shell used by `run_command`. |
| `MCP_BRIDGE_ALLOW_IPS` | unset | Comma-separated IPs/CIDRs allowed to connect. Unset = any IP (token still required). |
| `MCP_BRIDGE_ROOT` | unset | Confine all file/binary operations under this directory. |
| `MCP_BRIDGE_TLS_CERT` | unset | Path to a PEM certificate. Set with `MCP_BRIDGE_TLS_KEY` to serve HTTPS. |
| `MCP_BRIDGE_TLS_KEY` | unset | Path to the certificate's private key (PEM). |
| `MCP_BRIDGE_TLS_KEY_PASSWORD` | unset | Password for the private key, if it is encrypted. |
| `MCP_BRIDGE_TRUST_FORWARDED` | unset | Read client IP from `X-Forwarded-For`. **ONLY** behind a proxy you control. |

## Connecting your AI client

The server and your AI usually run on **different machines**. Two cases:

**Your client supports remote / HTTP MCP servers directly** - point it at
`http://<host>:8765/mcp` and add the header `Authorization: Bearer <token>`.

**Your client only launches local stdio servers** (the classic Claude Desktop
pattern), use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote)
bridge, which runs locally and forwards to the remote server. See
[`examples/claude_desktop_config.json`](examples/claude_desktop_config.json):

```json
{
  "mcpServers": {
    "mac-bridge": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://MAC_IP_OR_HOST:8765/mcp",
        "--header", "Authorization: Bearer YOUR_TOKEN_HERE"
      ]
    }
  }
}
```

**Test before wiring up an AI** with the MCP Inspector (no model required):

```bash
npx @modelcontextprotocol/inspector
# URL: http://127.0.0.1:8765/mcp   (transport: Streamable HTTP)
# Header: Authorization: Bearer <token>
```

...or run the included smoke test:

```bash
TOKEN=your-token ./examples/smoke_test.sh
```

## Tools

| Tool | Description |
|---|---|
| `system_info` | macOS version, CPU architecture (arm64 / x86_64), hostname, user. **Call first.** |
| `run_command` | Run a shell command; returns stdout, stderr, exit code, timeout flag. |
| `run_binary` | Execute a binary directly with verbatim args (no shell parsing). |
| `list_dir` | List a directory with type, size, and mode for each entry. |
| `read_file` | Read a file as text, or base64 for binaries. |
| `write_file` | Write a file; `base64_encoded` + `make_executable` for pushing binaries. |
| `screenshot` | Capture the screen as a base64 PNG (needs Screen Recording permission). |

Every tool returns a structured result and handles its own errors, so the AI
gets a clean signal instead of a stack trace.

## Example: cross-platform binary test loop

A typical "I built it on Windows, does it work on macOS?" round-trip:

1. `system_info` - confirm `arch` is `arm64` vs `x86_64`.
2. `write_file` - push your built artifact (base64, `make_executable=true`).
3. `run_binary` - run it with test arguments; inspect `stdout` / `exit_code`.
4. `read_file` - pull back any output files it produced.

Because the AI sees the architecture first, it can pick the right build and even
recompile via `run_command` (`clang`, `cargo build`, `go build`, ...) before
testing.

## 🔐 Security model

Requests pass through two gates, in order, on **every** request:

1. **IP allowlist** (`MCP_BRIDGE_ALLOW_IPS`) - a peer outside the list is
   rejected with **403 before the token is even compared**. Supports single
   addresses and CIDR ranges, IPv4 and IPv6.
2. **Bearer token** - checked with a constant-time comparison; missing/wrong
   gives **401**.

An optional **filesystem jail** (`MCP_BRIDGE_ROOT`) then confines every path
argument to a chosen directory.

**What IP does the server actually see?**

- **SSH tunnel** - traffic arrives from `127.0.0.1` (the tunnel exit on the Mac),
  so include `127.0.0.1`/`::1` in the allowlist; your real gate there is the SSH
  login itself.
- **Tailscale / WireGuard / LAN** - the server sees the client's real address, so
  the allowlist is meaningful. Pin it to your client's VPN IP or LAN subnet:

  ```bash
  export MCP_BRIDGE_ALLOW_IPS="127.0.0.1,::1,100.64.0.0/10,192.168.1.0/24"
  ```

**Operating rules of thumb**

- Keep `MCP_BRIDGE_HOST=127.0.0.1`; reach the server via SSH tunnel or a private
  VPN. **Never put it on the public internet.**
- Use a long random token; rotate it if it leaks; never commit `.env`.
- Run as a normal user, not root.
- **Encrypt the connection.** The server can serve HTTPS itself (see
  [TLS / HTTPS](#tls--https)), or you can rely on an SSH tunnel / VPN for
  encryption. Don't send the token over plain HTTP across a network. Leave
  `MCP_BRIDGE_TRUST_FORWARDED` off unless a reverse proxy you control sets it.

### TLS / HTTPS

The server has built-in TLS, point it at a certificate and key and it serves
`https://` directly, no reverse proxy required:

```bash
export MCP_BRIDGE_TLS_CERT="/path/to/server.crt"
export MCP_BRIDGE_TLS_KEY="/path/to/server.key"
python3 src/mac_bridge_mcp/server.py        # now on https://...
```

Three easy ways to get a certificate, best first:

- **Tailscale (recommended):** if you reach the Mac over Tailscale, run
  `tailscale cert <machine>.<tailnet>.ts.net`. You get a **real, trusted**
  certificate with no client-side configuration, clients verify it normally.
- **mkcert (LAN):** [`mkcert`](https://github.com/FiloSottile/mkcert) installs a
  local CA and issues certs your machines trust. Good for a home/office LAN.
- **Self-signed:** run [`examples/gen_self_signed_cert.sh`](examples/gen_self_signed_cert.sh).
  The traffic is encrypted, but clients **don't trust a self-signed cert by
  default** and will refuse to connect until told to trust it. For the
  `mcp-remote` bridge that means pointing Node at your cert:

  ```bash
  NODE_EXTRA_CA_CERTS=/path/to/server.crt npx mcp-remote https://<host>:8765/mcp \
    --header "Authorization: Bearer <token>"
  ```

When TLS is on, use `https://` in every client URL. The `MCP_BRIDGE_ALLOW_IPS`
and token gates apply exactly the same over HTTPS.

See [`SECURITY.md`](SECURITY.md) for the full policy and how to report issues.

## macOS permissions

- Shell and file tools need **no special permission**.
- `screenshot` needs **Screen Recording** for whatever process runs the server
  (Terminal / iTerm / `python`): System Settings -> Privacy & Security -> Screen
  Recording, then restart that app.
- Future mouse/keyboard control will need **Accessibility** permission in the
  same place.

## License

[MIT](LICENSE) © IamLeandrooooo

---

*Built on the [Model Context Protocol](https://modelcontextprotocol.io) and [FastMCP](https://github.com/jlowin/fastmcp).*
