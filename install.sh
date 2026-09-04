#!/usr/bin/env bash
# ledbar installer for Bazzite (works on any systemd based Linux with Python 3.11+).
#
# Installs to ~/.local/share/ledbar, creates ~/.config/ledbar/config.toml,
# and sets up two user services: ledbar-openrgb (headless OpenRGB SDK server)
# and ledbar (the LED bar itself). Nothing touches the immutable /usr.
#
#   ./install.sh                # install / update, enable services
#   ./install.sh --udev         # also install OpenRGB udev rules (asks for sudo)
#   ./install.sh --no-service   # copy files only
#   ./install.sh --no-openrgb-service   # you run OpenRGB's SDK server yourself
#   ./install.sh --boot         # also start at boot, before anyone logs in (enables lingering)
#   ./install.sh --openrgb      # download the current OpenRGB release candidate AppImage into ~/Applications
#                               # (Bazzite's ujust install-openrgb ships 1.0rc2, which cannot drive ASRock's
#                               #  ARGB headers per LED; that was fixed in 1.0rc3)
set -euo pipefail

APP_DIR="${LEDBAR_APP_DIR:-$HOME/.local/share/ledbar}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ledbar"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="$HOME/.local/bin"
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OPENRGB_PY_VERSION="0.3.6"
PYTHON="${PYTHON:-python3}"

WITH_SERVICE=1
WITH_OPENRGB_SERVICE=1
WITH_UDEV=0
WITH_BOOT=0
WITH_OPENRGB_DL=0
for arg in "$@"; do
    case "$arg" in
        --no-service) WITH_SERVICE=0 ;;
        --no-openrgb-service) WITH_OPENRGB_SERVICE=0 ;;
        --udev) WITH_UDEV=1 ;;
        --boot) WITH_BOOT=1 ;;
        --openrgb) WITH_OPENRGB_DL=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*"; }

# --- python ------------------------------------------------------------------
step "Checking Python"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 not found" >&2; exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ledbar needs Python 3.10 or newer (found $("$PYTHON" --version))" >&2; exit 1
fi
NEED_TOMLI=0
if ! "$PYTHON" -c 'import tomllib' >/dev/null 2>&1; then
    NEED_TOMLI=1   # Python 3.10 has no tomllib; the tomli package is a drop-in
fi
echo "using $("$PYTHON" --version) at $(command -v "$PYTHON")"

# distro detection (only used for hints)
OS_ID="unknown"; OS_LIKE=""
if [ -r /etc/os-release ]; then
    OS_ID="$( . /etc/os-release && echo "${ID:-unknown}" )"
    OS_LIKE="$( . /etc/os-release && echo "${ID_LIKE:-}" )"
fi

# --- stop a running copy while we replace its files ---------------------------
if command -v systemctl >/dev/null 2>&1 && systemctl --user is-active --quiet ledbar.service 2>/dev/null; then
    step "Stopping the running ledbar service for the upgrade"
    systemctl --user stop ledbar.service || true
fi

# --- files -------------------------------------------------------------------
step "Installing files to $APP_DIR"
mkdir -p "$APP_DIR" "$CONFIG_DIR" "$UNIT_DIR" "$BIN_DIR"
rm -rf "$APP_DIR/ledbar" "$APP_DIR/bin" "$APP_DIR/tools"
cp -r "$HERE/ledbar" "$HERE/bin" "$HERE/tools" "$APP_DIR/"
cp "$HERE/udev/60-ledbar-openrgb.rules" "$APP_DIR/"
find "$APP_DIR/ledbar" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
chmod +x "$APP_DIR/bin/"* "$APP_DIR/tools/"*.py
ln -sfn "$APP_DIR/bin/ledbar" "$BIN_DIR/ledbar"
echo "launcher: $BIN_DIR/ledbar"

# --- python dependency (openrgb-python) --------------------------------------
step "Installing openrgb-python $OPENRGB_PY_VERSION"
installed=0
if [ -e "$APP_DIR/venv/bin/python" ] && ! "$APP_DIR/venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
    echo "existing virtualenv no longer works (system Python changed?); recreating it"
    rm -rf "$APP_DIR/venv"
fi
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    if "$PYTHON" -m venv "$APP_DIR/venv" >/dev/null 2>&1; then
        echo "created virtualenv $APP_DIR/venv"
    else
        warn "could not create a virtualenv (python3-venv/ensurepip missing?)"
        rm -rf "$APP_DIR/venv"
    fi
fi
if [ -x "$APP_DIR/venv/bin/python" ]; then
    extra=""
    if [ "$NEED_TOMLI" = 1 ]; then extra="tomli"; fi
    # shellcheck disable=SC2086  # $extra is intentionally empty or a single package name
    if "$APP_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check "openrgb-python==$OPENRGB_PY_VERSION" $extra; then
        installed=1
        echo "installed into the virtualenv"
    else
        warn "pip install failed inside the virtualenv"
    fi
fi
if [ "$installed" = 0 ]; then
    echo "falling back to vendoring the pure-Python wheel into $APP_DIR/vendor"
    "$PYTHON" - "$APP_DIR/vendor" "$OPENRGB_PY_VERSION" <<'PY'
