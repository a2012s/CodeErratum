#!/usr/bin/env bash
# Install a freshly built bundle over /Applications/CodeErratum.app — safely.
#
# Why this script exists instead of a one-line `ditto`:
#
# A PyInstaller bundle IS its archive. The frozen app reads the PYZ lazily from
# its own executable, at import time — not from a memory snapshot taken at
# startup. `ditto src dst` overwrites the destination file *in place* (same
# inode, truncate + write), so installing over a running app leaves that
# process reading a torn file: the archive table of contents comes from the old
# bytes, the module payloads from the new ones, and the next lazy import dies
# with `zlib.error: Error -3 while decompressing data: incorrect header check`.
#
# The symptom is baffling from the outside: the app is fine on disk (selftest
# passes), a fresh launch works, but the *running* instance throws the moment
# it prints Chinese text — Rich imports `rich._unicode_data` lazily, and only
# for characters that need a cell-width lookup. It also stops answering
# SIGTERM, because the signal handler runs in a thread whose teardown dies the
# same way.
#
# So: quit the old instance first, then move the old bundle aside (rename, not
# overwrite) and copy the new one into a path that does not exist yet.
#
# Usage: packaging/install.sh [path/to/CodeErratum.app]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:-$ROOT/dist/CodeErratum.app}"
DEST="/Applications/CodeErratum.app"
TRASH="$HOME/.Trash"

if [[ ! -d "$SRC" ]]; then
    echo "找不到待安装的 bundle: $SRC" >&2
    echo "先跑 .venv/bin/python packaging/build.py" >&2
    exit 1
fi

# 1. Quit the running instance. SIGTERM is enough for a healthy app; the
#    fallback exists for an instance whose signal thread is already broken.
if pgrep -f "$DEST/Contents/MacOS/CodeErratum" >/dev/null 2>&1; then
    echo "→ 退出正在运行的 CodeErratum"
    pkill -f "$DEST/Contents/MacOS/CodeErratum" || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        pgrep -f "$DEST/Contents/MacOS/CodeErratum" >/dev/null 2>&1 || break
        sleep 0.5
    done
    if pgrep -f "$DEST/Contents/MacOS/CodeErratum" >/dev/null 2>&1; then
        echo "  应用未响应 SIGTERM（信号线程可能已损坏），强制结束"
        pkill -9 -f "$DEST/Contents/MacOS/CodeErratum" || true
        sleep 1
    fi
fi

# 2. Move the old bundle aside with a rename. The running app keeps its open
#    inode, so even a race with a late launch cannot tear the archive.
OLD=""
if [[ -d "$DEST" ]]; then
    OLD="$DEST.old-$(date +%s)"
    echo "→ 旧 bundle 移到 $OLD"
    mv "$DEST" "$OLD"
fi

# 3. Copy the new one into a path that does not exist yet: a fresh file, never
#    an in-place overwrite.
echo "→ 安装 $SRC"
ditto "$SRC" "$DEST"

# 4. Verify before declaring success: the frozen binary must run, and the
#    archive must be readable.
echo "→ 自检"
"$DEST/Contents/MacOS/CodeErratum" --selftest >/dev/null
VERSION=$(defaults read "$DEST/Contents/Info.plist" CFBundleShortVersionString)
echo "  已安装版本: $VERSION"

# 5. Only now is the old bundle disposable.
if [[ -n "$OLD" ]]; then
    mkdir -p "$TRASH"
    mv "$OLD" "$TRASH/CodeErratum.app.$(date +%Y%m%d-%H%M%S)" || true
fi

echo "完成。启动： open $DEST"
