#!/usr/bin/env python3
"""window-logger — log the focused window and power/presence events to per-host text
logs, and sync them to a server with rsync.

Single-file daemon. Requirements are tracked in CLAUDE.md (R1–R9).

Backends are platform-abstracted (WindowSource / PowerSource) so other platforms
(e.g. macOS) can be added later; only the Linux (niri + logind) backends ship today.

Subcommands:
    run       Run the daemon (default; used by the systemd service).
    snapshot  Write/print a single WINDOW line for the current focus, then exit.
    upload    Run one upload cycle now, then exit.
    status    Print resolved config and per-file transmission state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform as _platform
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - very old python
    tomllib = None

APP_NAME = "window-logger"

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "general": {
        "log_dir": "~/.local/share/window-logger/logs",
        "hostname": "",  # "" -> system hostname
    },
    "capture": {
        "heartbeat_interval": 60,      # seconds; snapshot even if focus unchanged (R1)
        "title_debounce": 4.0,         # seconds; collapse title-only churn (spinners)
        "max_title_length": 500,
        "fields": ["app_id", "pid", "title"],   # order of WINDOW columns (R4)
        "ignore_app_ids": [],          # never log these app_ids
        "redact_title_patterns": [],   # regexes; matched spans -> [REDACTED]
        "log_no_focus": True,          # emit a line when nothing is focused
    },
    "power": {
        "enabled": True,
        "log_suspend_resume": True,
        "log_shutdown": True,
        "log_lock_unlock": False,      # off by default (noise/privacy)
        "detect_unclean_shutdown": True,
    },
    "processes": {
        "enabled": True,               # log top-N processes by CPU (R10)
        "interval": 60,                # seconds between PROCS samples
        "top_n": 15,
        "sample_window": 0.5,          # seconds; CPU measured over this window (top-style)
        "normalize": True,             # True: 0-100% of whole machine; False: top/Irix (per-core sum)
        "include_cmdline": False,      # append a truncated cmdline (may contain secrets)
        "cmdline_max_length": 80,
    },
    "upload": {
        "enabled": False,              # off until a destination is configured
        "interval": 300,               # seconds between upload cycles
        "destination": "",             # user@host:/remote/path/
        "ssh_key": "~/.config/window-logger/id_ed25519",
        "ssh_port": 22,
        "ssh_options": [],             # extra -o options, e.g. ["ConnectTimeout=10"]
        "known_hosts": "~/.config/window-logger/known_hosts",
        "strict_host_key_checking": "accept-new",
        "timeout": 120,                # seconds per rsync invocation
        "max_backoff": 3600,           # cap on retry backoff after failures
        "move_sent": True,             # move confirmed+closed files to sent/
        "verify": True,                # confirm remote copy via rsync -ni dry run
        "bwlimit": 0,                  # KB/s; 0 = unlimited
    },
}

CONFIG_SEARCH = [
    os.environ.get("WINDOW_LOGGER_CONFIG", ""),
    "~/.config/window-logger/config.toml",
    "/etc/window-logger/config.toml",
]


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce(default, raw: str):
    """Coerce an env-var string to the type of the matching default value."""
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [x for x in (s.strip() for s in raw.split(",")) if x]
    return raw


def apply_env_overrides(cfg: dict) -> dict:
    """Any option is addressable via WINDOW_LOGGER_<SECTION>_<KEY> (e.g.
    WINDOW_LOGGER_PROCESSES_TOP_N=20). Env wins over the config file."""
    for section, opts in DEFAULTS.items():
        for key, default in opts.items():
            env = f"WINDOW_LOGGER_{section}_{key}".upper()
            if env in os.environ:
                cfg.setdefault(section, {})[key] = _coerce(default, os.environ[env])
    return cfg


def load_config(path: str | None) -> tuple[dict, str | None]:
    """Return (config, source_path). Missing file -> defaults only. Env overrides apply."""
    candidates = [path] if path else CONFIG_SEARCH
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand).expanduser()
        if p.is_file():
            if tomllib is None:
                die("python too old: tomllib required (Python 3.11+)")
            with p.open("rb") as fh:
                user = tomllib.load(fh)
            return apply_env_overrides(_deep_merge(DEFAULTS, user)), str(p)
    return apply_env_overrides(_deep_merge(DEFAULTS, {})), None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def die(msg: str, code: int = 1):
    print(f"{APP_NAME}: error: {msg}", file=sys.stderr)
    sys.exit(code)


def logmsg(msg: str):
    """Operational/diagnostic log -> stderr (captured by journald). NOT the audit log."""
    print(f"[{dt.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}",
          file=sys.stderr, flush=True)


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d")


def boot_time_iso() -> str | None:
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime"):
                    ts = int(line.split()[1])
                    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(
                        timespec="seconds")
    except OSError:
        pass
    return None


def iter_lines(proc: subprocess.Popen, stop_event: threading.Event, timeout=0.5):
    """Yield lines from proc.stdout, but wake every `timeout` seconds to check the
    stop event so shutdown is prompt (readline() alone blocks indefinitely)."""
    fd = proc.stdout
    assert fd is not None
    while not stop_event.is_set():
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return
        if not ready:
            if proc.poll() is not None:
                return
            continue
        line = fd.readline()
        if line == "":  # EOF
            return
        yield line


def sanitize_field(val) -> str:
    """Make a value safe for a single TAB-separated column."""
    s = "" if val is None else str(val)
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


# --------------------------------------------------------------------------- #
# Log writer (thread-safe, daily rotation) — R4
# --------------------------------------------------------------------------- #

class LogWriter:
    def __init__(self, log_dir: Path, hostname: str):
        self.log_dir = log_dir
        self.hostname = hostname
        self._lock = threading.Lock()
        self._fh = None
        self._cur_date = None
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, date_str: str) -> Path:
        return self.log_dir / f"{self.hostname}-{date_str}.log"

    def write(self, kind: str, fields: list):
        """Append one line: ISO8601 <TAB> KIND <TAB> fields..."""
        ts = now_iso()
        date_str = ts[:10]
        cols = [ts, kind] + [sanitize_field(f) for f in fields]
        line = "\t".join(cols) + "\n"
        with self._lock:
            if self._fh is None or self._cur_date != date_str:
                if self._fh is not None:
                    self._fh.close()
                self._cur_date = date_str
                self._fh = self._path_for(date_str).open("a", encoding="utf-8")
            self._fh.write(line)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self):
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def last_line_across_logs(self) -> str | None:
        """Return the last non-empty line of the most recent prior log file (for
        unclean-shutdown detection). Excludes the sent/ subdir."""
        files = sorted(p for p in self.log_dir.glob(f"{self.hostname}-*.log")
                       if p.is_file())
        for p in reversed(files):
            try:
                data = p.read_text(encoding="utf-8", errors="replace").rstrip("\n")
            except OSError:
                continue
            if data:
                return data.rsplit("\n", 1)[-1]
        return None


# --------------------------------------------------------------------------- #
# Window source backends (R1, R9)
# --------------------------------------------------------------------------- #

class WindowSnapshot:
    __slots__ = ("app_id", "pid", "title", "wid")

    def __init__(self, app_id="", pid="", title="", wid=None):
        self.app_id = app_id or ""
        self.pid = "" if pid in (None, "") else str(pid)
        self.title = title or ""
        self.wid = wid

    def identity(self):
        """What counts as a 'different window' (vs. a mere title change)."""
        return (self.wid, self.app_id, self.pid)

    def is_none(self):
        return not self.app_id and not self.title and self.wid is None


class WindowSource:
    """Abstract focus source. Subclasses call self.on_update(snapshot) on change and
    implement current() for one-shot snapshots."""

    def __init__(self):
        self.on_update = lambda snap: None

    def start(self, stop_event: threading.Event):
        raise NotImplementedError

    def current(self) -> WindowSnapshot:
        raise NotImplementedError


def ensure_niri_socket():
    """Under systemd the graphical-session env may not be imported, so NIRI_SOCKET
    can be unset. Discover the newest niri socket in XDG_RUNTIME_DIR and export it."""
    if os.environ.get("NIRI_SOCKET"):
        return
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    try:
        socks = sorted(Path(runtime).glob("niri.*.sock"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        socks = []
    if socks:
        os.environ["NIRI_SOCKET"] = str(socks[0])


class NiriWindowSource(WindowSource):
    """Focus via niri's JSON event stream. Maintains window state by id."""

    def __init__(self):
        super().__init__()
        self._windows: dict = {}
        self._focused_id = None
        self._lock = threading.Lock()
        ensure_niri_socket()

    # --- one-shot ---
    def current(self) -> WindowSnapshot:
        try:
            out = subprocess.run(
                ["niri", "msg", "--json", "focused-window"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip() and out.stdout.strip() != "null":
                return self._to_snap(json.loads(out.stdout))
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
            logmsg(f"niri focused-window query failed: {e}")
        return WindowSnapshot()

    @staticmethod
    def _to_snap(w: dict) -> WindowSnapshot:
        return WindowSnapshot(app_id=w.get("app_id") or "", pid=w.get("pid"),
                              title=w.get("title") or "", wid=w.get("id"))

    def _emit_focus(self):
        with self._lock:
            wid = self._focused_id
            w = self._windows.get(wid) if wid is not None else None
        snap = self._to_snap(w) if w else WindowSnapshot()
        self.on_update(snap)

    # --- event loop ---
    def start(self, stop_event: threading.Event):
        backoff = 1
        announced_down = False
        while not stop_event.is_set():
            try:
                proc = subprocess.Popen(
                    ["niri", "msg", "--json", "event-stream"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError as e:
                logmsg(f"cannot start niri event-stream: {e}")
                if not announced_down:
                    self.on_backend_status(False, str(e))
                    announced_down = True
                stop_event.wait(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue
            if announced_down:
                self.on_backend_status(True, "")
                announced_down = False
            backoff = 1
            try:
                for line in iter_lines(proc, stop_event):
                    line = line.strip()
                    if line:
                        self._handle_event(line)
            except OSError as e:
                logmsg(f"niri event-stream read error: {e}")
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except (OSError, subprocess.SubprocessError):
                    proc.kill()
            if not stop_event.is_set():
                logmsg("niri event-stream ended; reconnecting")
                if not announced_down:
                    self.on_backend_status(False, "event-stream ended")
                    announced_down = True
                stop_event.wait(1)

    # optional hook set by the daemon to record STATUS lines
    def on_backend_status(self, up: bool, detail: str):
        pass

    def _handle_event(self, line: str):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        changed = False
        with self._lock:
            if "WindowsChanged" in ev:
                self._windows = {w["id"]: w for w in ev["WindowsChanged"]["windows"]}
                foc = [w["id"] for w in self._windows.values() if w.get("is_focused")]
                self._focused_id = foc[0] if foc else None
                changed = True
            elif "WindowOpenedOrChanged" in ev:
                w = ev["WindowOpenedOrChanged"]["window"]
                self._windows[w["id"]] = w
                if w.get("is_focused"):
                    self._focused_id = w["id"]
                changed = w.get("is_focused") or w["id"] == self._focused_id
            elif "WindowClosed" in ev:
                wid = ev["WindowClosed"]["id"]
                self._windows.pop(wid, None)
                if wid == self._focused_id:
                    self._focused_id = None
                    changed = True
            elif "WindowFocusChanged" in ev:
                self._focused_id = ev["WindowFocusChanged"].get("id")
                changed = True
        if changed:
            self._emit_focus()


# --------------------------------------------------------------------------- #
# Power source backends (R6, R9)
# --------------------------------------------------------------------------- #

class PowerSource:
    """Abstract power/session source. Calls self.on_event(event, detail)."""

    def __init__(self):
        self.on_event = lambda event, detail: None

    def start(self, stop_event: threading.Event):
        raise NotImplementedError


class LogindPowerSource(PowerSource):
    """Power/session events via `gdbus monitor` on org.freedesktop.login1.

    Parses lines such as:
      /org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true)
      /org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForShutdown (true)
      /org/freedesktop/login1/session/_31: org.freedesktop.login1.Session.Lock ()
    """

    SLEEP_RE = re.compile(r"PrepareForSleep \((true|false)\)")
    SHUTDOWN_RE = re.compile(r"PrepareForShutdown \((true|false)\)")
    LOCK_RE = re.compile(r"Session\.Lock \(\)")
    UNLOCK_RE = re.compile(r"Session\.Unlock \(\)")

    def __init__(self, log_suspend_resume=True, log_shutdown=True, log_lock_unlock=False):
        super().__init__()
        self.log_suspend_resume = log_suspend_resume
        self.log_shutdown = log_shutdown
        self.log_lock_unlock = log_lock_unlock

    def start(self, stop_event: threading.Event):
        if not shutil.which("gdbus"):
            logmsg("gdbus not found; power events disabled")
            self.on_event("problem", "power-source-unavailable=gdbus-missing")
            return
        backoff = 1
        while not stop_event.is_set():
            try:
                proc = subprocess.Popen(
                    ["gdbus", "monitor", "--system", "--dest",
                     "org.freedesktop.login1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError as e:
                logmsg(f"cannot start gdbus monitor: {e}")
                stop_event.wait(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1
            try:
                for line in iter_lines(proc, stop_event):
                    self._handle(line.strip())
            except OSError as e:
                logmsg(f"gdbus monitor read error: {e}")
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except (OSError, subprocess.SubprocessError):
                    proc.kill()
            if not stop_event.is_set():
                stop_event.wait(1)

    def _handle(self, line: str):
        m = self.SLEEP_RE.search(line)
        if m:
            if self.log_suspend_resume:
                self.on_event("suspend" if m.group(1) == "true" else "resume", "")
            return
        m = self.SHUTDOWN_RE.search(line)
        if m and m.group(1) == "true":
            if self.log_shutdown:
                self.on_event("shutdown", "logind")
            return
        if self.log_lock_unlock:
            if self.LOCK_RE.search(line):
                self.on_event("lock", "")
            elif self.UNLOCK_RE.search(line):
                self.on_event("unlock", "")


# --------------------------------------------------------------------------- #
# Process sampler — top-N by CPU (R10)
# --------------------------------------------------------------------------- #

class ProcSampler:
    """Sample the top-N processes by CPU from /proc, top-style: measure CPU used over
    a short window. Platform-specific (Linux /proc); a macOS backend would go behind the
    same interface later (R9)."""

    def __init__(self, top_n=15, sample_window=0.5, normalize=True,
                 include_cmdline=False, cmdline_max_length=80):
        self.top_n = int(top_n)
        self.sample_window = float(sample_window)
        self.normalize = bool(normalize)
        self.include_cmdline = bool(include_cmdline)
        self.cmdline_max = int(cmdline_max_length)
        self.clk_tck = os.sysconf("SC_CLK_TCK")
        self.ncpu = os.cpu_count() or 1

    @staticmethod
    def _read_stat(pid: str):
        """Return (comm, utime+stime jiffies) for a pid, or None."""
        try:
            with open(f"/proc/{pid}/stat") as fh:
                data = fh.read()
        except OSError:
            return None
        # comm is in parens and may contain spaces/parens; split on the last ')'
        lp, rp = data.find("("), data.rfind(")")
        if lp < 0 or rp < 0:
            return None
        comm = data[lp + 1:rp]
        rest = data[rp + 2:].split()
        try:  # after ')' index 0 is 'state'; utime=field14 (idx 11), stime=field15 (idx 12)
            jiffies = int(rest[11]) + int(rest[12])
        except (IndexError, ValueError):
            return None
        return comm, jiffies

    def _cmdline(self, pid: str) -> str:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            return ""
        s = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        return s[: self.cmdline_max]

    def _snapshot(self) -> dict:
        snap = {}
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            st = self._read_stat(entry.name)
            if st:
                snap[entry.name] = st
        return snap

    def sample(self) -> list[str] | None:
        """Return a list of 'comm:pid:cpu%' tokens for the top-N by CPU, or None."""
        s1 = self._snapshot()
        time.sleep(self.sample_window)
        s2 = self._snapshot()
        results = []
        for pid, (comm, j2) in s2.items():
            prev = s1.get(pid)
            if not prev:
                continue
            dj = j2 - prev[1]
            if dj <= 0:
                continue
            cpu = dj / (self.clk_tck * self.sample_window) * 100.0
            if self.normalize:
                cpu /= self.ncpu
            results.append((cpu, pid, comm))
        results.sort(reverse=True)
        tokens = []
        for cpu, pid, comm in results[: self.top_n]:
            name = comm.replace(":", "_").replace(" ", "_")
            tok = f"{name}:{pid}:{cpu:.1f}"
            if self.include_cmdline:
                cmd = self._cmdline(pid).replace("\t", " ").replace(":", " ")
                if cmd:
                    tok += f":{cmd}"
            tokens.append(tok)
        return tokens


# --------------------------------------------------------------------------- #
# Uploader — network-resilient rsync + transmission tracking (R5, R7, R8)
# --------------------------------------------------------------------------- #

class Uploader:
    def __init__(self, cfg: dict, log_dir: Path, hostname: str):
        self.cfg = cfg
        self.log_dir = log_dir
        self.hostname = hostname
        self.sent_dir = log_dir / "sent"
        self.manifest_path = log_dir / ".upload-state.json"
        self._lock = threading.Lock()
        self._failures = 0

    # --- manifest ---
    def _load_manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"files": {}, "last_success": None, "last_attempt": None,
                    "last_error": None}

    def _save_manifest(self, man: dict):
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(man, indent=2))
        tmp.replace(self.manifest_path)

    def _ssh_command(self) -> str:
        u = self.cfg
        key = str(Path(u["ssh_key"]).expanduser())
        parts = ["ssh", "-i", key, "-p", str(u["ssh_port"]),
                 "-o", "BatchMode=yes",
                 "-o", f"StrictHostKeyChecking={u['strict_host_key_checking']}"]
        kh = u.get("known_hosts")
        if kh:
            parts += ["-o", f"UserKnownHostsFile={Path(kh).expanduser()}"]
        for opt in u.get("ssh_options", []):
            parts += ["-o", opt]
        # quote key path in case of spaces
        return " ".join(f'"{p}"' if " " in p else p for p in parts)

    def _rsync_base(self) -> list:
        u = self.cfg
        cmd = ["rsync", "-a", "--partial", "--timeout", str(int(u["timeout"])),
               "--exclude", "sent/", "--exclude", ".upload-state.json",
               "--exclude", "*.tmp",
               "-e", self._ssh_command()]
        if u.get("bwlimit"):
            cmd += [f"--bwlimit={int(u['bwlimit'])}"]
        return cmd

    def _dest(self) -> str:
        d = self.cfg["destination"]
        return d if d.endswith("/") else d + "/"

    def _run_rsync(self, extra: list) -> subprocess.CompletedProcess:
        cmd = self._rsync_base() + extra + [f"{self.log_dir}/", self._dest()]
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=int(self.cfg["timeout"]) + 30)

    def _closed_local_files(self) -> list[str]:
        """Names of *.log files in the log dir (excludes today's open file)."""
        out = []
        for p in sorted(self.log_dir.glob(f"{self.hostname}-*.log")):
            if p.is_file():
                out.append(p.name)
        return out

    @staticmethod
    def _is_closed(name: str) -> bool:
        m = re.search(r"-(\d{4}-\d{2}-\d{2})\.log$", name)
        return bool(m) and m.group(1) < today_str()

    def upload_once(self) -> tuple[bool, str]:
        """One upload cycle. Returns (ok, message). Never raises for network errors."""
        with self._lock:
            man = self._load_manifest()
            man["last_attempt"] = now_iso()
            if not self.cfg.get("enabled"):
                return False, "upload disabled"
            if not self.cfg.get("destination"):
                man["last_error"] = "no destination configured"
                self._save_manifest(man)
                return False, "no destination configured"
            try:
                res = self._run_rsync([])
            except subprocess.TimeoutExpired:
                self._failures += 1
                man["last_error"] = "rsync timeout"
                self._save_manifest(man)
                return False, "rsync timeout"
            except OSError as e:
                self._failures += 1
                man["last_error"] = f"rsync spawn failed: {e}"
                self._save_manifest(man)
                return False, f"rsync spawn failed: {e}"

            if res.returncode != 0:
                self._failures += 1
                err = (res.stderr or "").strip().splitlines()
                man["last_error"] = err[-1] if err else f"rsync exit {res.returncode}"
                self._save_manifest(man)
                return False, man["last_error"]

            # success
            self._failures = 0
            man["last_success"] = now_iso()
            man["last_error"] = None

            confirmed = self._confirm_synced()
            self._update_files(man, confirmed)
            self._save_manifest(man)
            moved = self._move_sent(man, confirmed)
            if moved:
                self._save_manifest(man)
            return True, f"synced ok; confirmed={len(confirmed)} moved={moved}"

    def _confirm_synced(self) -> set[str]:
        """Files whose remote copy matches local. If verify is off, trust the transfer
        (all local files). If on, run a dry-run itemize; files NOT listed are in sync."""
        local = set(self._closed_local_files())
        if not self.cfg.get("verify"):
            return local
        try:
            res = self._run_rsync(["-n", "-i", "--out-format=%n"])
        except (subprocess.SubprocessError, OSError):
            return set()  # can't confirm -> confirm nothing (conservative)
        if res.returncode != 0:
            return set()
        differing = set()
        for ln in (res.stdout or "").splitlines():
            ln = ln.strip().rstrip("/")
            if ln and ln != ".":
                differing.add(os.path.basename(ln))
        return {f for f in local if f not in differing}

    def _update_files(self, man: dict, confirmed: set[str]):
        files = man.setdefault("files", {})
        for name in self._closed_local_files():
            p = self.log_dir / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rec = files.setdefault(name, {})
            rec["size"] = size
            if name in confirmed:
                rec["status"] = "sent" if self._is_closed(name) else "synced-open"
                rec["confirmed_at"] = now_iso()
            else:
                rec.setdefault("status", "pending")

    def _move_sent(self, man: dict, confirmed: set[str]) -> int:
        """Move confirmed + closed (past-day) files into sent/. Returns count moved."""
        if not self.cfg.get("move_sent"):
            return 0
        moved = 0
        self.sent_dir.mkdir(parents=True, exist_ok=True)
        files = man.setdefault("files", {})
        for name in list(confirmed):
            if not self._is_closed(name):
                continue
            src = self.log_dir / name
            if not src.is_file():
                continue
            try:
                shutil.move(str(src), str(self.sent_dir / name))
            except OSError as e:
                logmsg(f"could not move {name} to sent/: {e}")
                continue
            rec = files.setdefault(name, {})
            rec["status"] = "sent"
            rec["moved"] = True
            rec["moved_at"] = now_iso()
            moved += 1
        return moved

    def backoff_seconds(self) -> float:
        base = float(self.cfg["interval"])
        if self._failures == 0:
            return base
        return min(base * (2 ** min(self._failures, 8)), float(self.cfg["max_backoff"]))


# --------------------------------------------------------------------------- #
# Daemon orchestration
# --------------------------------------------------------------------------- #

class Daemon:
    def __init__(self, cfg: dict, config_source: str | None):
        self.cfg = cfg
        self.config_source = config_source
        g = cfg["general"]
        self.hostname = (g.get("hostname") or socket.gethostname()).strip()
        self.hostname = re.sub(r"[^A-Za-z0-9_.-]", "_", self.hostname) or "unknown-host"
        self.log_dir = Path(g["log_dir"]).expanduser()
        self.writer = LogWriter(self.log_dir, self.hostname)
        self.stop_event = threading.Event()

        # capture state
        c = cfg["capture"]
        self.heartbeat_interval = float(c["heartbeat_interval"])
        self.title_debounce = float(c["title_debounce"])
        self.max_title = int(c["max_title_length"])
        self.fields = list(c["fields"])
        self.ignore_app_ids = set(c["ignore_app_ids"])
        self.redact = [re.compile(p) for p in c["redact_title_patterns"]]
        self.log_no_focus = bool(c["log_no_focus"])

        self._state_lock = threading.Lock()
        self._current = WindowSnapshot()
        self._last_logged = None            # identity+title tuple last written
        self._last_window_write = 0.0
        self._title_pending_since = None

        self.window_source = select_window_source()
        self.power_source = select_power_source(cfg["power"])
        self.uploader = Uploader(cfg["upload"], self.log_dir, self.hostname)

        p = cfg["processes"]
        self.proc_sampler = ProcSampler(
            top_n=p["top_n"], sample_window=p["sample_window"],
            normalize=p["normalize"], include_cmdline=p["include_cmdline"],
            cmdline_max_length=p["cmdline_max_length"])
        self._threads: list[threading.Thread] = []

    # ---- WINDOW writing (R1) ----
    def _format_window(self, snap: WindowSnapshot) -> list:
        title = snap.title
        for rx in self.redact:
            title = rx.sub("[REDACTED]", title)
        if len(title) > self.max_title:
            title = title[: self.max_title] + "…"
        vals = {"app_id": snap.app_id or "(none)", "pid": snap.pid,
                "title": title, "wid": snap.wid}
        return [vals.get(f, "") for f in self.fields]

    def _write_window(self, snap: WindowSnapshot, now: float):
        self.writer.write("WINDOW", self._format_window(snap))
        self._last_logged = (snap.identity(), snap.title)
        self._last_window_write = now
        self._title_pending_since = None

    def _on_window_update(self, snap: WindowSnapshot):
        now = time.monotonic()
        with self._state_lock:
            self._current = snap
            self._evaluate(now, from_event=True)

    def _evaluate(self, now: float, from_event: bool):
        """Decide whether to write a WINDOW line. Caller holds _state_lock."""
        snap = self._current
        if snap.is_none():
            if not self.log_no_focus:
                return
        elif snap.app_id in self.ignore_app_ids:
            return

        cur_key = (snap.identity(), snap.title)
        if self._last_logged is None:
            self._write_window(snap, now)
            return

        last_identity, last_title = self._last_logged
        if snap.identity() != last_identity:
            # real window switch -> log immediately
            self._write_window(snap, now)
            return

        if snap.title != last_title:
            # title-only change -> debounce (niri titles carry live spinners)
            if self._title_pending_since is None:
                self._title_pending_since = now
            if now - self._title_pending_since >= self.title_debounce:
                self._write_window(snap, now)
            return

        # unchanged -> heartbeat only
        if now - self._last_window_write >= self.heartbeat_interval:
            self._write_window(snap, now)

    def _ticker(self):
        """Drives title-debounce flushes and heartbeats even without new events."""
        while not self.stop_event.wait(1.0):
            now = time.monotonic()
            with self._state_lock:
                self._evaluate(now, from_event=False)

    # ---- POWER writing (R6) ----
    def _on_power_event(self, event: str, detail: str):
        kind = "STATUS" if event == "problem" else "POWER"
        self.writer.write(kind, [event, detail])

    def _on_window_backend_status(self, up: bool, detail: str):
        if up:
            self.writer.write("STATUS", ["window-source-recovered", detail])
        else:
            self.writer.write("STATUS", ["window-source-unavailable", detail])

    def _startup_power(self):
        # Classify how the PREVIOUS session ended, BEFORE writing anything this run.
        # prior: None (detection off), "none" (no prior log), "clean", or "unclean".
        prior = None
        last_ts = None
        last_event = None
        if self.cfg["power"].get("detect_unclean_shutdown", True):
            last = self.writer.last_line_across_logs()
            if last is None:
                prior = "none"          # no prior log at all -> fresh client / first run
            else:
                cols = last.split("\t")
                if len(cols) >= 2:
                    last_ts = cols[0]
                    typ = cols[1]
                    if typ in ("POWER", "STATUS") and len(cols) >= 3:
                        last_event = f"{typ}:{cols[2]}"
                    else:
                        last_event = typ
                    clean = (typ == "POWER" and len(cols) >= 3
                             and cols[2] in ("offline", "shutdown", "suspend"))
                    prior = "clean" if clean else "unclean"
                else:
                    last_event = "unparseable"
                    prior = "unclean"

        boot = boot_time_iso()
        detail = f"boot={boot}" if boot else "boot=?"
        if prior is not None:
            detail += f" prior={prior}"
        self.writer.write("POWER", ["online", detail])

        if prior == "unclean":
            self.writer.write(
                "STATUS",
                ["previous-session-unclean", f"last={last_ts} last_event={last_event}"])

    # ---- process sampling loop (R10) ----
    def _process_loop(self):
        if not self.cfg["processes"].get("enabled", True):
            return
        interval = float(self.cfg["processes"]["interval"])
        # first sample soon after startup, then every `interval`
        while not self.stop_event.is_set():
            try:
                tokens = self.proc_sampler.sample()
                if tokens:
                    self.writer.write("PROCS", [" ".join(tokens)])
            except Exception as e:  # noqa: BLE001 - keep daemon alive
                logmsg(f"process sample failed: {e}")
            # sample_window already consumed some time inside sample()
            if self.stop_event.wait(max(0.0, interval - self.proc_sampler.sample_window)):
                break

    # ---- upload loop (R5/R7) ----
    def _upload_loop(self):
        if not self.cfg["upload"].get("enabled"):
            return
        # small initial delay so startup events flush first
        if self.stop_event.wait(5):
            return
        while not self.stop_event.is_set():
            ok, msg = self.uploader.upload_once()
            logmsg(f"upload: {msg}")
            wait_s = self.uploader.backoff_seconds()
            if self.stop_event.wait(wait_s):
                break

    # ---- lifecycle ----
    def _install_signals(self):
        def handler(signum, _frame):
            logmsg(f"received signal {signum}; shutting down")
            self.stop_event.set()
        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, handler)

    def run(self):
        self._install_signals()
        logmsg(f"starting; host={self.hostname} log_dir={self.log_dir} "
               f"config={self.config_source or '(defaults)'}")
        if self.cfg["power"].get("enabled", True):
            self._startup_power()

        # wire callbacks
        self.window_source.on_update = self._on_window_update
        if isinstance(self.window_source, NiriWindowSource):
            self.window_source.on_backend_status = self._on_window_backend_status
        self.power_source.on_event = self._on_power_event

        # seed current focus so the first heartbeat has data promptly
        try:
            self._current = self.window_source.current()
        except Exception as e:  # noqa: BLE001 - startup best effort
            logmsg(f"initial focus query failed: {e}")

        self._spawn(self.window_source.start, "window-source")
        if self.cfg["power"].get("enabled", True):
            self._spawn(self.power_source.start, "power-source")
        self._spawn(self._ticker, "ticker", pass_stop=False)
        if self.cfg["processes"].get("enabled", True):
            self._spawn(self._process_loop, "processes", pass_stop=False)
        self._spawn(self._upload_loop, "upload", pass_stop=False)

        # wait until stopped
        while not self.stop_event.wait(0.5):
            pass

        # clean offline marker
        if self.cfg["power"].get("enabled", True):
            self.writer.write("POWER", ["offline", "reason=signal"])
        for t in self._threads:
            t.join(timeout=4)
        self.writer.close()
        logmsg("stopped")

    def _spawn(self, target, name, pass_stop=True):
        def wrapper():
            try:
                if pass_stop:
                    target(self.stop_event)
                else:
                    target()
            except Exception as e:  # noqa: BLE001 - keep daemon alive
                logmsg(f"thread {name} crashed: {e}")
        t = threading.Thread(target=wrapper, name=name, daemon=True)
        t.start()
        self._threads.append(t)


# --------------------------------------------------------------------------- #
# Backend selection (R9)
# --------------------------------------------------------------------------- #

def select_window_source() -> WindowSource:
    system = _platform.system()
    if system == "Linux":
        if os.environ.get("NIRI_SOCKET") or shutil.which("niri"):
            return NiriWindowSource()
        die("no supported Linux window backend found (expected niri). "
            "Hyprland/sway/X11 backends are future work.")
    # elif system == "Darwin": return MacWindowSource()  # future (Quartz)
    die(f"unsupported platform for window capture: {system}")


def select_power_source(power_cfg: dict) -> PowerSource:
    system = _platform.system()
    if system == "Linux":
        return LogindPowerSource(
            log_suspend_resume=power_cfg.get("log_suspend_resume", True),
            log_shutdown=power_cfg.get("log_shutdown", True),
            log_lock_unlock=power_cfg.get("log_lock_unlock", False))
    # elif system == "Darwin": return MacPowerSource()  # future (IOKit/pmset)
    return LogindPowerSource()  # best effort


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #

def cmd_run(cfg, source, args):
    Daemon(cfg, source).run()
    return 0


def cmd_snapshot(cfg, source, args):
    # Print-only by default so this debugging command never pollutes the audit log
    # (a lone appended line would look like an unclean session on the next start).
    d = Daemon(cfg, source)
    snap = d.window_source.current()
    fields = d._format_window(snap)
    line = "\t".join([now_iso(), "WINDOW"] + [sanitize_field(x) for x in fields])
    print(line)
    if getattr(args, "append", False):
        d.writer.write("WINDOW", fields)
        print("(appended to audit log)", file=sys.stderr)
    d.writer.close()
    return 0


def cmd_upload(cfg, source, args):
    d = Daemon(cfg, source)
    if not cfg["upload"].get("enabled"):
        print("upload is disabled in config ([upload] enabled = false)")
        return 1
    ok, msg = d.uploader.upload_once()
    print(f"upload {'ok' if ok else 'FAILED'}: {msg}")
    return 0 if ok else 2


def cmd_status(cfg, source, args):
    d = Daemon(cfg, source)
    print(f"host:        {d.hostname}")
    print(f"config:      {source or '(defaults)'}")
    print(f"log_dir:     {d.log_dir}")
    print(f"platform:    {_platform.system()} / window={type(d.window_source).__name__}"
          f" power={type(d.power_source).__name__}")
    up = cfg["upload"]
    print(f"upload:      enabled={up.get('enabled')} dest={up.get('destination') or '-'}"
          f" interval={up.get('interval')}s")
    man = d.uploader._load_manifest()
    print(f"last_success:{man.get('last_success')}  last_error={man.get('last_error')}")
    files = man.get("files", {})
    logs = sorted(p.name for p in d.log_dir.glob(f"{d.hostname}-*.log") if p.is_file())
    sent = sorted(p.name for p in (d.log_dir / 'sent').glob('*.log')) \
        if (d.log_dir / 'sent').is_dir() else []
    print(f"\nlocal log files ({len(logs)} pending, {len(sent)} sent):")
    for name in logs:
        st = files.get(name, {}).get("status", "pending")
        size = (d.log_dir / name).stat().st_size
        print(f"  [{st:<12}] {name}  ({size} bytes)")
    for name in sent:
        print(f"  [sent (moved)] sent/{name}")
    return 0


COMMANDS = {
    "run": cmd_run,
    "snapshot": cmd_snapshot,
    "upload": cmd_upload,
    "status": cmd_status,
}


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_NAME, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="run", choices=list(COMMANDS))
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("--append", action="store_true",
                        help="(snapshot) also append the line to the audit log")
    args = parser.parse_args(argv)
    cfg, source = load_config(args.config)
    return COMMANDS[args.command](cfg, source, args)


if __name__ == "__main__":
    sys.exit(main())