import io, json, sys, urllib.request, zipfile, shutil, os
target, version = sys.argv[1], sys.argv[2]
meta = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/openrgb-python/{version}/json", timeout=30))
url = next(u["url"] for u in meta["urls"] if u["filename"].endswith("py3-none-any.whl"))
data = urllib.request.urlopen(url, timeout=60).read()
shutil.rmtree(target, ignore_errors=True)
os.makedirs(target)
zipfile.ZipFile(io.BytesIO(data)).extractall(target)
print(f"vendored {url.rsplit('/', 1)[-1]} into {target}")
PY
    installed=1
    if [ "$NEED_TOMLI" = 1 ]; then
        warn "Python $("$PYTHON" --version | cut -d' ' -f2) needs the 'tomli' package: pip install --user tomli"
    fi
fi
if ! "$APP_DIR/bin/ledbar" --version >/dev/null; then
    echo "the launcher does not work; see errors above" >&2; exit 1
fi

# --- config ------------------------------------------------------------------
step "Configuration"
if [ -f "$CONFIG_DIR/config.toml" ]; then
    echo "keeping existing $CONFIG_DIR/config.toml"
else
    cp "$HERE/ledbar/config.example.toml" "$CONFIG_DIR/config.toml"
    echo "created $CONFIG_DIR/config.toml (edit count / reverse / brightness to match your bar)"
fi
"$APP_DIR/bin/ledbar" config check || true

# --- OpenRGB -----------------------------------------------------------------
step "OpenRGB"
OPENRGB_FALLBACK_URL="https://codeberg.org/OpenRGB/OpenRGB/releases/download/release_candidate_1.0rc3.1/OpenRGB_1.0rc3.1_x86_64_5e81e26.AppImage"
download_openrgb() {
    local dir="$HOME/Applications" url name
    mkdir -p "$dir"
    url="$("$PYTHON" - <<'PY'
import json, platform, urllib.request
arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "armv7l": "armhf", "i686": "i386"}.get(platform.machine(), "x86_64")

def newest():
    releases = json.load(urllib.request.urlopen("https://codeberg.org/api/v1/repos/OpenRGB/OpenRGB/releases?limit=5", timeout=20))
    for release in releases:
        for asset in release.get("assets", []):
            if asset["name"].endswith(".AppImage") and f"_{arch}_" in asset["name"]:
                return asset["browser_download_url"]
    return ""

try:
    print(newest())
except Exception:
    print("")
PY
)"
    [ -n "$url" ] || url="$OPENRGB_FALLBACK_URL"
    name="$(basename "$url")"
    if [ -s "$dir/$name" ]; then
        echo "already downloaded: $dir/$name"
    else
        echo "downloading $url"
        if command -v curl >/dev/null 2>&1; then
            if [ -t 1 ]; then curl -fL --progress-bar -o "$dir/$name.part" "$url"; else curl -fsSL -o "$dir/$name.part" "$url"; fi
        else
            wget -q -O "$dir/$name.part" "$url"
        fi
        mv "$dir/$name.part" "$dir/$name"
    fi
    chmod +x "$dir/$name"
    touch "$dir/$name"      # the newest AppImage wins in ledbar-openrgb-server's search
    echo "OpenRGB AppImage ready: $dir/$name"
}
if [ "$WITH_OPENRGB_DL" = 1 ]; then
    download_openrgb
fi
# same search as bin/ledbar-openrgb-server: any case, newest first (Gear Lever renames files)
OPENRGB_APPIMAGE="$( (find "$HOME/Applications" "$HOME/AppImages" "$HOME/.local/bin" "$HOME/Apps" \
                        -maxdepth 1 -type f -iname '*openrgb*.appimage' 2>/dev/null || true) \
                    | while IFS= read -r f; do
                          printf '%s\t%s\n' "$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)" "$f"
                      done | sort -rn | head -1 | cut -f2-)"
OPENRGB_COMMAND="$("$PYTHON" - "$CONFIG_DIR/config.toml" <<'PY'
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
try:
    with open(sys.argv[1], "rb") as handle:
        print(tomllib.load(handle).get("openrgb", {}).get("command", "") or "")
except Exception:
    print("")
PY
)"
if [ -n "$OPENRGB_COMMAND" ]; then
    echo "using [openrgb] command from the config: $OPENRGB_COMMAND"
elif [ -n "$OPENRGB_APPIMAGE" ]; then
    echo "found AppImage: $OPENRGB_APPIMAGE"
    [ -x "$OPENRGB_APPIMAGE" ] || chmod +x "$OPENRGB_APPIMAGE" 2>/dev/null || true
    case "$(basename "$OPENRGB_APPIMAGE")" in
        *1.0rc1*|*1.0rc2*|*0.9*|*0.8*|*0.7*)
            warn "this OpenRGB is older than 1.0rc3 and cannot drive ASRock's ARGB headers per LED;"
            warn "run:  bash install.sh --openrgb   to download the current release candidate" ;;
    esac
elif command -v openrgb >/dev/null 2>&1; then
    echo "found openrgb: $(command -v openrgb)"
