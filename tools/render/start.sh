#!/bin/bash
# Render 单实例双进程启动:EUR-Lex MCP(127.0.0.1:3010)+ FastAPI($PORT)。
# 第二个进程不另收费;MCP 起不来只降级欧盟法核查,不阻断国内法与案例核查。
set -u

MCP_PORT="${MCP_HTTP_PORT:-3010}"
MCP_LOG="${TMPDIR:-/tmp}/eurlex-mcp.log"
MCP_PID=""

start_eurlex_mcp() {
  echo "[start] launching EUR-Lex MCP on 127.0.0.1:$MCP_PORT ..."
  MCP_TRANSPORT_TYPE=http MCP_HTTP_PORT="$MCP_PORT" \
    npx -y @cyanheads/eur-lex-mcp-server >"$MCP_LOG" 2>&1 &
  MCP_PID=$!
  for _ in $(seq 1 30); do
    # 只要端口能建立连接即视为就绪(HTTP 状态码不做要求)
    if curl -s -o /dev/null "http://127.0.0.1:$MCP_PORT/mcp" 2>/dev/null; then
      echo "[start] EUR-Lex MCP ready (pid $MCP_PID)"
      return 0
    fi
    if ! kill -0 "$MCP_PID" 2>/dev/null; then
      echo "[start] WARN: EUR-Lex MCP exited early; EU statute checks will degrade. Log: $MCP_LOG"
      MCP_PID=""
      return 0
    fi
    sleep 1
  done
  echo "[start] WARN: EUR-Lex MCP not ready in 30s; continuing anyway. Log: $MCP_LOG"
  return 0
}

# Ansvar token 移植(可选):服务器无浏览器,无法完成 OAuth 授权;
# 把本机授权缓存 base64 后经 ANSVAR_TOKENS_B64 注入,首次调用自动续期。
# 未配置或解码失败时 Ansvar 源降级为"待核实",不影响其他数据源。
restore_ansvar_tokens() {
  [ -n "${ANSVAR_TOKENS_B64:-}" ] || return 0
  local token_file="$HOME/.ccitecheck/ansvar_tokens.json"
  mkdir -p "$HOME/.ccitecheck"
  if printf '%s' "$ANSVAR_TOKENS_B64" | base64 -d >"$token_file" 2>/dev/null \
    && grep -q '"refresh_token"' "$token_file"; then
    chmod 600 "$token_file"
    echo "[start] ansvar tokens restored from ANSVAR_TOKENS_B64"
  else
    rm -f "$token_file"
    echo "[start] WARN: ANSVAR_TOKENS_B64 invalid; ansvar checks will degrade"
  fi
}

restore_ansvar_tokens
start_eurlex_mcp

# exec 让 uvicorn 直接接替本进程,Render 的停止信号可直达主进程;
# MCP 子进程随实例回收一并终止。
exec python3 -m apps.api.server --host 0.0.0.0 --port "${PORT:-3000}"
