"""Reads Claude Code's on-disk session state (~/.claude) into a live feed of blocks.

Data sources (undocumented, internal to Claude Code, may change between versions):
  ~/.claude/sessions/<pid>.json                       live registry of running processes
  ~/.claude/projects/<cwd-encoded>/<sessionId>.jsonl   main transcript, one JSON object per line
  .../<sessionId>/subagents/agent-<agentId>.jsonl      one file per spawned subagent
  .../subagents/agent-<agentId>.meta.json              subagent metadata (agentType, toolUseId, ...)

Everything here parses defensively (dict.get everywhere, unknown types counted not raised)
because the format is not a stable contract.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CLAUDE_DIR = Path.home() / ".claude"
POLL_MS = 700
DISCOVERY_EVERY_N_TICKS = 3
TAIL_BYTES = 2_000_000
MAX_LINE_BYTES = 8_000_000
MAX_BLOCKS = 1500
BODY_MAX = 400
SHOW_THINKING = False
SUBAGENT_STALE_S = 120
MAX_SUBAGENTS_PER_SESSION = 5
SUBAGENT_DONE_KEEP_S = 600
ENDED_SESSION_KEEP_DAYS = 7

_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ClaudeSessionFeed"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=_LOG_DIR / "feed.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("claude_session_feed")

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def pid_alive(pid: int) -> bool:
    """Windows-safe liveness check.

    Never use os.kill(pid, 0) here: on Windows that calls TerminateProcess
    and would kill the Claude Code session instead of just checking it.
    """
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_registry() -> dict:
    """Live sessions from ~/.claude/sessions/<pid>.json, filtered to processes that are actually alive."""
    out = {}
    sessions_dir = CLAUDE_DIR / "sessions"
    if not sessions_dir.is_dir():
        return out
    for f in sessions_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        session_id = data.get("sessionId")
        if not pid or not session_id:
            continue
        if not pid_alive(pid):
            continue
        out[session_id] = data
    return out


_SESSION_ID_RE = re.compile(r"^[\w-]+$")


def find_transcript(session_id: str) -> Optional[Path]:
    if not _SESSION_ID_RE.match(session_id):
        return None
    return next(CLAUDE_DIR.glob(f"projects/*/{session_id}.jsonl"), None)


class Tailer:
    """Reads only the bytes appended since the last call. Tolerates an unfinished last line."""

    def __init__(self, path: Path, tail_bytes: int = 0):
        self.path = path
        self.pos = 0
        self.buf = b""
        self.missing = False
        self._discard_first = False
        if tail_bytes:
            try:
                size = path.stat().st_size
                if size > tail_bytes:
                    self.pos = size - tail_bytes
                    self._discard_first = True
            except OSError:
                pass

    def read_new(self) -> list:
        try:
            st = self.path.stat()
        except OSError:
            self.missing = True
            return []
        self.missing = False
        if st.st_size < self.pos:
            log.warning("File shrank, resetting tail position: %s", self.path)
            self.pos = 0
            self.buf = b""
            self._discard_first = False
        if st.st_size == self.pos:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read(st.st_size - self.pos)
        except OSError:
            return []
        self.pos = st.st_size
        data = self.buf + chunk
        lines = data.split(b"\n")
        self.buf = lines.pop()
        if self._discard_first and lines:
            lines.pop(0)
            self._discard_first = False
        objs = []
        for line in lines:
            if not line:
                continue
            if len(line) > MAX_LINE_BYTES:
                log.warning("Line too large (%d bytes), skipped: %s", len(line), self.path.name)
                continue
            try:
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                log.warning("JSON error in %s: %.80r", self.path.name, line[:80])
                continue
            if isinstance(obj, dict):
                objs.append(obj)
        return objs


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso(epoch_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_s))


@dataclass
class Block:
    id: str
    seq: int
    created_seq: int
    ts: str
    session: str
    kind: str
    title: str
    body: str = ""
    state: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "seq": self.seq, "ts": self.ts, "session": self.session,
            "kind": self.kind, "title": self.title, "body": self.body,
            "state": self.state, "meta": self.meta,
        }


@dataclass
class Subagent:
    agent_id: str
    tool_use_id: str = ""
    description: str = ""
    agent_type: str = ""
    model: str = ""
    tailer: Optional[Tailer] = None
    block_id: str = ""
    state: str = "running"
    last_activity: str = ""
    last_ts: float = 0.0


@dataclass
class Session:
    id: str
    pid: int = 0
    cwd: str = ""
    label: str = ""
    model: str = ""
    model_name: str = ""
    tokens_left: Optional[int] = None
    context_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    perm_mode: str = ""
    status: str = "busy"
    started_at: float = 0.0
    last_ts: float = 0.0
    turn_open: bool = False
    current_prompt: str = ""
    color_index: int = 0
    tailer: Optional[Tailer] = None
    pending_tools: dict = field(default_factory=dict)
    subagents: dict = field(default_factory=dict)
    subagents_by_tool: dict = field(default_factory=dict)
    unknown_types: Counter = field(default_factory=Counter)
    seen_notifications: set = field(default_factory=set)
    done_agent_ids: set = field(default_factory=set)
    pruned_agent_ids: set = field(default_factory=set)
    ended_at: Optional[float] = None
    activity: str = "Started"
    activity_state: str = "active"  # active | waiting | error | idle
    activity_ts: float = 0.0

    def category(self) -> str:
        """Coarse bucket the UI filters by.

        'inactive' is reserved for the 7-day archive (process gone). A session whose
        process is alive but whose turn has finished is always, definitionally, waiting
        on the human — Claude Code never does anything between turns on its own, and a
        turn frequently ends with a plain-text question that isn't a structured
        AskUserQuestion tool call, so there's no reliable signal to tell "finished, nothing
        needed" apart from "finished, asked something" in the transcript. Treating both as
        'waiting' matches what the user actually needs to know: is anyone waiting on me.
        """
        if self.status == "ended":
            return "inactive"
        if self.activity_state == "error":
            return "error"
        # Claude Code's own registry entry (busy/idle) is authoritative for whether a turn
        # is actually running; it catches cases our transcript-event guess gets wrong (see
        # activity_state below), except an explicit AskUserQuestion ("waiting"), which
        # should show even if the process still reports busy while blocked on it.
        if self.activity_state == "waiting" or self.status == "idle":
            return "waiting"
        if self.activity_state == "active":
            return "active"
        return "waiting"

    def header(self) -> dict:
        return {
            "id": self.id, "pid": self.pid, "label": self.label or self.id[:8], "cwd": self.cwd,
            "model": self.model, "model_name": self.model_name,
            "tokens_left": self.tokens_left, "context_tokens": self.context_tokens,
            "cost_usd": self.cost_usd, "perm_mode": self.perm_mode,
            "status": self.status, "color_index": self.color_index,
            "activity": self.activity, "activity_state": self.activity_state,
            "activity_ts": _iso(self.activity_ts) if self.activity_ts else None,
            "category": self.category(),
            "started_at": _iso(self.started_at) if self.started_at else None,
            "subagents": [
                {
                    "id": s.agent_id, "description": s.description or s.agent_type,
                    "state": s.state, "last_activity": s.last_activity,
                }
                for s in sorted(self.subagents.values(), key=lambda s: s.last_ts, reverse=True)
            ],
            "unknown": sum(self.unknown_types.values()),
        }


_TASK_NOTIF_RE = re.compile(
    r"<task-id>(?P<task_id>\w+)</task-id>\s*"
    r"(?:<tool-use-id>(?P<tool_use_id>[\w-]+)</tool-use-id>)?"
    r".*?<status>(?P<status>\w+)</status>"
    r".*?<summary>(?P<summary>.*?)</summary>",
    re.DOTALL,
)
_TASK_NUM_RE = {
    "tokens": re.compile(r"<subagent_tokens>(\d+)</subagent_tokens>"),
    "tool_uses": re.compile(r"<tool_uses>(\d+)</tool_uses>"),
    "duration_ms": re.compile(r"<duration_ms>(\d+)</duration_ms>"),
}
_HANDBACK_RE = re.compile(r'<agent-message from="(\w+)">', re.IGNORECASE)
_TOTAL_TOKENS_RE = re.compile(r"<total_tokens>(\d+)")


def parse_task_notification(text: str) -> dict:
    m = _TASK_NOTIF_RE.search(text)
    out: dict = {
        "task_id": m.group("task_id") if m else None,
        "tool_use_id": m.group("tool_use_id") if m else None,
        "status": m.group("status") if m else None,
        "summary": m.group("summary") if m else None,
        "tokens": None, "tool_uses": None, "duration_ms": None,
    }
    for key, pattern in _TASK_NUM_RE.items():
        m2 = pattern.search(text)
        if m2:
            out[key] = int(m2.group(1))
    return out


def summarize_tool(name: str, tool_input: dict):
    tool_input = tool_input or {}
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else name
        tool = parts[2] if len(parts) > 2 else ""
        return f"{server} · {tool}", ""
    if name in ("Bash", "PowerShell"):
        body = tool_input.get("description") or (tool_input.get("command") or "")[:120]
        return name, body
    if name in ("Read", "Edit", "Write"):
        path = tool_input.get("file_path", "")
        return name, Path(path).name if path else ""
    if name in ("Grep", "Glob"):
        return name, tool_input.get("pattern", "")
    if name in ("WebSearch", "ToolSearch"):
        return name, tool_input.get("query", "")
    if name == "WebFetch":
        return name, tool_input.get("url", "")
    if name == "Skill":
        return name, tool_input.get("skill", "")
    if name == "SendMessage":
        return name, f"to {tool_input.get('to', '?')}: {tool_input.get('summary', '')}"
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return name, v[:80]
    return name, ""


class Monitor:
    """Owns all session/subagent state and the block feed. One instance, one lock."""

    def __init__(self):
        self.sessions: dict = {}
        self.blocks: "OrderedDict[str, Block]" = OrderedDict()
        self.seq = 0
        self.lock = threading.Lock()
        self._tick_count = 0
        self._next_color = 0

    def tick(self) -> None:
        with self.lock:
            self._tick_count += 1
            if self._tick_count % DISCOVERY_EVERY_N_TICKS == 1:
                self._discover()
            for session in list(self.sessions.values()):
                if session.tailer is not None:
                    for obj in session.tailer.read_new():
                        try:
                            self._classify(session, obj)
                        except Exception:
                            log.exception("classify failed for session %s", session.id)
                for sub in list(session.subagents.values()):
                    if sub.tailer is None:
                        continue
                    for obj in sub.tailer.read_new():
                        try:
                            self._classify_sub(session, sub, obj)
                        except Exception:
                            log.exception("classify_sub failed for %s/%s", session.id, sub.agent_id)
                self._prune_subagents(session)
            self._mark_stale()

    _CATEGORY_PRIORITY = {"active": 0, "error": 1, "waiting": 2, "inactive": 3}

    def drain(self, after_seq: int) -> dict:
        with self.lock:
            new, updated = [], []
            for block in self.blocks.values():
                if block.seq <= after_seq:
                    continue
                (new if block.created_seq > after_seq else updated).append(block.to_dict())
            sessions = sorted(
                self.sessions.values(),
                key=lambda s: (self._CATEGORY_PRIORITY.get(s.category(), 9), -s.activity_ts),
            )
            return {
                "seq": self.seq,
                "sessions": [s.header() for s in sessions],
                "new": new,
                "updated": updated,
            }

    # -- internals --------------------------------------------------------

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _add_block(self, session_id, kind, title, body="", state="", meta=None) -> Block:
        seq = self._next_seq()
        bid = f"b{seq}"
        block = Block(id=bid, seq=seq, created_seq=seq, ts=_now_iso(), session=session_id,
                       kind=kind, title=title, body=(body or "")[:BODY_MAX], state=state, meta=meta or {})
        self.blocks[bid] = block
        while len(self.blocks) > MAX_BLOCKS:
            self.blocks.popitem(last=False)
        self._touch_session_activity(session_id, kind, title, block.body, state)
        return block

    def _update_block(self, bid, **changes) -> Optional[Block]:
        block = self.blocks.get(bid)
        if block is None:
            return None
        for k, v in changes.items():
            if v is None:
                continue
            if k == "body":
                v = v[:BODY_MAX]
            if k == "meta":
                if v:
                    block.meta.update(v)
                continue
            setattr(block, k, v)
        block.seq = self._next_seq()
        self._touch_session_activity(block.session, block.kind, block.title, block.body, block.state)
        return block

    def _touch_session_activity(self, session_id: str, kind: str, title: str, body: str, state: str) -> None:
        """Rolls every block create/update into the session's one-line 'what it's doing' summary.

        This is what lets the UI show one card per session instead of one per event.
        """
        session = self.sessions.get(session_id)
        if session is None or kind in ("session_start", "session_end"):
            return
        text = f"{title} — {body}" if body else title
        session.activity_ts = time.time()

        if kind == "question":
            session.activity_state = "waiting" if state == "waiting" else "active"
            session.activity = text[:140]
        elif kind == "error" or state in ("error", "denied"):
            session.activity_state = "error"
            session.activity = text[:140]
        elif kind == "interrupt":
            session.activity_state = "idle"
            session.activity = text[:140]
        elif kind == "turn_end":
            if session.activity_state not in ("waiting", "error"):
                session.activity_state = "idle"
        else:
            if session.activity_state != "waiting":
                session.activity_state = "active"
            session.activity = text[:140]

    def _discover(self) -> None:
        registry = read_registry()
        now = time.time()
        for session_id, entry in registry.items():
            session = self.sessions.get(session_id)
            if session is None:
                session = Session(id=session_id, cwd=entry.get("cwd", ""),
                                   started_at=(entry.get("startedAt") or 0) / 1000,
                                   color_index=self._next_color, activity_ts=now)
                self._next_color += 1
                self.sessions[session_id] = session
                transcript = find_transcript(session_id)
                if transcript:
                    session.tailer = Tailer(transcript, tail_bytes=TAIL_BYTES)
                self._add_block(session_id, "session_start", "Session started", session.cwd)
            elif session.tailer is None:
                transcript = find_transcript(session_id)
                if transcript:
                    session.tailer = Tailer(transcript, tail_bytes=TAIL_BYTES)
            reg_status = entry.get("status")
            if reg_status:
                session.status = reg_status
            session.pid = entry.get("pid") or session.pid
            self._scan_subagents(session)

        for session_id, session in list(self.sessions.items()):
            if session_id in registry:
                continue
            if session.status != "ended":
                session.status = "ended"
                session.ended_at = now
                self._add_block(session_id, "session_end", "Session ended")
            elif session.ended_at and now - session.ended_at > ENDED_SESSION_KEEP_DAYS * 86400:
                del self.sessions[session_id]

    def _scan_subagents(self, session: Session) -> None:
        if session.tailer is None:
            return
        agents_dir = session.tailer.path.parent / session.id / "subagents"
        if not agents_dir.is_dir():
            return
        for f in agents_dir.glob("agent-*.jsonl"):
            agent_id = f.stem[len("agent-"):]
            if agent_id in session.subagents or agent_id in session.pruned_agent_ids:
                continue
            meta_path = f.with_suffix("").with_suffix(".meta.json")
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}
            sub = Subagent(
                agent_id=agent_id,
                tool_use_id=meta.get("toolUseId", ""),
                description=meta.get("description", ""),
                agent_type=meta.get("agentType", ""),
                model=meta.get("model", ""),
                tailer=Tailer(f),
                last_ts=time.time(),
                state="done" if agent_id in session.done_agent_ids else "running",
            )
            session.subagents[agent_id] = sub
            block_id = session.subagents_by_tool.get(sub.tool_use_id)
            if block_id and block_id in self.blocks:
                sub.block_id = block_id
                self._update_block(block_id, state="running", meta={"agent_id": agent_id})
            else:
                block = self._add_block(session.id, "subagent", sub.description or sub.agent_type or "Subagent",
                                         state="running", meta={"agent_id": agent_id, "agent_type": sub.agent_type})
                sub.block_id = block.id

    def _mark_stale(self) -> None:
        now = time.time()
        for session in self.sessions.values():
            for sub in session.subagents.values():
                if sub.state == "running" and sub.last_ts and now - sub.last_ts > SUBAGENT_STALE_S:
                    sub.state = "stale"
                    if sub.block_id:
                        self._update_block(sub.block_id, state="stale",
                                            body=f"no activity for {int(now - sub.last_ts)}s")

    def _prune_subagents(self, session: Session) -> None:
        """Caps the per-session subagent list so a long-running session with many
        spawned subagents doesn't accumulate an ever-growing list in the UI."""
        now = time.time()
        for agent_id, sub in list(session.subagents.items()):
            if sub.state in ("done", "stale") and sub.last_ts and now - sub.last_ts > SUBAGENT_DONE_KEEP_S:
                del session.subagents[agent_id]
                session.pruned_agent_ids.add(agent_id)
        overflow = len(session.subagents) - MAX_SUBAGENTS_PER_SESSION
        if overflow > 0:
            oldest = sorted(session.subagents.values(), key=lambda s: s.last_ts)[:overflow]
            for sub in oldest:
                del session.subagents[sub.agent_id]
                session.pruned_agent_ids.add(sub.agent_id)

    # -- classification -----------------------------------------------------

    def _classify(self, session: Session, obj: dict) -> None:
        if obj.get("timestamp"):
            session.last_ts = time.time()
        cwd = obj.get("cwd")
        if cwd:
            session.cwd = cwd
        typ = obj.get("type")

        if typ == "user":
            self._classify_user(session, obj)
        elif typ == "assistant":
            self._classify_assistant(session, obj)
        elif typ == "attachment":
            self._classify_attachment(session, obj)
        elif typ == "queue-operation":
            if obj.get("operation") == "enqueue":
                content = obj.get("content", "")
                if isinstance(content, str) and "<task-notification>" in content:
                    self._handle_task_notification(session, content)
        elif typ == "system":
            self._classify_system(session, obj)
        elif typ == "ai-title":
            if obj.get("aiTitle"):
                session.label = obj["aiTitle"]
        elif typ == "agent-name":
            if obj.get("agentName"):
                session.label = obj["agentName"]
        elif typ == "permission-mode":
            session.perm_mode = obj.get("permissionMode", session.perm_mode)
        elif typ == "cost-state":
            if obj.get("totalCostUSD") is not None:
                session.cost_usd = obj["totalCostUSD"]
        else:
            session.unknown_types[typ or "?"] += 1

    def _classify_user(self, session: Session, obj: dict) -> None:
        if obj.get("isSidechain"):
            return
        content = obj.get("message", {}).get("content")
        is_meta = obj.get("isMeta", False)

        if isinstance(content, str):
            if is_meta:
                if content.startswith("Another Claude session sent a message:"):
                    self._handle_handback(session, content)
                return
            if content.startswith("<"):
                return
            session.current_prompt = content
            session.turn_open = True
            self._add_block(session.id, "prompt", "Prompt", content)
            return

        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "tool_result":
                self._handle_tool_result(session, obj, item)
            elif item_type == "text":
                text = item.get("text", "")
                if text.startswith("[Request interrupted"):
                    self._add_block(session.id, "interrupt", "Interrupted by user")
                    for bid in session.pending_tools.values():
                        self._update_block(bid, state="denied")
                    session.pending_tools.clear()

    def _handle_handback(self, session: Session, content: str) -> None:
        m = _HANDBACK_RE.search(content)
        idx = content.find("The report follows:")
        body = content[idx + len("The report follows:"):].strip() if idx >= 0 else content
        body = "\n".join(line.strip() for line in body.splitlines())
        agent_id = m.group(1) if m else None
        sub = session.subagents.get(agent_id) if agent_id else None
        if sub and sub.block_id:
            sub.state = "done"
            session.done_agent_ids.add(sub.agent_id)
            self._update_block(sub.block_id, state="done", body=body)
        else:
            self._add_block(session.id, "subagent", "Subagent report", body, state="done")

    def _handle_tool_result(self, session: Session, user_obj: dict, item: dict) -> None:
        tool_use_id = item.get("tool_use_id")
        bid = session.pending_tools.pop(tool_use_id, None) if tool_use_id else None
        is_error = item.get("is_error", False)
        tool_result = user_obj.get("toolUseResult") or {}
        denial = user_obj.get("toolDenialKind")

        state = "error" if is_error else "done"
        meta = {}
        body = None

        if denial:
            state = "denied"
            meta["denial"] = denial
        elif isinstance(tool_result, dict):
            if "agentId" in tool_result:
                if bid and tool_use_id:
                    session.subagents_by_tool[tool_use_id] = bid
                    sub = session.subagents.get(tool_result.get("agentId"))
                    if sub:
                        sub.block_id = bid
                        sub.tool_use_id = tool_use_id
                return
            if "answers" in tool_result:
                answers = tool_result.get("answers") or {}
                state = "answered"
                body = "; ".join(f"{q}: {a}" for q, a in answers.items())

        if bid:
            self._update_block(bid, state=state, body=body, meta=meta or None)

    def _classify_assistant(self, session: Session, obj: dict) -> None:
        if obj.get("isSidechain"):
            return
        message = obj.get("message", {})
        if obj.get("isApiErrorMessage"):
            text = ""
            content = message.get("content")
            if isinstance(content, list) and content:
                text = content[0].get("text", "")
            elif isinstance(content, str):
                text = content
            self._add_block(session.id, "error", "Error", text, state="error")
            return

        model = message.get("model")
        if model:
            session.model = model
        usage = message.get("usage")
        if usage:
            session.context_tokens = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )

        content = message.get("content")
        if not isinstance(content, list):
            return
        for cblock in content:
            if not isinstance(cblock, dict):
                continue
            ctype = cblock.get("type")
            if ctype == "text":
                self._add_block(session.id, "answer", "Answer", cblock.get("text", ""), meta={"model": model})
            elif ctype == "thinking" and SHOW_THINKING:
                self._add_block(session.id, "thinking", "Thinking", cblock.get("thinking", "")[:120])
            elif ctype == "tool_use":
                self._handle_tool_use(session, cblock)

    def _handle_tool_use(self, session: Session, cblock: dict) -> None:
        name = cblock.get("name", "")
        tool_input = cblock.get("input", {})
        tool_id = cblock.get("id")

        if name == "Agent":
            block = self._add_block(session.id, "subagent", tool_input.get("description", "Subagent"),
                                     state="running",
                                     meta={"subagent_type": tool_input.get("subagent_type", ""),
                                           "model": tool_input.get("model", "")})
            if tool_id:
                session.pending_tools[tool_id] = block.id
                session.subagents_by_tool[tool_id] = block.id
            return

        if name == "AskUserQuestion":
            questions = tool_input.get("questions", [])
            headers = " · ".join(q.get("header", "") for q in questions if isinstance(q, dict))
            body_lines = []
            for q in questions:
                if not isinstance(q, dict):
                    continue
                opts = ", ".join(o.get("label", "") for o in q.get("options", []) if isinstance(o, dict))
                body_lines.append(f"{q.get('question', '')} [{opts}]")
            block = self._add_block(session.id, "question", headers or "Question",
                                     "\n".join(body_lines), state="waiting")
            if tool_id:
                session.pending_tools[tool_id] = block.id
            return

        title, body = summarize_tool(name, tool_input)
        block = self._add_block(session.id, "tool", title, body, state="running")
        if tool_id:
            session.pending_tools[tool_id] = block.id

    def _classify_attachment(self, session: Session, obj: dict) -> None:
        att = obj.get("attachment", {})
        atype = att.get("type")
        if atype == "total_tokens_reminder":
            m = _TOTAL_TOKENS_RE.search(att.get("text", ""))
            if m:
                session.tokens_left = int(m.group(1))
        elif atype == "model":
            identity = att.get("identity", {})
            name = identity.get("marketingName") or identity.get("modelId")
            if name and name != session.model_name:
                if session.model_name:
                    self._add_block(session.id, "model", f"Model: {name}")
                session.model_name = name
        elif atype == "queued_command" and att.get("commandMode") == "prompt":
            self._add_block(session.id, "prompt", "Message sent mid-turn", att.get("prompt", ""),
                             meta={"queued": True})
        else:
            session.unknown_types[f"attachment:{atype}"] += 1

    def _classify_system(self, session: Session, obj: dict) -> None:
        subtype = obj.get("subtype")
        if subtype == "turn_duration":
            session.turn_open = False
            duration = obj.get("durationMs")
            seconds = f"{duration / 1000:.0f}s" if duration else ""
            self._add_block(session.id, "turn_end", f"Turn ended · {seconds}")
        elif subtype == "away_summary":
            self._add_block(session.id, "summary", "Update", obj.get("content", ""))
        elif subtype == "scheduled_task_fire":
            self._add_block(session.id, "wakeup", "Wakeup", obj.get("content", ""))
        else:
            session.unknown_types[f"system:{subtype}"] += 1

    def _handle_task_notification(self, session: Session, content: str) -> None:
        info = parse_task_notification(content)
        task_id = info.get("task_id")
        if not task_id:
            return
        key = (task_id, info.get("status"))
        if key in session.seen_notifications:
            return
        session.seen_notifications.add(key)
        # task_id is the agent_id for subagent notifications; mark done permanently, since
        # eviction from session.subagents (the count/time cap) can later rediscover the same
        # agent_id as a fresh Subagent, past the point this notification would fire again.
        session.done_agent_ids.add(task_id)

        tool_use_id = info.get("tool_use_id")
        block_id = session.subagents_by_tool.get(tool_use_id) if tool_use_id else None
        if not block_id:
            for sub in session.subagents.values():
                if sub.tool_use_id == tool_use_id:
                    block_id = sub.block_id
                    break

        body = info.get("summary") or ""
        meta = {k: info[k] for k in ("tokens", "tool_uses", "duration_ms") if info.get(k) is not None}
        if block_id:
            for sub in session.subagents.values():
                if sub.block_id == block_id:
                    sub.state = "done"
                    session.done_agent_ids.add(sub.agent_id)
            self._update_block(block_id, state="done", body=body, meta=meta or None)
        else:
            self._add_block(session.id, "subagent", body or "Subagent finished", state="done", meta=meta)

    def _classify_sub(self, session: Session, sub: Subagent, obj: dict) -> None:
        sub.last_ts = time.time()
        typ = obj.get("type")
        if typ == "attachment":
            att = obj.get("attachment", {})
            if att.get("type") == "total_tokens_reminder":
                m = _TOTAL_TOKENS_RE.search(att.get("text", ""))
                if m:
                    session.tokens_left = int(m.group(1))
            return
        if typ != "assistant":
            return
        content = obj.get("message", {}).get("content")
        if not isinstance(content, list):
            return
        for cblock in content:
            if not isinstance(cblock, dict):
                continue
            ctype = cblock.get("type")
            if ctype == "tool_use":
                title, body = summarize_tool(cblock.get("name", ""), cblock.get("input", {}))
                sub.last_activity = f"{title} {body}".strip()[:80]
            elif ctype == "text":
                sub.last_activity = cblock.get("text", "")[:80]
        if sub.block_id and sub.state == "running":
            self._update_block(sub.block_id, body=sub.last_activity)
