#!/bin/bash
# DJ Grab first-run setup for the shared app. The app runs this by itself the first time it opens.
# Everything goes into ~/Library/Application Support/DJGrab. No Homebrew, no Xcode, no password needed.
# Prints one line per step ("STEP <pct> <text>") so the app can show progress.
set -eo pipefail
SUPPORT="$HOME/Library/Application Support/DJGrab"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$SUPPORT/bin" "$SUPPORT/engine"
export UV_INSTALL_DIR="$SUPPORT/uv" UV_NO_MODIFY_PATH=1 UV_PYTHON_INSTALL_DIR="$SUPPORT/python"
UV="$SUPPORT/uv/uv"

echo "STEP 5 Getting the Python installer"
if [[ ! -x "$UV" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi

echo "STEP 25 Installing Python (one time, about 40 MB)"
"$UV" python install 3.12 >/dev/null 2>&1
[[ -x "$SUPPORT/venv/bin/python3" ]] || "$UV" venv --seed --python 3.12 "$SUPPORT/venv" >/dev/null

echo "STEP 55 Installing the download tools"
"$UV" pip install --python "$SUPPORT/venv/bin/python3" -q --upgrade "yt-dlp[default]" mutagen static-ffmpeg deno

echo "STEP 80 Getting ffmpeg (audio converter)"
"$SUPPORT/venv/bin/python3" - <<'EOF'
import os, static_ffmpeg.run as r
ff, fp = r.get_or_fetch_platform_executables_else_raise()
b = os.path.expanduser("~/Library/Application Support/DJGrab/bin")
for src, name in ((ff, "ffmpeg"), (fp, "ffprobe")):
    dst = os.path.join(b, name)
    if os.path.lexists(dst): os.remove(dst)
    os.symlink(src, dst)
EOF
[[ -x "$SUPPORT/venv/bin/deno" ]] && ln -sf "$SUPPORT/venv/bin/deno" "$SUPPORT/bin/deno"

echo "STEP 95 Copying DJ Grab's engine"
cp -R "$HERE/engine/." "$SUPPORT/engine/"
mkdir -p "$SUPPORT/engine/logs"
echo "STEP 100 Ready"
