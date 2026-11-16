#!/bin/bash
# Rebuilds DJ Grab.app from the files next to this script, then reopens it.
# The app runs this by itself when an update is waiting. You can also run it yourself:
#   bash ~/Documents/"DJ Grab"/build_app.sh
set -eo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
# Resolve the real folder (not a symlink to it), so the links below never point at themselves.
HERE="$(cd "$(dirname "$0")" && pwd -P)"
# DJ Grab's code moved to Documents/DJ Grab (Nov 2026): an old copy hands the build over to the new one
NEW="$HOME/Documents/DJ Grab"
if [[ "$HERE" != "$(cd "$NEW" 2>/dev/null && pwd -P)" && -f "$NEW/build_app.sh" ]]; then exec bash "$NEW/build_app.sh" "$@"; fi
SUPPORT="$HOME/Library/Application Support/DJGrab"
SRC="$HERE/app-src"
LOG="$HOME/Library/Logs/DJGrab-update.log"
mkdir -p "$HERE/logs"
exec > >(tee "$HERE/logs/update.log") 2>&1      # a copy Claude can read too

fail() {
  echo "ERROR: $1"
  osascript -e "display alert \"DJ Grab update failed\" message \"$1 Details are in ~/Library/Logs/DJGrab-update.log\"" >/dev/null 2>&1 || true
  exit 1
}
trap 'fail "The build stopped with an error."' ERR

[[ -x "$SUPPORT/venv/bin/python3" ]] || fail "DJ Grab's engine isn't installed. Run install.sh first."

# Point the app and the djgrab command at this folder, so dropped-in updates apply.
rm -f "$SUPPORT/updates" "$SUPPORT/djgrab.py"
ln -sfn "$HERE" "$SUPPORT/updates"            # -f: fine if a second update ran at the same moment
ln -sfn "$HERE/djgrab.py" "$SUPPORT/djgrab.py"
# only one update builds at a time
LOCK="$SUPPORT/.building"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [[ -n "$(find "$LOCK" -maxdepth 0 -mmin -5 2>/dev/null)" ]]; then echo "Another update is already installing"; exit 0; fi
  rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK"
fi
trap 'rmdir "$LOCK" 2>/dev/null; fail "The build stopped with an error."' ERR
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

VER="$(tr -dc '0-9' < "$SRC/VERSION")"
echo "Building DJ Grab version $VER"
BUILD="$(mktemp -d)"
APP="$BUILD/DJ Grab.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sed "s/__VERSION__/$VER/g" "$SRC/Info.plist" > "$APP/Contents/Info.plist"
if [[ -d "$SRC/AppIcon.iconset" ]]; then
  iconutil -c icns "$SRC/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns" || true
fi
# The shared app carries its own engine + first-run setup (Vinny's copy uses the updates folder instead)
ENG="$APP/Contents/Resources/engine"; mkdir -p "$ENG"
cp "$HERE/djgrab.py" "$HERE/djtools.py" "$HERE/djcues.py" "$HERE/hqsep.py" "$HERE/artist_genres.json" \
   "$HERE/song_genres.json" "$HERE/moments.json" "$ENG/"
rsync -a --exclude __pycache__ "$HERE/roformer" "$ENG/"
cp "$SRC/VERSION" "$ENG/ENGINE_VERSION"
cp "$SRC/setup.sh" "$APP/Contents/Resources/setup.sh"
swiftc -O -parse-as-library -target "$(uname -m)-apple-macos13.0" \
  "$SRC/DJGrab.swift" -o "$APP/Contents/MacOS/DJGrab"
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true

# Replace the app wherever it's installed now
if [[ -d "/Applications/DJ Grab.app" ]]; then DEST="/Applications"
elif [[ -d "$HOME/Applications/DJ Grab.app" ]]; then DEST="$HOME/Applications"
elif [[ -w "/Applications" ]]; then DEST="/Applications"
else DEST="$HOME/Applications"; mkdir -p "$DEST"; fi

pkill -x DJGrab 2>/dev/null || true
sleep 1
rm -rf "$DEST/DJ Grab.app"
mv "$APP" "$DEST/"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST/DJ Grab.app" || true
rm -rf "$BUILD"
echo "Installed DJ Grab version $VER to $DEST"
# reopen it (twice if the first try doesn't take, e.g. while macOS is still registering the new app)
sleep 2
open "$DEST/DJ Grab.app" || true
sleep 5
pgrep -x DJGrab >/dev/null || open "$DEST/DJ Grab.app" || true
