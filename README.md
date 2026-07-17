# window-logger

A small, dependency-light daemon that records the **focused window**, **power/presence
events**, and the **top processes by CPU** on a Linux desktop to per-host text logs, and
syncs those logs to a central server with `rsync`.

Built for **niri** (Wayland) first; the capture backends are abstracted so other
compositors and macOS can be added later.

## What it logs

One file per host per day: `logs/<hostname>-YYYY-MM-DD.log`. Tab-separated lines:

```
2026-07-17T00:16:27-07:00	POWER	online	boot=2026-07-13T16:30:00-07:00
2026-07-17T00:16:27-07:00	WINDOW	com.mitchellh.ghostty	260350	Build window logger
2026-07-17T00:21:05-07:00	PROCS	claude:314134:12.0 chrome:325288:8.0 niri:872:4.0
2026-07-17T00:16:36-07:00	POWER	suspend
2026-07-17T00:40:02-07:00	POWER	resume
2026-07-17T08:04:04-07:00	STATUS	previous-session-unclean	last=2026-07-17T02:11:59-07:00
2026-07-17T09:00:00-07:00	POWER	offline	reason=signal
```

`TYPE` is one of:

| TYPE     | meaning |
|----------|---------|
| `WINDOW` | focused window; columns = configured `fields` (default `app_id`, `pid`, `title`) |
| `POWER`  | `online` / `offline` / `suspend` / `resume` / `shutdown` / `lock` / `unlock` |
| `STATUS` | anomalies an auditor should see (unclean prior shutdown, capture backend down) |
| `PROCS`  | top-N processes by CPU as `comm:pid:cpu%` tokens |

Power + heartbeat lines let an auditor tell **on vs. off vs. asleep vs. crashed** apart
from mere idle time. A crash leaves a heartbeat gap with no clean `offline`; the next boot
emits `STATUS previous-session-unclean`.

## Capture behavior

- **Window** (hybrid): logged immediately on a real window switch; title-only changes are
  debounced (`title_debounce`) to avoid spam from live-updating titles; a heartbeat
  snapshot is written every `heartbeat_interval` even when nothing changes.
- **Power**: via logind D-Bus signals (`gdbus monitor`, no root). Clean `offline` is written
  on SIGTERM (i.e. on logout/shutdown when systemd stops the service).
- **Processes**: top-N by CPU sampled from `/proc` over a short window, every `interval`.

## Install (client)

From the repo:

```bash
./install.sh
```

Or ship a single self-contained script to each client:

```bash
./build-deploy.sh                      # produces dist/window-logger-deploy.sh
scp dist/window-logger-deploy.sh client:/tmp/ && ssh client 'bash /tmp/window-logger-deploy.sh'
```

The installer:
- installs the daemon to `~/.local/bin/window-logger`,
- writes `~/.config/window-logger/config.toml` (from the example, if absent),
- generates an SSH keypair for uploads (if absent),
- installs and enables a **systemd user service** (`window-logger.service`).

It never overwrites an existing config or key, so re-running upgrades safely.

## Server setup (uploads)

Uploads use **rsync over SSH key auth** (password auth is intentionally unsupported — there
is no secure non-interactive path for it). One dedicated, per-client key.

On the server, once:

```bash
sudo useradd -m -s /bin/bash loguser
sudo -u loguser mkdir -p /srv/window-logs
sudo chown loguser:loguser /srv/window-logs
```

Then authorize each client by adding its public key
(`~/.config/window-logger/id_ed25519.pub`) to `loguser`'s `~/.ssh/authorized_keys`. For
least privilege, lock the key to rsync-write into just that directory:

```
command="rrsync -wo /srv/window-logs/",restrict ssh-ed25519 AAAA... window-logger@client
```

Finally, on the client, set the destination in `~/.config/window-logger/config.toml`:

```toml
[upload]
enabled = true
destination = "loguser@your-server.example.com:/srv/window-logs/"
```

and restart: `systemctl --user restart window-logger.service`.

### Transmission tracking & resilience

- Logging never depends on the network. Uploads retry on an interval with exponential
  backoff and automatically catch up when connectivity returns.
- After a closed (past-day) file is **confirmed** present on the server (via an
  `rsync -ni` dry-run compare), it is moved into `logs/sent/`; the current day's file stays
  put and is marked `synced-open`. State is also recorded in `logs/.upload-state.json`.

## Commands

```bash
window-logger run                # the daemon (what systemd runs)
window-logger snapshot           # print one WINDOW line for the current focus (print-only)
window-logger snapshot --append  # ...and also append it to the audit log
window-logger upload             # run one upload cycle now
window-logger status             # resolved config + per-file transmission state
```

`snapshot` is print-only by default so this debugging command never leaves a stray line in
the audit log (which would otherwise look like an unclean session on the next start).

## Configuration

See `config.example.toml` for all options (paths, intervals, fields, redaction,
process sampling, upload). Every option is also settable via environment variable
`WINDOW_LOGGER_<SECTION>_<KEY>`, e.g. `WINDOW_LOGGER_PROCESSES_TOP_N=20`. Env wins over the
file — handy for per-host tweaks via the systemd unit's `Environment=`.

## Logs & troubleshooting

```bash
journalctl --user -u window-logger -f          # daemon diagnostics (not the audit log)
ls ~/.local/share/window-logger/logs/          # the audit logs
```

## Requirements / platform

- Linux, Wayland, **niri** (uses `niri msg` IPC), `python3` ≥ 3.11, `rsync`, systemd user
  session, `gdbus` (from glib, for power events).
- Structured for future backends (Hyprland/sway/X11, macOS) — see `CLAUDE.md`.
