#!/bin/bash
# EUR-Lex MCP 本地服务（HTTP 127.0.0.1:3010，端点 /mcp）。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export MCP_TRANSPORT_TYPE=http MCP_HTTP_PORT=3010 MCP_HTTP_HOST=127.0.0.1
exec "$ROOT/runtime/node/bin/node" \
  "$ROOT/vendor/eurlex/node_modules/@cyanheads/eur-lex-mcp-server/dist/index.js"