elif command -v flatpak >/dev/null 2>&1 && flatpak info org.openrgb.OpenRGB >/dev/null 2>&1; then
    echo "found Flatpak org.openrgb.OpenRGB"
else
    case "$OS_ID $OS_LIKE" in
        *bazzite*)                       hint="ujust install-openrgb" ;;
        *arch*|*cachyos*|*chimeraos*|*manjaro*|*endeavouros*) hint="sudo pacman -S openrgb" ;;
        *fedora*|*nobara*)               hint="sudo dnf install openrgb" ;;
        *suse*)                          hint="sudo zypper install OpenRGB" ;;
        *debian*|*ubuntu*|*mint*|*pop*)  hint="sudo apt install openrgb   (needs 0.9+; otherwise: flatpak install flathub org.openrgb.OpenRGB)" ;;
        *steamos*)                       hint="flatpak install flathub org.openrgb.OpenRGB   (or the AppImage from openrgb.org into ~/Applications)" ;;
        *)                               hint="flatpak install flathub org.openrgb.OpenRGB   (or your distro's openrgb package, 0.9 or newer)" ;;
    esac
    warn "OpenRGB not found. Install it, then re-run this installer:  $hint"
fi

# --- udev rules --------------------------------------------------------------
if [ "$WITH_UDEV" = 1 ]; then
    step "udev rules (sudo)"
    if ls /usr/lib/udev/rules.d/*openrgb*.rules /lib/udev/rules.d/*openrgb*.rules /etc/udev/rules.d/60-openrgb.rules >/dev/null 2>&1; then
        echo "your OpenRGB package already provides udev rules; nothing to do"
    else
        RULES_SRC="$HERE/udev/60-ledbar-openrgb.rules"
        if [ -n "$OPENRGB_APPIMAGE" ]; then
            TMP="$(mktemp -d)"
            if (cd "$TMP" && "$OPENRGB_APPIMAGE" --appimage-extract 'usr/lib/udev/rules.d/60-openrgb.rules' >/dev/null 2>&1) \
                && [ -s "$TMP/squashfs-root/usr/lib/udev/rules.d/60-openrgb.rules" ]; then
                RULES_SRC="$TMP/squashfs-root/usr/lib/udev/rules.d/60-openrgb.rules"
                echo "using the full rules file bundled in the AppImage"
            else
                echo "could not extract rules from the AppImage; using the minimal ASRock rules"
            fi
        fi
        sudo install -m 644 "$RULES_SRC" /etc/udev/rules.d/60-ledbar-openrgb.rules
        sudo udevadm control --reload-rules
        sudo udevadm trigger
        echo "installed /etc/udev/rules.d/60-ledbar-openrgb.rules"
    fi
else
    echo "udev rules not installed (re-run with --udev if OpenRGB cannot see your motherboard as a normal user)"
fi

# --- systemd -----------------------------------------------------------------
step "systemd user services"
for unit in ledbar-openrgb.service ledbar.service; do
    sed "s#%h/.local/share/ledbar#$APP_DIR#g" "$HERE/systemd/$unit" > "$UNIT_DIR/$unit"
done
if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found; units copied to $UNIT_DIR but not enabled"
    WITH_SERVICE=0
else
    systemctl --user daemon-reload
fi
if [ "$WITH_SERVICE" = 1 ]; then
    if [ "$WITH_OPENRGB_SERVICE" = 1 ]; then
        systemctl --user enable ledbar-openrgb.service
        if systemctl --user restart ledbar-openrgb.service; then
            echo "enabled ledbar-openrgb.service (headless OpenRGB SDK server)"
        else
            warn "ledbar-openrgb.service failed to start: journalctl --user -u ledbar-openrgb"
        fi
    else
        systemctl --user disable --now ledbar-openrgb.service 2>/dev/null || true
        echo "ledbar-openrgb.service left disabled (using your own OpenRGB SDK server)"
    fi
    systemctl --user enable ledbar.service
    if systemctl --user restart ledbar.service; then
        echo "enabled ledbar.service"
    else
        warn "ledbar.service failed to start: journalctl --user -u ledbar"
    fi
else
    echo "services installed but not enabled (--no-service)"
fi
if [ "$WITH_BOOT" = 1 ] && command -v loginctl >/dev/null 2>&1; then
    step "Start at boot (user lingering)"
    if loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"; then
        echo "lingering enabled: the ledbar services now start at boot, before login"
    else
        warn "could not enable lingering; run:  sudo loginctl enable-linger $USER"
    fi
fi

# --- done --------------------------------------------------------------------
step "Done"
cat <<MSG
Next steps:
  1. ledbar status          # check Steam, sensors and the OpenRGB device/zone ledbar picked
  2. ledbar identify        # work out [leds] reverse / color_order / offset, then edit
                            #   $CONFIG_DIR/config.toml
  3. ledbar demo            # walk through every Steam Machine pattern on your bar
  4. systemctl --user restart ledbar        # apply config changes
     journalctl --user -u ledbar -f          # watch the log

If $BIN_DIR is not on your PATH, use $APP_DIR/bin/ledbar instead.
MSG
