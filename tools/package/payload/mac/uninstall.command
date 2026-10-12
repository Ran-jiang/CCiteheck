#!/bin/bash
# CCiteheck 卸载器（macOS）
set -uo pipefail

INSTALL_DIR="$HOME/Applications/CCiteheck"
AGENT_DIR="$HOME/Library/LaunchAgents"
WEF_DIR="$HOME/Library/Containers/com.microsoft.Word/Data/Documents/wef"
WEF_CACHE="$HOME/Library/Containers/com.microsoft.Word/Data/Library/Caches/Wef"
UID_NUM="$(id -u)"

echo "CCiteheck 卸载器"
echo "================"

echo "[1/4] 停止并移除常驻服务..."
for svc in api eurlex; do
  launchctl bootout "gui/$UID_NUM/com.ccitecheck.$svc" 2>/dev/null || true
  rm -f "$AGENT_DIR/com.ccitecheck.$svc.plist"
done

echo "[2/4] 移除 Word 加载项..."
rm -f "$WEF_DIR/ccitecheck-manifest.xml"
rm -rf "$WEF_CACHE" 2>/dev/null || true

echo "[3/4] localhost HTTPS 开发证书默认保留（其他 Office 插件开发可能仍需）。"
read -r -p "是否同时移除证书？(y/N) " yn
if [ "${yn:-N}" = "y" ] || [ "${yn:-N}" = "Y" ]; then
  "$INSTALL_DIR/runtime/node/bin/node" \
    "$INSTALL_DIR/vendor/certs/node_modules/office-addin-dev-certs/cli.js" uninstall 2>/dev/null || true
fi

echo "[4/4] 删除安装目录 $INSTALL_DIR ..."
read -r -p "确认删除全部程序文件（含 .env 与日志）？(y/N) " yn
if [ "${yn:-N}" = "y" ] || [ "${yn:-N}" = "Y" ]; then
  rm -rf "$INSTALL_DIR"
  echo "已删除。"
else
  echo "已保留程序目录，仅停用了服务与 Word 加载项。"
fi
read -r -p "按回车关闭..."
