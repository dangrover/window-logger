#!/usr/bin/env bash
# window-logger installer (Linux / systemd user service).
#
# Run from the repo:            ./install.sh
# Or via the bundled deploy:    dist/window-logger-deploy.sh  (sets WL_SRC_DIR)
#
# Idempotent: safe to re-run to upgrade the script/unit. Never overwrites an
# existing config.toml or SSH key.
set -euo pipefail

SRC="${WL_SRC_DIR:-$(cd "$(dirname "$(readlink -f "$0")")" && pwd)}"

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
BIN="$BIN_DIR/window-logger"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/window-logger"
CFG="$CFG_DIR/config.toml"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/window-logger.service"
KEY="$CFG_DIR/id_ed25519"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*" >&2; }

# --- sanity checks ---
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v rsync   >/dev/null || warn "rsync not found - uploads will not work"
command -v niri    >/dev/null || warn "niri not found - this client may be unsupported"
command -v systemctl >/dev/null || { echo "systemd (systemctl) is required"; exit 1; }

# --- install the daemon script ---
say "Installing daemon -> $BIN"
mkdir -p "$BIN_DIR"
install -m 0755 "$SRC/window_logger.py" "$BIN"
# Stamp the version if installing from a git checkout (the copy has no .git).
# If the source was already stamped (e.g. the bundle), the "" pattern won't match.
VER="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || true)"
if [[ -n "$VER" ]]; then
  DATE="$(git -C "$SRC" show -s --format=%cs HEAD 2>/dev/null || true)"
  sed -i "s|^_EMBEDDED_VERSION = \"\"|_EMBEDDED_VERSION = \"$VER ($DATE)\"|" "$BIN"
fi

# --- config ---
mkdir -p "$CFG_DIR"
chmod 700 "$CFG_DIR"
if [[ -f "$CFG" ]]; then
  say "Config exists, leaving as-is: $CFG"
else
  say "Creating config from example: $CFG"
  install -m 0600 "$SRC/config.example.toml" "$CFG"
  warn "Edit $CFG - set [upload] destination and enable it when ready."
fi

# --- ssh key for uploads ---
if [[ -f "$KEY" ]]; then
  say "SSH key exists: $KEY"
else
  say "Generating SSH key for uploads: $KEY"
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "window-logger@$(hostname)" >/dev/null
  chmod 600 "$KEY"
fi

# --- systemd user unit ---
say "Installing systemd user unit -> $UNIT"
mkdir -p "$UNIT_DIR"
sed -e "s#@BIN@#$BIN#g" -e "s#@CONFIG@#$CFG#g" \
    "$SRC/systemd/window-logger.service.in" > "$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now window-logger.service

say "Done. Service status:"
systemctl --user --no-pager --lines=0 status window-logger.service || true

cat <<EOF

Next steps
----------
1) Authorize this client on the server. Add its PUBLIC key to loguser's
   authorized_keys (least-privilege form shown):

     command="rrsync -wo /srv/window-logs/",restrict $(cat "$KEY.pub")

2) Point uploads at the server and enable them in:
     $CFG
   set [upload] enabled = true and the correct destination, then:
     systemctl --user restart window-logger.service
     window-logger upload      # test one cycle
     window-logger status      # see per-file transmission state

Logs:  ${XDG_DATA_HOME:-$HOME/.local/share}/window-logger/logs/
View:  journalctl --user -u window-logger -f
EOF
