"""Self-check for monitor.py: synthetic transcript lines through the real classify/Tailer path.

Run: python test_monitor.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import time

from claude_session_feed.monitor import (  # noqa: E402
    MAX_SUBAGENTS_PER_SESSION, SUBAGENT_DONE_KEEP_S, Monitor, Session, Subagent, Tailer, pid_alive,
    parse_task_notification,
)
from claude_session_feed.__main__ import _pick_jump_label  # noqa: E402


def make_lines():
    return [
        json.dumps({"type": "user", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:00Z",
                    "isSidechain": False, "message": {"content": "Test prompt"}}),
        json.dumps({"type": "assistant", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:01Z",
                    "isSidechain": False,
                    "message": {"model": "claude-sonnet-5", "stop_reason": "tool_use",
                                "usage": {"input_tokens": 50000, "cache_read_input_tokens": 100000,
                                          "cache_creation_input_tokens": 0},
                                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                             "input": {"command": "echo hi", "description": "Test"}}]}}),
        json.dumps({"type": "user", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:02Z",
                    "isSidechain": False,
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi"}]},
                    "toolUseResult": {"stdout": "hi", "stderr": "", "interrupted": False}}),
        json.dumps({"type": "assistant", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:03Z",
                    "isSidechain": False,
                    "message": {"model": "claude-sonnet-5", "stop_reason": "tool_use",
                                "content": [{"type": "tool_use", "id": "toolu_2", "name": "Agent",
                                             "input": {"description": "Test agent"}}]}}),
        json.dumps({"type": "user", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:04Z",
                    "isSidechain": False,
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "async"}]},
                    "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": "ag1"}}),
        json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-09-18T10:00:05Z",
                    "content": "<task-notification><task-id>ag1</task-id>"
                               "<tool-use-id>toolu_2</tool-use-id><status>completed</status>"
                               "<summary>Test agent finished</summary>"
                               "<usage><subagent_tokens>100221</subagent_tokens>"
                               "<tool_uses>21</tool_uses><duration_ms>166708</duration_ms></usage>"
                               "</task-notification>"}),
        json.dumps({"type": "assistant", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:06Z",
                    "isSidechain": False,
                    "message": {"model": "claude-sonnet-5", "stop_reason": "tool_use",
                                "content": [{"type": "tool_use", "id": "toolu_3", "name": "AskUserQuestion",
                                             "input": {"questions": [{"header": "Q", "question": "Continue?",
                                                                       "options": [{"label": "Yes"}, {"label": "No"}]}]}}]}}),
        json.dumps({"type": "user", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:07Z",
                    "isSidechain": False,
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_3", "content": "ok"}]},
                    "toolUseResult": {"answers": {"Continue?": "Yes"}}}),
        json.dumps({"type": "attachment", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:08Z",
                    "isSidechain": False,
                    "attachment": {"type": "total_tokens_reminder",
                                   "text": "<total_tokens>14912201 tokens left</total_tokens>"}}),
        json.dumps({"type": "attachment", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:09Z",
                    "isSidechain": False,
                    "attachment": {"type": "model", "identity": {"marketingName": "Sonnet 5"}}}),
        json.dumps({"type": "system", "subtype": "turn_duration", "timestamp": "2026-09-18T10:00:10Z",
                    "cwd": "C:\\proj", "durationMs": 41000, "messageCount": 4}),
        '{"type":',  # malformed on purpose
        json.dumps({"type": "future_thing"}),  # unknown type, must not raise
    ]


def run():
    monitor = Monitor()
    session = Session(id="s1", cwd="C:\\proj")
    monitor.sessions["s1"] = session

    tmp = Path(tempfile.mkdtemp()) / "s1.jsonl"
    tmp.write_text("", encoding="utf-8")
    tailer = Tailer(tmp)
    session.tailer = tailer

    with open(tmp, "a", encoding="utf-8") as fh:
        fh.write("\n".join(make_lines()) + "\n")

    for obj in tailer.read_new():
        monitor._classify(session, obj)

    blocks = list(monitor.blocks.values())
    kinds = [b.kind for b in blocks]
    assert "prompt" in kinds, kinds
    assert any(b.kind == "tool" and b.state == "done" for b in blocks), kinds
    subagent_blocks = [b for b in blocks if b.kind == "subagent"]
    assert any(b.state == "done" and b.meta.get("tokens") == 100221 for b in subagent_blocks), subagent_blocks
    assert any(b.kind == "question" and b.state == "answered" for b in blocks), kinds
    # tokens_left comes from context_tokens + the model's known context window now, not
    # the <total_tokens> reminder above (kept in make_lines() to prove it's ignored):
    # claude-sonnet-5 is 1M, usage summed to 150000 used -> 850000 left.
    assert session.context_tokens == 150000, session.context_tokens
    assert session.tokens_left == 1_000_000 - 150000, session.tokens_left
    assert session.unknown_types.get("future_thing") == 1, session.unknown_types
    # turn_duration is the last real event, and nothing waiting/erroring came after
    # the answered question, so the one-card-per-session summary should read idle —
    # which, for a still-running process, categorizes as "waiting on the human".
    assert session.activity_state == "idle", session.activity_state
    assert session.category() == "waiting", session.category()

    # unfinished last line must be buffered, not dropped or crashed on
    with open(tmp, "a", encoding="utf-8") as fh:
        fh.write('{"type": "user", "cwd": "C:\\\\proj", "message": {"content": "hal')
    assert tailer.read_new() == []
    assert tailer.buf != b""
    with open(tmp, "a", encoding="utf-8") as fh:
        fh.write('bo"}}\n')
    assert len(tailer.read_new()) == 1

    assert pid_alive(os.getpid()) is True
    assert pid_alive(4_000_000) is False

    print(f"OK - {len(blocks)} blocks, {sum(session.unknown_types.values())} unknown line(s)")


def run_prune_subagents():
    monitor = Monitor()
    session = Session(id="s1", cwd="C:\\proj")
    now = time.time()

    # count cap: 8 subagents, oldest last_ts first -> only the 5 newest survive
    for i in range(8):
        session.subagents[f"a{i}"] = Subagent(agent_id=f"a{i}", state="running", last_ts=now - (8 - i))
    monitor._prune_subagents(session)
    assert len(session.subagents) == MAX_SUBAGENTS_PER_SESSION, session.subagents
    assert set(session.subagents) == {"a3", "a4", "a5", "a6", "a7"}, session.subagents

    # time-based expiry: done/stale older than SUBAGENT_DONE_KEEP_S disappear, newer stay
    session2 = Session(id="s2", cwd="C:\\proj")
    session2.subagents["old"] = Subagent(agent_id="old", state="done", last_ts=now - SUBAGENT_DONE_KEEP_S - 100)
    session2.subagents["recent"] = Subagent(agent_id="recent", state="done", last_ts=now - 300)
    session2.subagents["running"] = Subagent(agent_id="running", state="running", last_ts=now)
    monitor._prune_subagents(session2)
    assert set(session2.subagents) == {"recent", "running"}, session2.subagents

    print("OK - prune_subagents: count cap and time expiry both hold")


def run_scan_subagents_revives_done():
    # A subagent that finished and was later evicted (count/time cap) must come back as
    # "done", not "running", when _scan_subagents() rediscovers its still-on-disk
    # agent-*.jsonl on the next cycle -- its <task-notification> already fired once and
    # won't fire again (session.seen_notifications dedup), so state has to be remembered
    # per agent_id (session.done_agent_ids), not only on the (evicted) Subagent object.
    monitor = Monitor()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        session_id = "s1"
        transcript = root / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        agents_dir = root / session_id / "subagents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent-x1.jsonl").write_text("", encoding="utf-8")
        (agents_dir / "agent-x1.meta.json").write_text(
            json.dumps({"toolUseId": "tu1", "description": "d", "agentType": "fork"}), encoding="utf-8"
        )

        session = Session(id=session_id, cwd="C:\\proj", tailer=Tailer(transcript))

        # first discovery: not done yet -> "running"
        monitor._scan_subagents(session)
        assert session.subagents["x1"].state == "running", session.subagents["x1"]

        # notification arrives, then the cap evicts it (simulating _prune_subagents)
        session.done_agent_ids.add("x1")
        del session.subagents["x1"]

        # rediscovery must not resurrect it as "running"
        monitor._scan_subagents(session)
        assert session.subagents["x1"].state == "done", session.subagents["x1"]

    print("OK - scan_subagents: evicted-then-rediscovered agent stays done")


def run_scan_subagents_pruned_stays_gone():
    # Eviction must be a one-way trip: re-globbing agent-*.jsonl on the next discovery
    # cycle (every ~2.1s) used to resurrect a pruned agent as a fresh entry, bumping its
    # last_ts to "now" and making _prune_subagents() evict a DIFFERENT one next cycle
    # instead -- a carousel of subagents cycling in and out of the 5-slot list.
    monitor = Monitor()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        session_id = "s1"
        transcript = root / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")
        agents_dir = root / session_id / "subagents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent-x1.jsonl").write_text("", encoding="utf-8")
        (agents_dir / "agent-x1.meta.json").write_text(
            json.dumps({"toolUseId": "tu1", "description": "d", "agentType": "fork"}), encoding="utf-8"
        )

        session = Session(id=session_id, cwd="C:\\proj", tailer=Tailer(transcript))
        monitor._scan_subagents(session)
        assert "x1" in session.subagents

        # pruned (count or time cap) -> marked as pruned, same as _prune_subagents() does
        del session.subagents["x1"]
        session.pruned_agent_ids.add("x1")

        # its file is still on disk; repeated discovery must not bring it back
        for _ in range(5):
            monitor._scan_subagents(session)
        assert "x1" not in session.subagents, session.subagents

    print("OK - scan_subagents: pruned agent stays gone across repeated discovery")


def run_parse_task_notification():
    # Real transcripts put a newline between </task-id> and <tool-use-id>; a bug here left
    # tool_use_id permanently None, which silently broke matching the subagent back to its
    # block, so it never left "running" state.
    text = (
        "<task-notification>\n"
        "<task-id>abc123</task-id>\n"
        "<tool-use-id>toolu_xyz</tool-use-id>\n"
        "<output-file>C:\\out.txt</output-file>\n"
        "<status>completed</status>\n"
        "<summary>done</summary>\n"
        "</task-notification>"
    )
    info = parse_task_notification(text)
    assert info["task_id"] == "abc123", info
    assert info["tool_use_id"] == "toolu_xyz", info
    assert info["status"] == "completed", info

    print("OK - parse_task_notification: tool_use_id survives the newline")


def run_classify_codex_token_count():
    # Codex's token_count event carries two different counters (see monitor.py's comment
    # at the call site) -- this locks in that tokens_left/context_tokens come from
    # last_token_usage (current, bounded) and NOT total_token_usage (lifetime, unbounded,
    # would make tokens_left go negative and stay there for the rest of a long session).
    monitor = Monitor()
    session = Session(id="c1", cwd="C:\\proj", source="codex")
    monitor.sessions["c1"] = session

    event = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 880344},  # lifetime-cumulative, must be ignored
        "last_token_usage": {"total_tokens": 55710},    # current turn's context size
        "model_context_window": 258400,
    }}}
    monitor._classify_codex(session, event)
    assert session.context_tokens == 55710, session.context_tokens
    assert session.tokens_left == 258400 - 55710, session.tokens_left

    # missing model_context_window (unknown model/future format) -> no invented number
    session2 = Session(id="c2", cwd="C:\\proj", source="codex")
    monitor._classify_codex(session2, {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"total_tokens": 100},
    }}})
    assert session2.context_tokens == 100, session2.context_tokens
    assert session2.tokens_left is None, session2.tokens_left

    print("OK - classify_codex token_count: tokens_left from the window, not the lifetime total")


def run_classify_claude_tokens_left():
    # Mirrors run_classify_codex_token_count() for the Claude Code side: tokens_left must
    # come from context_tokens (real per-turn usage) plus the model's known context window
    # (CLAUDE_CONTEXT_WINDOWS), not the old <total_tokens> session-quota reminder.
    from claude_session_feed.monitor import CLAUDE_CONTEXT_WINDOWS  # noqa: E402

    monitor = Monitor()
    session = Session(id="s1", cwd="C:\\proj")
    monitor.sessions["s1"] = session

    assistant_msg = {"type": "assistant", "isSidechain": False, "message": {
        "model": "claude-sonnet-5",
        "usage": {"input_tokens": 50000, "cache_read_input_tokens": 100000,
                   "cache_creation_input_tokens": 0},
        "content": [{"type": "text", "text": "hi"}],
    }}
    monitor._classify(session, assistant_msg)
    assert session.context_tokens == 150000, session.context_tokens
    assert session.tokens_left == CLAUDE_CONTEXT_WINDOWS["claude-sonnet-5"] - 150000, session.tokens_left

    # unknown model (not in the researched table) -> tokens_left is None, never a guess
    session2 = Session(id="s2", cwd="C:\\proj")
    monitor._classify(session2, {"type": "assistant", "isSidechain": False, "message": {
        "model": "claude-nonexistent-9",
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "content": [],
    }})
    assert session2.context_tokens == 10, session2.context_tokens
    assert session2.tokens_left is None, session2.tokens_left

    print("OK - classify_assistant tokens_left: known model uses its window, unknown model stays None")


def run_pick_jump_label():
    # /clear keeps the same terminal (same pid) but starts a fresh session id; the old
    # Session object lingers with status="ended" and its now-stale label. Picking that
    # one sends _jump_to_pid() searching for a window title Claude Code already
    # overwrote, silently breaking the double-click jump on the new, active session.
    old = Session(id="s1", cwd="C:\\proj", pid=111, label="Old Name", status="ended", last_ts=100.0)
    new = Session(id="s2", cwd="C:\\proj", pid=111, label="New Name", status="busy", last_ts=200.0)
    sessions = {"s1": old, "s2": new}

    assert _pick_jump_label(sessions.values(), 111) == "New Name", "must prefer the live session"
    assert _pick_jump_label(sessions.values(), 999) == "", "no match -> empty label"

    print("OK - pick_jump_label: live session wins a pid collision after /clear")


if __name__ == "__main__":
    run()
    run_prune_subagents()
    run_scan_subagents_revives_done()
    run_scan_subagents_pruned_stays_gone()
    run_parse_task_notification()
    run_classify_codex_token_count()
    run_classify_claude_tokens_left()
    run_pick_jump_label()
