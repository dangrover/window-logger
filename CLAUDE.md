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
    `app_id`, `pid`, `title` (title last; sanitized only for TAB/newline safety —
    titles are recorded verbatim, see Design philosophy).
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
R11. **Client-side log retention.** Prune local log files older than a configurable
    `retain_days`, to bound disk use — **without** ever deleting them on the server (rsync
    is never run with `--delete`). Guarded by `require_sent` (default true): only files
    confirmed present on the server are eligible, so retention can't cause data loss.
R12. **Automatic versioning + version logging.** The tool version is derived, never
    manually bumped: live git short-hash of the checkout the daemon runs from (the primary
    chezmoi deploy is a real git checkout), falling back to a stamp injected at
    bundle/install time, else `unknown`. Logged as a `STATUS version ...` line at each
    startup (auditor sees which version produced a session's logs) and exposed via
    `window-logger version` / `--version` and in `status`.
R13. **User idle/presence qualification.** Log when the session goes input-idle and
    when it becomes active again, so an auditor can tell "screen on, nobody at the
    controls" apart from active use. `POWER idle` (detail `since=<iso>`, back-dated to
    when input actually stopped) and `POWER active` (detail `idle_for=<N>s`). Source:
    Wayland `ext-idle-notify-v1`, spoken directly over the compositor socket (pure
    stdlib, no new dependencies). Configurable `[idle] timeout` (default 300s) and
    `mode` (`inhibitor-aware` default: a held idle inhibitor, e.g. video playback,
    counts as present; `input-only` uses protocol v2 raw input silence). Behind an
    `IdleSource` abstraction (R9; macOS later via IOHIDIdleTime).

## Target environment (initial client: `dgframework`)

- **Compositor: niri** (Wayland). This is the primary supported backend.
- Focused-window source of truth: `niri msg --json focused-window` (one-shot JSON) and
  `niri msg --json event-stream` (continuous events). Fields available:
  `id, title, app_id, pid, workspace_id, is_focused, is_floating, is_urgent, layout`.
- Desktop shell: DankMaterialShell (DMS) / Quickshell (`dms`, `qs` present).
- Tooling present on client: `niri`, `jq`, `rsync`, `python3` (3.14), user `systemd`.
- Distro: CachyOS (Arch-based).

### Design philosophy — consensual, faithful monitoring (READ BEFORE ADDING "PRIVACY" KNOBS)

