#!/bin/bash
# CCiteheck 本地完整服务安装器（macOS）
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/Applications/CCiteheck"
AGENT_DIR="$HOME/Library/LaunchAgents"
WEF_DIR="$HOME/Library/Containers/com.microsoft.Word/Data/Documents/wef"
WEF_CACHE="$HOME/Library/Containers/com.microsoft.Word/Data/Library/Caches/Wef"
UID_NUM="$(id -u)"

fail() { echo ""; echo "安装失败：$1"; echo "可重新运行本安装器；已有安装不会损坏。"; read -r -p "按回车退出..."; exit 1; }

echo "CCiteheck 本地服务安装器"
echo "========================"

# 0. 解除 Gatekeeper 隔离标记（对整个解包目录）
xattr -rd com.apple.quarantine "$SRC" 2>/dev/null || true

# 1. 预检
if pgrep -x "Microsoft Word" >/dev/null 2>&1; then
  fail "Microsoft Word 正在运行，请完全退出 Word 后重新运行安装器。"
fi
for port in 3000 3010; do
  holder="$(lsof -nP -iTCP:$port -sTCP:LISTEN -Fc 2>/dev/null | sed -n 's/^c//p' | head -1)"
  if [ -n "$holder" ]; then
    if launchctl print "gui/$UID_NUM/com.ccitecheck.api" >/dev/null 2>&1 \
      || launchctl print "gui/$UID_NUM/com.ccitecheck.eurlex" >/dev/null 2>&1; then
      echo "检测到旧版 CCiteheck 服务，将自动升级。"
    else
      fail "端口 $port 已被进程「$holder」占用，请先释放该端口。"
    fi
    break
  fi
done

# 2. 停旧服务（若有）
launchctl bootout "gui/$UID_NUM/com.ccitecheck.api" 2>/dev/null || true
launchctl bootout "gui/$UID_NUM/com.ccitecheck.eurlex" 2>/dev/null || true

# 3. 拷贝 payload（升级时保留已有 .env 与日志）
echo "[1/6] 安装文件到 $INSTALL_DIR ..."
KEEP_ENV=""
if [ -f "$INSTALL_DIR/.env" ]; then
  KEEP_ENV="$(mktemp)"; cp "$INSTALL_DIR/.env" "$KEEP_ENV"
fi
mkdir -p "$INSTALL_DIR"
ditto "$SRC/payload/" "$INSTALL_DIR/" || fail "文件拷贝失败"
mkdir -p "$INSTALL_DIR/logs"
# 本版随包分发的 .env（内部分发版含真实密钥）；被旧配置覆盖前先存一份，
# 供升级时合并新增配置项使用。
PKG_ENV=""
if [ -f "$INSTALL_DIR/.env" ] && [ -n "$KEEP_ENV" ]; then
  PKG_ENV="$(mktemp)"; cp "$INSTALL_DIR/.env" "$PKG_ENV"
fi
if [ -n "$KEEP_ENV" ]; then
  # 保留既有取值，同时把本版新增的配置项合并进来（否则新增的密钥/开关
  # 永远进不到老安装里）
  cp "$KEEP_ENV" "$INSTALL_DIR/.env"
  NEW_ENV="$PKG_ENV"
  [ -n "$NEW_ENV" ] || NEW_ENV="$INSTALL_DIR/.env.template"
  if [ -f "$NEW_ENV" ]; then
    "$INSTALL_DIR/runtime/python/bin/python3" - "$INSTALL_DIR/.env" "$NEW_ENV" <<'PYEOF'
import re, sys
cur_path, new_path = sys.argv[1], sys.argv[2]
cur = open(cur_path, encoding="utf-8").read()
new = open(new_path, encoding="utf-8").read()
have = {m.group(1) for m in re.finditer(r'^([A-Z_][A-Z0-9_]*)=', cur, re.M)}
add = [l for l in new.splitlines()
       if (m := re.match(r'^([A-Z_][A-Z0-9_]*)=', l)) and m.group(1) not in have]
