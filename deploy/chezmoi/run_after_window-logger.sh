#!/usr/bin/env bash
# chezmoi runs this after every `apply`. It:
#   1. generates a per-device upload SSH key if missing (prints pubkey to authorize),
#   2. enables the systemd user service,
#   3. restarts the service ONLY when the daemon binary changed (so routine applies
#      are quiet), by comparing a stored hash.
set -euo pipefail

BIN="$HOME/.local/bin/window-logger"
KEY="$HOME/.config/window-logger/id_ed25519"
STATE="$HOME/.local/state/window-logger/deployed.sha256"

# 1. per-device upload key
if [[ ! -f "$KEY" ]]; then
  mkdir -p "$(dirname "$KEY")"; chmod 700 "$(dirname "$KEY")"
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "window-logger@$(hostname)" >/dev/null
  chmod 600 "$KEY"
  echo "window-logger: generated a new upload key on $(hostname)."
  echo "  Authorize it on the server (add to dangrover@server.alder.dangrover.com ~/.ssh/authorized_keys):"
  echo "    $(cat "$KEY.pub")"
fi

# 2. enable the service (no-op if already enabled)
systemctl --user daemon-reload
systemctl --user enable window-logger.service >/dev/null 2>&1 || true
systemctl --user start window-logger.service   >/dev/null 2>&1 || true

# 3. restart only if the binary changed since last deploy
if [[ -x "$BIN" ]]; then
  mkdir -p "$(dirname "$STATE")"
  new="$(sha256sum "$BIN" | cut -d' ' -f1)"
  old="$(cat "$STATE" 2>/dev/null || true)"
  if [[ "$new" != "$old" ]]; then
    systemctl --user restart window-logger.service || true
    echo "$new" > "$STATE"
    echo "window-logger: restarted (daemon updated)."
  fi
fi
