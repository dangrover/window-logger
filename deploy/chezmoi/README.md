# Deploying window-logger with chezmoi

This directory is a **reference** you copy into your own chezmoi source repo (the tree that
`chezmoi` manages). It keeps two things in sync across every device:

1. **Config + systemd unit** — managed as normal dotfiles.
2. **The daemon code itself** — pulled fresh from the GitHub repo via a chezmoi *external*,
   so `chezmoi update` on any device always installs the latest `window_logger.py`.

## Why this works cleanly

`window_logger.py` is a single self-contained file, so a chezmoi `file` external is enough —
no bundling, no cloning. The code lives in `dangrover/window-logger`; your dotfiles repo
only references it.

## Files (map into your chezmoi source root)

```
.chezmoiexternal.toml                     -> fetches ~/.local/bin/window-logger from GitHub
run_after_window-logger.sh                -> keygen + enable + restart-on-change hook
dot_config/
  private_window-logger/config.toml       -> ~/.config/window-logger/config.toml (mode 0600)
  systemd/user/window-logger.service      -> ~/.config/systemd/user/window-logger.service
```

The unit uses `%h` paths, so it's identical on every device. `config.toml` is also identical
(hostname is auto-detected); rename it to `config.toml.tmpl` if you ever want per-device
values. The upload SSH key is a **per-device secret** — it is *not* stored in chezmoi; the
`run_` hook generates one per device and prints its public key for you to authorize.

## First-time setup on a new device

```bash
chezmoi init --apply https://github.com/dangrover/dotfiles.git   # your dotfiles repo
# The run_ hook prints the new device's public key -> add it to the server's authorized_keys.
window-logger status        # verify (see health check below)
```

## Keeping code up to date

```bash
chezmoi update                       # git pull dotfiles + apply; refreshes externals per refreshPeriod
chezmoi apply --refresh-externals    # force an immediate pull of the latest daemon
```

The `run_after_` hook restarts the service **only when the daemon binary actually changed**,
so routine applies don't churn the service (or spam the audit log with online/offline pairs).

## Pinning for stability

`.chezmoiexternal.toml` points at the `master` branch (always latest). For fleet stability,
point `url` at a released tag instead (e.g. `.../refs/tags/v1.0/window_logger.py`) and bump
the tag when you want devices to move — that turns "latest code" into "latest *approved*
code".