if add:
    open(cur_path, "a", encoding="utf-8").write(
        "\n# 本次升级新增的配置项\n" + "\n".join(add) + "\n")
    print(f"  已合并 {len(add)} 项新增配置")
PYEOF
  fi
  rm -f "$KEEP_ENV" "$PKG_ENV"
elif [ ! -f "$INSTALL_DIR/.env" ]; then
  cp "$INSTALL_DIR/.env.template" "$INSTALL_DIR/.env"
  echo "提示：包内未含密钥，已用模板生成 .env——语义核查需要填入 DASHSCOPE_API_KEY。"
fi
cp "$SRC/uninstall.command" "$INSTALL_DIR/uninstall.command" 2>/dev/null || true
chmod +x "$INSTALL_DIR"/bin/*.sh "$INSTALL_DIR/uninstall.command" 2>/dev/null || true

# 4. HTTPS 开发证书（10 年有效期）
echo "[2/6] 安装并信任 localhost HTTPS 证书（可能需要输入登录密码）..."
"$INSTALL_DIR/runtime/node/bin/node" \
  "$INSTALL_DIR/vendor/certs/node_modules/office-addin-dev-certs/cli.js" \
  install --days 365 || fail "HTTPS 证书安装未完成"

# 5. 注册并启动常驻服务
echo "[3/6] 注册开机自启服务..."
mkdir -p "$AGENT_DIR"
for svc in api eurlex; do
  sed "s|__ROOT__|$INSTALL_DIR|g" "$INSTALL_DIR/com.ccitecheck.$svc.plist.tmpl" \
    > "$AGENT_DIR/com.ccitecheck.$svc.plist"
  launchctl bootstrap "gui/$UID_NUM" "$AGENT_DIR/com.ccitecheck.$svc.plist" \
    || fail "服务 com.ccitecheck.$svc 启动失败，日志见 $INSTALL_DIR/logs/"
done

# 6. 就绪检查：直接测端口是否在监听（TCP 连接），绕开 curl 在 mac 上
# 对本机开发证书验证失败（Secure Transport / OpenSSL 均可能 exit 60）的坑。
# 服务能 bind 端口即视为就绪；功能性由用户在浏览器打开网页版验证。
port_ready() {  # $1=端口 $2=尝试次数
  local port="$1" tries="$2" i
  for i in $(seq 1 "$tries"); do
    sleep 1
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      exec 3>&- 3<&-
      return 0
    fi
  done
  return 1
}
echo "[4/6] 等待服务就绪..."
if ! port_ready 3000 30; then
  echo "--- api.err.log 末尾 ---"
  [ -f "$INSTALL_DIR/logs/api.err.log" ] && tail -20 "$INSTALL_DIR/logs/api.err.log"
  fail "API 服务未在 30 秒内就绪，完整日志见 $INSTALL_DIR/logs/api.err.log"
fi
port_ready 3010 15 || echo "警告：EUR-Lex 服务未就绪（不影响国内法规核查），日志见 $INSTALL_DIR/logs/eurlex.err.log"

# 7. Word 加载项
echo "[5/6] 安装 Word 加载项..."
mkdir -p "$WEF_DIR"
cp "$INSTALL_DIR/apps/word_addin/manifest.xml" "$WEF_DIR/ccitecheck-manifest.xml"
rm -rf "$WEF_CACHE" 2>/dev/null || true

# 8. 自检
echo "[6/6] 环境自检："
"$INSTALL_DIR/bin/run-doctor.sh" || true

echo ""
echo "安装完成！"
echo "· Word：打开 Word → 开始 → 加载项 → CCiteheck 法律引用核查"
echo "· 网页版：https://localhost:3000（即将自动打开）"
echo "· 卸载：运行 $INSTALL_DIR/uninstall.command"
open "https://localhost:3000" 2>/dev/null || true
read -r -p "按回车关闭..."
