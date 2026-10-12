#!/bin/bash
# 环境自检：法规库、千问、法宝、EUR-Lex 配置状态。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT:$ROOT/runtime/site-packages"
exec "$ROOT/runtime/python/bin/python3" -m apps.cli.main doctor --law-db "$ROOT/data/laws.sqlite"
