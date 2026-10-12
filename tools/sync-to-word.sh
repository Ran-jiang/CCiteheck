#!/bin/bash
# 把仓库最新代码同步进 Word 插件的运行目录（默认 ~/Applications/CCiteheck）。
#
# 用法：
#   tools/sync-to-word.sh              # 拉取 origin 最新并同步
#   tools/sync-to-word.sh --dry-run    # 只看会改什么，不落地
#   tools/sync-to-word.sh --yes        # 重启服务前不再询问
#   tools/sync-to-word.sh --force      # 没有新提交也强制同步（修复运行目录被改乱）
#
# 只同步 src/ 与 apps/：.env、logs/、data/、debug_runs/、runtime/、vendor/ 均不触碰。
# 依赖文件（requirements.txt / package.json 等）有变更时会中止，因为那必须走完整重装。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${CCITECHECK_HOME:-$HOME/Applications/CCiteheck}"
WEF_DIR="$HOME/Library/Containers/com.microsoft.Word/Data/Documents/wef"
MANIFEST_SRC="$REPO/apps/word_addin/manifest.xml"
MANIFEST_DEST="$WEF_DIR/ccitecheck-manifest.xml"
SERVICE="com.ccitecheck.api"
HEALTH_URL="https://localhost:3000/api/health"
UID_NUM="$(id -u)"

DRY_RUN=0
ASSUME_YES=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y)  ASSUME_YES=1 ;;
    --force)   FORCE=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（可用：--dry-run --yes --force）" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[×]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 0. 预检 ----------
log "预检"
[ -d "$INSTALL_DIR" ] || die "运行目录不存在：$INSTALL_DIR（还没装过？先跑安装器）"
[ -d "$INSTALL_DIR/src" ] && [ -d "$INSTALL_DIR/apps" ] \
  || die "运行目录结构不对（缺 src/ 或 apps/）：$INSTALL_DIR"
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "不是 git 仓库：$REPO"

# 只关心已跟踪文件；未跟踪的产物目录不算脏
if ! git -C "$REPO" diff --quiet || ! git -C "$REPO" diff --cached --quiet; then
  echo
  git -C "$REPO" status --short --untracked-files=no
  echo
  die "仓库有未提交的改动，先提交或 stash，避免同步出一份和 GitHub 不一致的代码"
fi

BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
UPSTREAM="$(git -C "$REPO" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
[ -n "$UPSTREAM" ] || die "分支 $BRANCH 没有对应的上游分支，无法确定要同步哪个远端版本"
log "当前分支：$BRANCH  上游：$UPSTREAM"

# ---------- 1. 取远端 ----------
log "git fetch"
git -C "$REPO" fetch --quiet --prune origin

OLD="$(git -C "$REPO" rev-parse HEAD)"
NEW="$(git -C "$REPO" rev-parse "$UPSTREAM")"

if [ "$OLD" = "$NEW" ] && [ "$FORCE" -eq 0 ]; then
  log "没有新提交（HEAD = $UPSTREAM = ${NEW:0:7}）"
  # 即使代码没更新，运行目录也可能被手改过，报一下漂移情况
  # --omit-dir-times：目录 mtime 差异是安装留下的噪音，不算漂移
  # grep -v '^\.'：首字符 . 表示“内容无需更新，仅属性差异”，同样滤掉
  DRIFT_OPTS=(-an --itemize-changes --delete --omit-dir-times
      --exclude '__pycache__/' --exclude '*.pyc' --exclude 'tests/' --exclude '.DS_Store')
  DRIFT="$( { rsync "${DRIFT_OPTS[@]}" "$REPO/src/"  "$INSTALL_DIR/src/"
              rsync "${DRIFT_OPTS[@]}" "$REPO/apps/" "$INSTALL_DIR/apps/"; } 2>/dev/null \
            | grep -v '^\.' | head -20 || true)"
  if [ -n "$DRIFT" ]; then
    warn "运行目录与仓库存在差异，如需强制拉平请加 --force："
    echo "$DRIFT" | sed 's/^/    /'
  else
    log "运行目录与仓库一致，无需同步。"
  fi
  exit 0
fi

# ---------- 2. 依赖变更闸门 ----------
if [ "$OLD" != "$NEW" ]; then
  CHANGED="$(git -C "$REPO" diff --name-only "$OLD" "$NEW")"
else
  CHANGED=""   # --force 且无新提交：按“全都可能变了”处理
fi

