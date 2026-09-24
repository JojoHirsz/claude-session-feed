"""Reads Codex CLI's on-disk session state (~/.codex) into the pieces monitor.py
needs to add a Codex session as another Session/Block in the same feed.

Everything below was checked empirically against Codex 0.156.1 on this machine on
2026-09-24 (a live "codex-tui" process, and a freshly spawned throwaway one) before
being written -- see the report handed back alongside this change for the full trail.
Like monitor.py's own header says: this format is undocumented and internal to
Codex CLI, not a stable contract, so parsing here is defensive on purpose.

Data sources that turned out to work:
  ~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<local-ts>-<conversationId>.jsonl
        The timestamp LOOKS like UTC (ISO-8601-shaped) but isn't -- confirmed
        empirically: GetProcessTimes gave 08:32:03 UTC for a live process whose
        rollout filename read "T10-32-06", a 2-hour gap matching this machine's
        UTC+2 (CEST) offset exactly. It's local wall-clock time; see _rollout_epoch.
        One JSON object per line. Line 1 is always
        {"type":"session_meta","payload":{"session_id","cwd",
        "originator":"codex-tui" for an interactive session, "cli_version",...}}.
        Turn/activity events are {"type":"event_msg","payload":{"type":
        "task_started"|"item_completed"|"token_count"|"task_complete",...}}.
  ~/.codex/session_index.jsonl
        Flat, append-only, ACROSS ALL sessions on the machine:
        {"id":<conversationId>,"thread_name":<AI-generated title>,"updated_at":...}.
        The same id can repeat as the title gets refined; last line wins.

Data sources that looked promising but were ruled out empirically:
  ~/.codex/thread-writer-locks/<conversationId>.lock looked like it might be an OS
        lock, but a plain `Remove-Item` deleted it while the owning process was
        confirmed alive (Get-Process succeeded on its pid), and it was NOT
        recreated afterwards. It's a marker file held only briefly around specific
        write operations, not for a session's lifetime -- absent is the NORMAL
        state for a live, idle session, so its presence/absence is not a liveness
        signal.
  ~/.codex/logs_2.sqlite(-wal), state_5.sqlite, thread_history_1.sqlite are shared,
        machine-wide files across every Codex conversation, not one per session --
        fresh mtime says "some Codex session did something recently" and can never
        be attributed to one particular conversationId without parsing row content.
  ~/.codex/process_manager/chat_processes.json logs shell/tool subprocesses Codex
        itself launches mid-turn (has cwd/conversationId), but its osPid/processId
        fields were observed null on every sampled entry, live ones included --
        structurally unusable as a liveness signal on this machine/version.

The one thing that IS reliable is the live OS process itself: a real interactive
session is a `codex.exe` process whose full image path sits OUTSIDE ~/.codex (the
real npm-installed CLI lives under .../npm/node_modules/@openai/codex/...). Two
OTHER codex.exe binaries exist ON DISK under ~/.codex itself
(.sandbox-bin/codex.exe and plugins/.plugin-appserver/codex.exe) -- confirmed those
are internal sandbox/plugin-host helpers, not user sessions -- and the path check
excludes them for free, no name/cmdline heuristics needed.

Confirmed separately (see the report): Codex never calls the Win32
SetConsoleTitleW API. That is NOT the same as "no window title" though --
correction made after live testing against a real Git-Bash/mintty session on
2026-09-24: Codex sets its window title itself via an ANSI OSC escape written
over the pty, as "<thread name> | <folder>" (a session labeled "Plane morgen"
showed up as a window titled "Plane morgen | Projects"). __main__.py's
jump-to-window therefore tries a title match on the session's label first, same
as Claude Code, and only falls back to the process-ancestry walk
(_jump_to_process_tree) when there's no label yet or no matching window is
found. Only confirmed under mintty so far -- not re-tested under a native
Windows console (cmd/PowerShell/Windows Terminal), which may render the OSC
escape differently or not at all.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

CODEX_DIR = Path(os.environ.get("CLAUDE_SESSION_FEED_CODEX_DEMO_DIR") or (Path.home() / ".codex"))

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_FILETIME_EPOCH_OFFSET_S = 11_644_473_600  # 1601-01-01 -> 1970-01-01, in seconds


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260),
    ]


def _codex_exe_pids() -> list:
    """PIDs of every running process literally named codex.exe, whatever binary
    that is (Toolhelp32 snapshot; szExeFile is a base filename, not a full path)."""
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot:
        return []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        out = []
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return out
        while True:
            if entry.szExeFile.lower() == "codex.exe":
                out.append(entry.th32ProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return out
    finally:
        kernel32.CloseHandle(snapshot)


def _process_image_path(pid: int) -> str:
    """Full path to a process's executable, or "" if it can't be opened/queried
    (e.g. already exited, or a privileged process)."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(4096)
        size = ctypes.c_uint32(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def _process_start_epoch(pid: int) -> Optional[float]:
    """Process creation time as a Unix epoch, or None if unavailable.

    GetProcessTimes writes a FILETIME (100ns ticks since 1601-01-01) into each of
    the 8-byte out buffers; reading it back as a plain uint64 is the standard,
    correct trick (a FILETIME's two DWORDs occupy the same 8 bytes, low part
    first, as a little-endian uint64 on x86/x64).
    """
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exit_t, kernel_t, user_t = (ctypes.c_uint64() for _ in range(4))
        ok = kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                       ctypes.byref(kernel_t), ctypes.byref(user_t))
        if not ok or not creation.value:
            return None
        return creation.value / 10_000_000 - _FILETIME_EPOCH_OFFSET_S
    finally:
        kernel32.CloseHandle(handle)


