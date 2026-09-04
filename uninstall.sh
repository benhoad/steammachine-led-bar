#!/usr/bin/env bash
# Removes ledbar. Keeps ~/.config/ledbar unless --purge is given.
set -euo pipefail
APP_DIR="${LEDBAR_APP_DIR:-$HOME/.local/share/ledbar}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ledbar"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="$HOME/.local/bin"

systemctl --user disable --now ledbar.service ledbar-openrgb.service 2>/dev/null || true
rm -f "$UNIT_DIR/ledbar.service" "$UNIT_DIR/ledbar-openrgb.service"
systemctl --user daemon-reload
rm -rf "$APP_DIR"
rm -f "$BIN_DIR/ledbar"
if [ "${1:-}" = "--purge" ]; then
    rm -rf "$CONFIG_DIR"
    echo "removed $CONFIG_DIR"
else
    echo "kept $CONFIG_DIR (use --purge to delete it)"
fi
echo "ledbar removed. The udev rules in /etc/udev/rules.d/60-ledbar-openrgb.rules (if installed) were left in place:"
echo "  sudo rm /etc/udev/rules.d/60-ledbar-openrgb.rules && sudo udevadm control --reload-rules"
