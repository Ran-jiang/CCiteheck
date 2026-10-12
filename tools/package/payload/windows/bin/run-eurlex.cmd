@echo off
rem EUR-Lex MCP local service (HTTP 127.0.0.1:3010, endpoint /mcp)
set "ROOT=%~dp0.."
set "MCP_TRANSPORT_TYPE=http"
set "MCP_HTTP_PORT=3010"
set "MCP_HTTP_HOST=127.0.0.1"
"%ROOT%\runtime\node\node.exe" ^
  "%ROOT%\vendor\eurlex\node_modules\@cyanheads\eur-lex-mcp-server\dist\index.js" ^
  >> "%ROOT%\logs\eurlex.log" 2>&1
