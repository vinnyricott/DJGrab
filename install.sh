#!/bin/bash
# DJ Grab installer (first-time setup). Run from Terminal:  bash ~/Downloads/DJGrab/install.sh
# After this, updates install themselves. See README.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support/DJGrab"
HOME_DIR="$HOME/DJ music/.djgrab"       # where DJ Grab's code lives (hidden folder)
OUT_DEFAULT="$HOME/DJ music/DJ Grab"

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$1"; }

[[ "$(uname)" == "Darwin" ]] || { echo "This installer is for macOS."; exit 1; }

say "Checking Apple developer tools (needed to build the app)"
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install || true
  echo "A window opened to install the Command Line Tools. Finish that, then run this installer again."
  exit 0
fi

say "Checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x $b ]] && eval "$($b shellenv)"; done
fi
if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew (it will ask for your Mac password)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x $b ]] && eval "$($b shellenv)"; done
fi

say "Installing ffmpeg, Python and Deno (Deno is what yt-dlp needs to read YouTube)"
brew install ffmpeg python@3.12 deno
PY="$(brew --prefix python@3.12)/bin/python3.12"

say "Setting up the download engine"
mkdir -p "$SUPPORT" "$HOME_DIR"
if [[ "$HERE" != "$HOME_DIR" ]]; then
  cp -R "$HERE/djgrab.py" "$HERE/build_app.sh" "$HERE/app-src" "$HERE/README.md" "$HOME_DIR/"
fi
[[ -d "$SUPPORT/venv" ]] || "$PY" -m venv "$SUPPORT/venv"
"$SUPPORT/venv/bin/python3" -m pip install -q --upgrade pip
"$SUPPORT/venv/bin/python3" -m pip install -q --upgrade "yt-dlp[default]" mutagen

# Terminal shortcut:  djgrab <link>
BIN="$(brew --prefix)/bin/djgrab"
cat > "$BIN" <<EOF
#!/bin/bash
exec "$SUPPORT/venv/bin/python3" "$SUPPORT/djgrab.py" "\$@"
EOF
chmod +x "$BIN"
mkdir -p "$OUT_DEFAULT"

say "Building DJ Grab.app"
bash "$HOME_DIR/build_app.sh"

say "Done"
echo "Saves to: $OUT_DEFAULT  (change it in DJ Grab > Settings)"
echo "Terminal: djgrab <link>"