DEP_HITS="$(printf '%s\n' "$CHANGED" | grep -E '^(requirements\.txt|package\.json|package-lock\.json|pyproject\.toml|tools/package/runtime-versions\.lock)$' || true)"
if [ -n "$DEP_HITS" ]; then
  echo
  printf '%s\n' "$DEP_HITS" | sed 's/^/    /'
  echo
  die "以上依赖文件有变更，rsync 同步不了（runtime/site-packages、vendor/ 需要重装）。
     请走完整流程：python3 tools/package/build.py 打包后跑 install.command"
fi

# ---------- 3. 拉取 ----------
if [ "$OLD" != "$NEW" ]; then
  COUNT="$(git -C "$REPO" rev-list --count "$OLD..$NEW")"
  log "拉取 $COUNT 个新提交：${OLD:0:7} -> ${NEW:0:7}"
  git -C "$REPO" --no-pager log --oneline "$OLD..$NEW" | sed 's/^/    /'
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "--dry-run：跳过 git pull"
  else
    git -C "$REPO" pull --ff-only --quiet origin "$BRANCH" \
      || die "git pull --ff-only 失败（本地与远端可能已分叉）"
  fi
else
  log "--force：无新提交，直接按当前 HEAD（${OLD:0:7}）拉平运行目录"
fi

# ---------- 4. 同步文件 ----------
RSYNC_OPTS=(-a --delete --omit-dir-times
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude 'tests/'
  --exclude '.DS_Store')
[ "$DRY_RUN" -eq 1 ] && RSYNC_OPTS+=(-n --itemize-changes)

log "同步 src/ 与 apps/ -> $INSTALL_DIR"
rsync "${RSYNC_OPTS[@]}" "$REPO/src/"  "$INSTALL_DIR/src/"  | sed 's/^/    /'
rsync "${RSYNC_OPTS[@]}" "$REPO/apps/" "$INSTALL_DIR/apps/" | sed 's/^/    /'

# ---------- 5. 判断要不要重启服务 ----------
NEED_RESTART=0
if [ -z "$CHANGED" ]; then
  NEED_RESTART=1   # --force 无提交信息，保守重启
elif printf '%s\n' "$CHANGED" | grep -qE '^(src/|apps/api/|apps/cli/|apps/feishu/|apps/web/)'; then
  NEED_RESTART=1
fi

if [ "$NEED_RESTART" -eq 0 ]; then
  log "本次只改了前端静态资源，服务无需重启——在 Word 里关掉任务窗格再打开即可看到新版。"
else
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "--dry-run：此处会重启 $SERVICE"
  else
    if [ "$ASSUME_YES" -eq 0 ]; then
      warn "即将重启后端服务；若此刻 Word 里有正在跑的核查任务，会被中断。"
      read -r -p "    继续？[y/N] " ans
      case "$ans" in
        y|Y|yes|YES) ;;
        *) die "已取消。文件已同步，但服务未重启——Python 改动尚未生效。" ;;
      esac
    fi
    log "重启 $SERVICE"
    launchctl kickstart -k "gui/$UID_NUM/$SERVICE"
    READY=0
    for _ in $(seq 1 20); do
      if [ "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 "$HEALTH_URL" 2>/dev/null)" = "200" ]; then
        READY=1; break
      fi
      sleep 1
    done
    [ "$READY" -eq 1 ] || die "服务重启后 20 秒内没起来，查日志：$INSTALL_DIR/logs/api.err.log"
    log "服务就绪：$(curl -sk --max-time 5 "$HEALTH_URL")"
  fi
fi

# ---------- 6. manifest ----------
WORD_RESTART=0
if [ -f "$MANIFEST_DEST" ] && cmp -s "$MANIFEST_SRC" "$MANIFEST_DEST"; then
  :
else
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "--dry-run：manifest 有变化，此处会更新 $MANIFEST_DEST"
    WORD_RESTART=1
  else
    log "manifest 有变化，更新到 Word 的 wef 目录"
    mkdir -p "$WEF_DIR"
    cp "$MANIFEST_SRC" "$MANIFEST_DEST"
    cp "$MANIFEST_SRC" "$INSTALL_DIR/apps/word_addin/manifest.xml"
    WORD_RESTART=1
  fi
fi

# ---------- 7. 小结 ----------
echo
log "完成（$( [ "$DRY_RUN" -eq 1 ] && echo '演练，未落地' || echo "运行目录现为 ${NEW:0:7}" )）"
if [ "$WORD_RESTART" -eq 1 ]; then
  warn "manifest 变了，需要完全退出 Word 再打开，加载项才会用新配置。"
else
  echo "    下一步：在 Word 里关掉任务窗格再重新打开即可。"
fi
