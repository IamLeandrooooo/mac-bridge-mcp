#!/usr/bin/env bash
# Generate a self-signed TLS certificate for mac-bridge-mcp.
#
# Usage:
#   ./examples/gen_self_signed_cert.sh                 # CN/SAN = localhost
#   ./examples/gen_self_signed_cert.sh 100.x.y.z       # add an IP SAN
#   ./examples/gen_self_signed_cert.sh mymac.local     # add a DNS SAN
#
# Produces ./certs/server.crt and ./certs/server.key, then prints the env vars
# to point the server at them.
#
# NOTE: self-signed certs are NOT trusted by clients out of the box. The
# connection is still encrypted, but the client must be told to trust this cert
# (e.g. NODE_EXTRA_CA_CERTS for mcp-remote) or it will refuse to connect. For a
# trusted cert with zero fuss, prefer `tailscale cert`, see the README.
set -euo pipefail

HOST="${1:-localhost}"
OUT="${OUT:-certs}"
mkdir -p "$OUT"

# Build a SAN entry: IP if it looks like an address, otherwise DNS.
if [[ "$HOST" =~ ^[0-9.]+$ || "$HOST" == *:* ]]; then
  SAN="IP:${HOST}"
else
  SAN="DNS:${HOST}"
fi

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$OUT/server.key" \
  -out "$OUT/server.crt" \
  -days 825 \
  -subj "/CN=${HOST}" \
  -addext "subjectAltName=${SAN},DNS:localhost,IP:127.0.0.1"

chmod 600 "$OUT/server.key"

echo
echo "Created:"
echo "  $OUT/server.crt"
echo "  $OUT/server.key"
echo
echo "Run the server with TLS:"
echo "  export MCP_BRIDGE_TLS_CERT=\"\$PWD/$OUT/server.crt\""
echo "  export MCP_BRIDGE_TLS_KEY=\"\$PWD/$OUT/server.key\""
