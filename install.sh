#!/usr/bin/env bash
# Install / uninstall the AI Rate Limit indicator.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID="ai-ratelimit@raptor-zip.github.io"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}"

# claude-* 時代の旧バージョンの残り物
OLD_UUID="claude-ratelimit@raptor-zip.github.io"
OLD_UNITS=("claude-usage.timer" "claude-usage.service")

remove_legacy() {
    systemctl --user disable --now claude-usage.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/claude-usage.timer" "$UNIT_DIR/claude-usage.service"
    gnome-extensions disable "$OLD_UUID" 2>/dev/null || true
    rm -rf "$HOME/.local/share/gnome-shell/extensions/$OLD_UUID"
    rm -f "$BIN_DIR/claude-usage-fetch"
    rm -rf "$CACHE_DIR/claude-usage"
}

uninstall() {
    remove_legacy

    systemctl --user disable --now ai-usage.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/ai-usage.timer" "$UNIT_DIR/ai-usage.service"
    systemctl --user daemon-reload 2>/dev/null || true

    gnome-extensions disable "$UUID" 2>/dev/null || true
    rm -rf "$EXT_DIR"
    rm -f "$BIN_DIR/ai-usage-fetch"
    rm -rf "$CACHE_DIR/ai-usage"

    echo "Uninstalled. Restart GNOME Shell (Alt+F2 -> r on X11) to drop the indicator."
}

install_all() {
    # Fetcher -> ~/.local/bin so nothing depends on where this repo lives
    mkdir -p "$BIN_DIR"
    install -m 755 "$SRC/bin/ai-usage-fetch.py" "$BIN_DIR/ai-usage-fetch"

    # GNOME Shell extension (copied, not symlinked: some setups refuse symlinks)
    mkdir -p "$EXT_DIR"
    install -m 644 \
        "$SRC/extension/$UUID/metadata.json" \
        "$SRC/extension/$UUID/extension.js" \
        "$SRC/extension/$UUID/stylesheet.css" \
        "$EXT_DIR/"

    # systemd user timer
    mkdir -p "$UNIT_DIR"
    install -m 644 "$SRC/systemd/ai-usage.service" "$SRC/systemd/ai-usage.timer" "$UNIT_DIR/"
    systemctl --user daemon-reload
    systemctl --user enable --now ai-usage.timer

    # First fetch, so the panel has something to show right away
    "$BIN_DIR/ai-usage-fetch" || true

    # 新しいものを入れてから旧バージョンを掃除（旧タイマーの二重実行を防ぐ）
    remove_legacy
    systemctl --user daemon-reload

    cat <<EOF

Installed:
  extension : $EXT_DIR
  fetcher   : $BIN_DIR/ai-usage-fetch
  timer     : $UNIT_DIR/ai-usage.timer (every 2 minutes)

To activate:
  1) Restart GNOME Shell: Alt+F2 -> r -> Enter  (X11 only; on Wayland, log out and back in)
  2) gnome-extensions enable $UUID
EOF
}

case "${1:-install}" in
    uninstall) uninstall ;;
    install) install_all ;;
    *) echo "usage: $0 [install|uninstall]" >&2; exit 1 ;;
esac
