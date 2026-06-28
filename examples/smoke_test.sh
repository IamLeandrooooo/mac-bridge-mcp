#!/usr/bin/env bash
# Quick end-to-end check against a running mac-bridge-mcp server.
# Usage: TOKEN=your-token ./examples/smoke_test.sh [http://127.0.0.1:8765/mcp]
set -euo pipefail

URL="${1:-http://127.0.0.1:8765/mcp}"
TOKEN="${TOKEN:?Set TOKEN to your MCP_BRIDGE_TOKEN}"

hdr_accept="Accept: application/json, text/event-stream"
hdr_ct="Content-Type: application/json"

echo "1) Unauthenticated request (expect 401):"
curl -s -o /dev/null -w "   -> %{http_code}\n" -X POST "$URL" \
  -H "$hdr_ct" -H "$hdr_accept" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'

echo "2) Authenticated initialize (expect 200 + serverInfo):"
curl -s -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" -H "$hdr_ct" -H "$hdr_accept" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}' \
  | sed 's/^data: //' | head -c 500
echo

echo "Done. For full interactive testing, use: npx @modelcontextprotocol/inspector"