This tool is deployed **only between consenting parties who have agreed to be monitored**
(e.g. the operator's own fleet, or people who have explicitly opted in). Given that, the
guiding principle is **faithful, complete capture** — the log should be an honest record of
what was on screen, not a curated one.

Concretely, per an explicit 2026-07-17 decision by the user:

- **Do NOT add redaction, title-filtering, app-ignore, or similar "privacy" features.**
  `ignore_app_ids` and `redact_title_patterns` existed briefly and were deliberately
  **removed**. Do not reintroduce them or anything of that nature (title scrubbing,
  per-app blocklists, "sensitive window" suppression, etc.).
- Window **titles are recorded verbatim.** The only title processing is mechanical: strip
  TAB/newline so a line stays one TSV row, and truncate absurdly long titles
  (`max_title_length`) purely for log hygiene — not for content control.
- The right place for any "should this person be monitored at all" decision is **consent
  and deployment scope**, not runtime filtering in this tool.
- If a future request sounds like redaction/obfuscation, surface this note and confirm
  intent before implementing — it contradicts the established design intent.

### Other design notes

- niri exposes an **event stream**, so focus changes can be captured event-driven rather
  than only by polling — but the user asked for interval-based snapshots, so interval
  polling is the baseline behavior. Keep the window-source behind a small abstraction so
  other compositors (Hyprland `hyprctl`, sway `swaymsg`, X11) can be added later.

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
  - **IdleSource** (`WaylandIdleSource`): user idle/active via `ext-idle-notify-v1`,
    spoken directly over the Wayland socket (pure stdlib wire client — niri advertises
    the protocol at v2; verified live). Emits `POWER idle`/`POWER active` (R13);
    availability problems emit `STATUS idle-source-unavailable/recovered`.
    `WAYLAND_DISPLAY` is recovered from the niri socket name when systemd didn't
    import the session env.
  - **ProcSampler**: top-N processes by CPU from `/proc`, top-style delta over a short
    window, on its own interval (R10). → `PROCS` lines.
  - **Uploader**: rsync over SSH (R5), network-resilient with backoff (R7), confirms via
    `rsync -ni` dry-run and maintains `.upload-state.json` + `sent/` folder (R8).
  - Backends selected by `platform` for future macOS support (R9).
  - `apply_env_overrides` makes every option settable via `WINDOW_LOGGER_<SECTION>_<KEY>`.
- CLI subcommands: `run` (daemon), `snapshot` (one-shot test line), `upload` (force sync),
  `status` (health check), `tail` (tail -f the local audit log, follows across daily
  rotation; `-n` trailing lines), `version`.
- **SIGHUP reloads config live** (serviced by the ticker, not the handler) and writes a
  `STATUS config-reload` line; `systemctl --user reload window-logger` triggers it via
  `ExecReload`. Tunables (intervals, debounce, fields, top_n, upload/retention settings)
  apply live; structural changes (log_dir, hostname, enabling/disabling subsystems) still
  need a restart. Loops read intervals fresh each cycle so reloads take effect.
- `config.example.toml`, `install.sh` (repo installer + systemd unit), `build-deploy.sh`
  (bundles everything into one self-contained deploy script for R3).

Log line schema: `ISO8601±tz <TAB> {WINDOW|POWER|STATUS|PROCS} <TAB> …`.
- WINDOW: configured fields in order (title last).
- POWER:  `event <TAB> detail`  (online/offline/suspend/resume/shutdown/lock/unlock/
          idle/active). The `online` detail always carries
          `boot=<iso> prior=<none|clean|unclean>` so each startup self-describes how
          the previous session ended. `idle` carries `since=<iso>` (when input
          actually stopped — the line itself is stamped one timeout later, at
          detection); `active` carries `idle_for=<N>s`. An idle bracket interrupted
          by `STATUS idle-source-unavailable` is unterminated (state unknown).
- STATUS: `event <TAB> detail`. `previous-session-unclean` fires only when prior=unclean
          and is qualified with `last=<ts> last_event=<TYPE[:subtype]>`. Also `version`
          (each startup), `config-reload` / `config-reload-failed` (SIGHUP),
          window-source-unavailable/recovered, power-source-unavailable,
          idle-source-unavailable/recovered.
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
- 2026-07-17 — Refined unclean-shutdown detection: startup always classifies the prior
  session as none/clean/unclean (annotated on the `online` line); the unclean STATUS line
  is qualified with the last event. `snapshot` is now print-only by default (add `--append`)
  so it never pollutes the audit log / trips the detector.
- 2026-07-17 — Decisions from live testing: keep full window titles (no redaction); PROCS
  CPU normalized to 0-100% of machine by default (`[processes] normalize`).
- 2026-07-17 — **Removed `ignore_app_ids` and `redact_title_patterns` entirely.** Tool is for
  consensual monitoring between agreeing parties; faithful verbatim capture is the intent.
  No redaction/filtering knobs — see "Design philosophy". Do not reintroduce.
- 2026-07-17 — Added R12 (automatic git-hash-based versioning + `STATUS version` startup
  line; `version`/`--version` commands). No manual version bumps.
- 2026-07-17 — Review fixes: SIGHUP config reload (logs `STATUS config-reload`; `ExecReload`
  in the unit; loops read intervals live); verify dry-run failure now falls back to trusting
  the successful primary upload (so files aren't left un-prunable). Server-side security
  findings (world-readable logs, full-shell key) noted but intentionally deferred by user.
- 2026-07-17 — Added R11 (client-side retention: prune local logs older than retain_days,
  server copies untouched, `require_sent` safety guard).
- 2026-07-17 — Server (first client `dgframework`): dangrover@server.alder.dangrover.com,
  logs to ~/window-logs/ (quick same-user setup, publicly reachable FQDN). First real
  upload verified end-to-end on 2026-07-17.
- 2026-07-17 — `status` upgraded to a health check (service state, last-log staleness, last
  transmission) with HEALTHY/WARN/UNHEALTHY verdict + matching exit code (0/1/2) and `--json`.
- 2026-07-17 — Added R13 (idle/presence qualification). Investigated logind IdleHint
  (dead — niri never sets it), DMS IPC (no idle handler), swayidle (extra dependency);
  chose speaking `ext-idle-notify-v1` directly over the Wayland socket in pure stdlib.
  Kept only `since=` on the idle line (`timeout=` was redundant — user decision).
- 2026-07-17 — Added `tail` subcommand (tail -f of the local audit log, follows daily
  rotation) — user request; was previously done by hand.
- 2026-07-17 — Multi-device deploy plan: chezmoi. `deploy/chezmoi/` manages config + unit as
  dotfiles and fetches the single-file daemon from GitHub via a chezmoi external (auto-update);
  a run_ hook does per-device keygen + restart-on-change. (User will roll this out later.)

## Status

Bootstrapping — building v1 against the `dgframework` niri client. Update this file as
decisions land and as requirements change (see above).