def discover_live_pids() -> dict:
    """pid -> start epoch for every REAL interactive codex CLI process on this
    machine right now (filtered by executable path, see module docstring)."""
    codex_dir_str = str(CODEX_DIR).lower()
    out = {}
    for pid in _codex_exe_pids():
        path = _process_image_path(pid)
        if not path or path.lower().startswith(codex_dir_str):
            continue  # internal sandbox/plugin-host helper under ~/.codex, not a user session
        start = _process_start_epoch(pid)
        if start is not None:
            out[pid] = start
    return out


_ROLLOUT_RE = re.compile(
    r"^rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-[0-9a-f-]+\.jsonl$"
)


def _rollout_epoch(path: Path) -> Optional[float]:
    """The start time encoded in a rollout filename, or None if it doesn't match.

    The "T10-32-06" in the filename looks UTC (ISO-8601-shaped) but isn't -- confirmed
    empirically against a real live session: GetProcessTimes gave 08:32:03 UTC for the
    process, while its rollout filename read "T10-32-06", a 2-hour gap matching exactly
    this machine's local UTC+2 (CEST) offset. So this is local wall-clock time, and must
    be converted with mktime (which interprets a naive struct as local time), not treated
    as UTC.
    """
    m = _ROLLOUT_RE.match(path.name)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


def find_rollout(pid_start_epoch: float, tolerance_s: float = 30.0) -> Optional[Path]:
    """The rollout file a codex.exe process created at startup, matched by how
    close its filename timestamp is to the process's own creation time (both are
    set within a few seconds of each other in practice -- confirmed empirically:
    process start and rollout-file creation landed 4 seconds apart). The
    sessions/<yyyy>/<mm>/<dd> folder split is local-calendar-date too, matching the
    filename's local timestamp (see _rollout_epoch)."""
    dt = datetime.fromtimestamp(pid_start_epoch)  # local, matching the filename convention
    day_dir = CODEX_DIR / "sessions" / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
    if not day_dir.is_dir():
        return None
    best, best_diff = None, tolerance_s
    for f in day_dir.glob("rollout-*.jsonl"):
        ts = _rollout_epoch(f)
        if ts is None:
            continue
        diff = abs(ts - pid_start_epoch)
        if diff <= best_diff:
            best, best_diff = f, diff
    return best


def read_session_meta(rollout_path: Path) -> dict:
    """The first line of a rollout file: {"session_id", "cwd", "originator"}."""
    try:
        with open(rollout_path, "r", encoding="utf-8") as fh:
            first = fh.readline()
        obj = json.loads(first)
    except (OSError, ValueError):
        return {}
    if obj.get("type") != "session_meta":
        return {}
    payload = obj.get("payload") or {}
    return {"session_id": payload.get("session_id", ""), "cwd": payload.get("cwd", ""),
            "originator": payload.get("originator", "")}


def read_thread_name(session_id: str) -> str:
    """Scans session_index.jsonl for the most recent AI-generated title for this
    session_id (the same id can appear more than once as the title gets refined;
    last line wins, mirroring how Claude Code's own ai-title event just overwrites
    session.label whenever a new one arrives)."""
    if not session_id:
        return ""
    path = CODEX_DIR / "session_index.jsonl"
    name = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("id") == session_id and obj.get("thread_name"):
                    name = obj["thread_name"]
    except OSError:
        pass
    return name


@dataclass
class CodexSessionInfo:
    pid: int
    started_at: float
    session_id: str = ""
    cwd: str = ""
    label: str = ""
    rollout_path: Optional[Path] = None


def discover_codex_sessions() -> list:
    """One CodexSessionInfo per live, real interactive `codex` CLI process found
    right now. A rollout whose originator isn't "codex-tui" (e.g. a `codex exec`
    batch run, if one is ever caught mid-flight) is skipped -- this only wants to
    surface interactive sessions, mirroring the task's own scope for Claude Code."""
    out = []
    for pid, started in discover_live_pids().items():
        rollout = find_rollout(started)
        meta = read_session_meta(rollout) if rollout else {}
        if meta and meta.get("originator") not in ("", "codex-tui"):
            continue
        out.append(CodexSessionInfo(
            pid=pid, started_at=started, session_id=meta.get("session_id", ""),
            cwd=meta.get("cwd", ""), label=read_thread_name(meta.get("session_id", "")),
            rollout_path=rollout,
        ))
    return out
