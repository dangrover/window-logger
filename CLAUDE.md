# window-logger

A small daemon that periodically records the **currently focused window** on a Linux
desktop to a local text log, and syncs those logs to a central server.

## Requirements

Numbered so future changes can be tracked against them. **When requirements change or new
ones arrive, update this list and the "Requirement changes" log at the bottom — keep this
file the source of truth for scope.**

R1. **Focused-window logging.** Record the currently focused window (same info as the
    DMS/dankbar "focused window" widget) to a per-host text log file.
R2. **Config file.** Snapshot/heartbeat interval, upload interval, paths, rsync target,
    and other options are all set via a config file (`config.example.toml`).
R3. **Easy single-script deployment.** One script deploys to each Linux client and installs
    it as a **systemd user service** (per-user — focus is a per-session concept).
R4. **Log format.** Files named per host + per day; each line is
    `timestamp<TAB>TYPE<TAB>fields…`. Hostname is in the filename. Chosen window fields:
    `app_id`, `pid`, `title` (title last; sanitized/redactable).
R5. **Upload via rsync.** Logs sync to an established server/path via `rsync` over SSH key
    auth using saved credentials.
R6. **Power / presence events.** Record power & session events — boot/online, suspend,
    resume, shutdown, clean offline, and detection of *unclean* previous shutdown — so an
    auditor can tell from the log alone when a machine was on, off, asleep, or having a
    problem. Also surface operational anomalies (e.g. window backend unavailable) as
    `STATUS` events. Disambiguates "log gap = idle" vs "gap = machine off/broken".
R7. **Network resilience.** Capture never depends on the network; it keeps writing locally
    while offline. Uploads retry and automatically catch up when connectivity returns
    (no data loss, daemon never crashes on a failed upload).
R8. **Transmission tracking.** The client tracks whether each log file has been transmitted
    — via a local state manifest (`.upload-state.json`) AND by moving confirmed-sent,
    closed (past-day) files into a `sent/` subfolder. rsync itemized/dry-run output is used
    to *confirm* a file is actually present on the server before marking it sent.
R9. **Platform-agnostic design (future-proofing).** Window capture and power capture sit
    behind abstract `WindowSource` / `PowerSource` backends chosen by a platform selector.
    Linux backends (niri + logind) ship now; a macOS backend (Quartz for focus, IOKit/pmset
    for power) is a *later* step but the structure must not preclude it.
R10. **Top-N processes by CPU.** Periodically log the top-N (default 15) processes by CPU,
    at a customizable interval, enabled by default. Every option is addressable via config
    **and** environment variable (`WINDOW_LOGGER_<SECTION>_<KEY>`).

## Target environment (initial client: `dgframework`)

- **Compositor: niri** (Wayland). This is the primary supported backend.
- Focused-window source of truth: `niri msg --json focused-window` (one-shot JSON) and
  `niri msg --json event-stream` (continuous events). Fields available:
  `id, title, app_id, pid, workspace_id, is_focused, is_floating, is_urgent, layout`.
- Desktop shell: DankMaterialShell (DMS) / Quickshell (`dms`, `qs` present).
- Tooling present on client: `niri`, `jq`, `rsync`, `python3` (3.14), user `systemd`.
- Distro: CachyOS (Arch-based).

### Design notes

- niri exposes an **event stream**, so focus changes can be captured event-driven rather
  than only by polling — but the user asked for interval-based snapshots, so interval
  polling is the baseline behavior. Keep the window-source behind a small abstraction so
  other compositors (Hyprland `hyprctl`, sway `swaymsg`, X11) can be added later.
- Window titles can contain sensitive data (URLs, document names). Treat privacy/redaction
  as a real config concern.

## Repo conventions

- Keep the client deployable as a single self-contained script + a config file.
- Don't commit real secrets, rsync destinations, or SSH keys. Provide `*.example` files.
- Real config lives outside the repo (installed to `~/.config/window-logger/`).

## Architecture (how the requirements map to code)

- `window_logger.py` — single-file daemon. Threads, all writes serialized through one
  `LogWriter` (thread-safe, daily rotation):
  - **WindowSource** (`NiriWindowSource`): reads `niri msg --json event-stream`, tracks
    window state + focus. Hybrid capture (R1): log immediately on a real window switch;
    throttle title-only churn via `title_debounce` (niri titles carry live spinners);
    heartbeat snapshot every `heartbeat_interval`. → `WINDOW` lines.
  - **PowerSource** (`LogindPowerSource`): reads `gdbus monitor --system` on
    `org.freedesktop.login1` (no root needed) for `PrepareForSleep` / `PrepareForShutdown`
    / session `Lock`/`Unlock`. Plus startup `online`/`boot` + unclean-shutdown detection
    + SIGTERM → clean `offline`. → `POWER` / `STATUS` lines (R6).
  - **ProcSampler**: top-N processes by CPU from `/proc`, top-style delta over a short
    window, on its own interval (R10). → `PROCS` lines.
  - **Uploader**: rsync over SSH (R5), network-resilient with backoff (R7), confirms via
    `rsync -ni` dry-run and maintains `.upload-state.json` + `sent/` folder (R8).
  - Backends selected by `platform` for future macOS support (R9).
  - `apply_env_overrides` makes every option settable via `WINDOW_LOGGER_<SECTION>_<KEY>`.
- CLI subcommands: `run` (daemon), `snapshot` (one-shot test line), `upload` (force sync),
  `status` (config + per-file transmission state).
- `config.example.toml`, `install.sh` (repo installer + systemd unit), `build-deploy.sh`
  (bundles everything into one self-contained deploy script for R3).

Log line schema: `ISO8601±tz <TAB> {WINDOW|POWER|STATUS|PROCS} <TAB> …`.
- WINDOW: configured fields in order (title last).
- POWER:  `event <TAB> detail`  (online/offline/suspend/resume/shutdown/lock/unlock).
- STATUS: `event <TAB> detail`  (previous-session-unclean, window-source-unavailable, …).
- PROCS:  space-joined `comm:pid:cpu%` tokens (optionally `:cmdline`).

## Requirement changes

Append dated entries here whenever scope shifts (newest last):

- 2026-07-17 — Initial scope R1–R5 (window logging, config, single-script systemd deploy,
  log format, rsync upload).
- 2026-07-17 — Added R6 (power/presence events for auditor on/off/problem visibility).
- 2026-07-17 — Added R7 (network resilience) and R8 (per-file transmission tracking).
- 2026-07-17 — Added R9 (platform-agnostic backends; macOS is a later step).
- 2026-07-17 — Added R10 (top-N processes by CPU) + env-var override for all options.
- 2026-07-17 — Decided: server auth is SSH key (dedicated per-client key, least-privilege
  `rrsync` in authorized_keys). Password auth rejected (no secure non-interactive path).

## Status

Bootstrapping — building v1 against the `dgframework` niri client. Update this file as
decisions land and as requirements change (see above).
