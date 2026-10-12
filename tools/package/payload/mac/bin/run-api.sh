#!/bin/bash
# CCiteheck API 服务（HTTPS :3000）。由 launchd 拉起，日志由 plist 重定向。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT:$ROOT/runtime/site-packages"
CERT_DIR="$HOME/.office-addin-dev-certs"
exec "$ROOT/runtime/python/bin/python3" -m apps.api.server \
  --cert "$CERT_DIR/localhost.crt" --key "$CERT_DIR/localhost.key"
