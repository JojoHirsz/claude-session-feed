"""Builds a fake ~/.claude-shaped directory tree so the widget can be screenshotted
without ever touching real (sensitive) session data.

Usage: python demo/make_demo_data.py <target_dir> <pid>
  <pid> must belong to a process that is alive for as long as the demo widget
  runs against this directory (read_registry() drops entries whose pid isn't
  alive, via a real OpenProcess check) - pass the demo pythonw.exe's own pid.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

FEATURES = [
    "Live feed of running Claude Code sessions - status comes straight from Claude Code's own process registry, never guessed.",
    "Filter bar All/Active/Waiting/Error/Inactive: the dots only light up in color when a category actually has something in it.",
    "Double-click a session card to jump straight to that session's real terminal window.",
    "Per-session subagent tracking: at most 5 visible at once, finished ones drop off automatically after 10 minutes.",
    "Always-on-top pin, survives minimizing and maximizing.",
    "Light/dark follows the Windows system theme automatically, or switch it by hand.",
    "Window docks to the right screen edge and can be freely resized by width.",
    "Live token, cost, and model display per session.",
]

# Standalone closing sentence per feature for the done notification - deliberately
# NOT a slice/truncation of FEATURES[i], or the activity line turns into
# "Title — Title...".
DONE_SUMMARIES = [
    "Feed stayed stable across several session switches in testing.",
    "Color matching checked against empty and populated categories.",
    "Terminal jump verified across three separate attempts.",
    "Cap and expiry tested with real timestamps.",
    "Pin now survives minimize/maximize cycles too.",
    "Theme switching follows both the system value and the manual override.",
    "Width is resizable, the dock edge stays fixed.",
    "Numbers refresh every poll cycle without stutter.",
]

# (session label, cwd, status, prompt, [(feature_index, state), ...])
SESSIONS = [
    ("Landing page redesign", r"C:\Demo\landing-page", "busy",
     "Can you take a quick look at this?",
     [(0, "done"), (1, "done"), (2, "running")]),
    ("API refactor", r"C:\Demo\billing-api", "idle",
     "Split the billing endpoint into smaller handlers.",
     [(3, "done"), (4, "running")]),
    ("Data migration", r"C:\Demo\warehouse-etl", "busy",
     "Migrate the old customer table to the new schema.",
     [(5, "done"), (6, "done"), (7, "done")]),
    ("Release notes", r"C:\Demo\release-notes", "idle",
     "Finds running sessions regardless of shell "
     "- Git Bash, PowerShell, or a plain Windows terminal, all detected.",
     []),
]


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def build(demo_dir: Path, pid: int) -> None:
    now_ms = int(time.time() * 1000)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for label, cwd, status, prompt, agents in SESSIONS:
        session_id = str(uuid.uuid4())
        project_dir = demo_dir / "projects" / "demo"
        transcript = project_dir / f"{session_id}.jsonl"

        sessions_dir = demo_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{pid}-{session_id[:8]}.json").write_text(
            json.dumps({
                "pid": pid, "sessionId": session_id, "cwd": cwd,
                "startedAt": now_ms, "status": status, "name": label,
            }),
            encoding="utf-8",
        )

        _write(transcript, {"type": "ai-title", "aiTitle": label, "timestamp": now_iso})
        _write(transcript, {
            "type": "user", "cwd": cwd, "timestamp": now_iso,
            "message": {"content": prompt},
        })

        for feature_index, state in agents:
            agent_id = uuid.uuid4().hex[:17]
            tool_use_id = "toolu_demo_" + uuid.uuid4().hex[:20]
            description = FEATURES[feature_index]
            summary = DONE_SUMMARIES[feature_index]

            _write(transcript, {
                "type": "assistant", "timestamp": now_iso,
                "message": {"content": [{
                    "type": "tool_use", "id": tool_use_id, "name": "Agent",
                    "input": {"subagent_type": "fork", "description": description},
                }]},
            })
            _write(transcript, {
                "type": "user", "timestamp": now_iso,
                "toolUseResult": {"isAsync": True, "agentId": agent_id},
                "message": {"content": [{
                    "type": "tool_result", "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": "Async agent launched"}],
                }]},
            })

            sub_dir = project_dir / session_id / "subagents"
            _write(sub_dir / f"agent-{agent_id}.jsonl", {
                "type": "assistant", "timestamp": now_iso,
                "message": {"content": [{"type": "text", "text": summary}]},
            })
            (sub_dir / f"agent-{agent_id}.meta.json").write_text(
                json.dumps({
                    "agentType": "fork", "isFork": True,
                    "description": description, "toolUseId": tool_use_id,
                }),
                encoding="utf-8",
            )

            if state == "done":
                notification = (
                    "<task-notification>\n"
                    f"<task-id>{agent_id}</task-id>\n"
                    f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                    "<status>completed</status>\n"
                    f"<summary>{summary}</summary>\n"
                    "</task-notification>"
                )
                _write(transcript, {
                    "type": "queue-operation", "operation": "enqueue",
                    "timestamp": now_iso, "content": notification,
                })


# One Codex CLI session, "busy" (no task_complete yet, so it reads as active rather than
# idle -- see monitor.py's _classify_codex/_touch_session_activity), with a token_count
# event so the card's context indicator (model_context_window - last_token_usage, see
# codex_monitor.py's docstring) is actually visible in the screenshot.
CODEX_SESSION = (
    "Refactor auth middleware", r"C:\Demo\auth-service",
    "Split session refresh out of the login handler into its own module.",
    "Extracted refresh-token handling into session.rs, updated the three call sites, "
    "ran the auth test suite -- all green.",
    258_400,  # model_context_window, as observed live for gpt-5.6-terra (see codex_monitor.py)
    190_000,  # last_token_usage.total_tokens
)


def build_codex(codex_dir: Path, pid: int) -> None:
    """Writes one ~/.codex-shaped session (rollout file + session_index.jsonl entry) so
    discover_codex_sessions() can find it via the CLAUDE_SESSION_FEED_CODEX_DEMO_PID
    bypass in codex_monitor.discover_live_pids() (no real codex.exe process involved).

    The rollout filename's embedded local timestamp must land within find_rollout()'s
    30s tolerance of the epoch that bypass hands back (time.time() at call time), so
    this stamps "now" too -- both this write and the widget's first discovery tick
    happen within a couple of seconds of each other.
    """
    label, cwd, prompt, answer, window, used = CODEX_SESSION
    session_id = str(uuid.uuid4())
    now = time.localtime()  # local wall-clock, matching the rollout filename convention
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", now)
    day_dir = codex_dir / "sessions" / time.strftime("%Y", now) / time.strftime("%m", now) / time.strftime("%d", now)
    rollout = day_dir / f"rollout-{stamp}-{session_id}.jsonl"

    _write(rollout, {"type": "session_meta", "payload": {
        "session_id": session_id, "cwd": cwd, "originator": "codex-tui",
    }})
    _write(rollout, {"type": "event_msg", "payload": {"type": "task_started"}})
    _write(rollout, {"type": "event_msg", "payload": {
        "type": "item_completed",
        "item": {"type": "UserMessage", "content": [{"type": "Text", "text": prompt}]},
    }})
    _write(rollout, {"type": "event_msg", "payload": {
        "type": "item_completed",
        "item": {"type": "AgentMessage", "content": [{"type": "Text", "text": answer}]},
    }})
    _write(rollout, {"type": "event_msg", "payload": {
        "type": "token_count",
        "info": {
            "total_token_usage": {"total_tokens": used + 620_000},  # lifetime-cumulative, not context fill
            "last_token_usage": {"total_tokens": used},
            "model_context_window": window,
        },
    }})

    _write(codex_dir / "session_index.jsonl", {"id": session_id, "thread_name": label})


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python make_demo_data.py <target_dir> <pid>")
        raise SystemExit(2)
    build(Path(sys.argv[1]), int(sys.argv[2]))
    build_codex(Path(sys.argv[1]) / "codex_home", int(sys.argv[2]))
    print("demo data written to", sys.argv[1])
