"""Self-check for monitor.py: synthetic transcript lines through the real classify/Tailer path.

Run: python test_monitor.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from claude_session_feed.monitor import Monitor, Session, Tailer, pid_alive  # noqa: E402


def make_lines():
    return [
        json.dumps({"type": "user", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:00Z",
                    "isSidechain": False, "message": {"content": "Test prompt"}}),
        json.dumps({"type": "assistant", "cwd": "C:\\proj", "timestamp": "2026-09-18T10:00:01Z",
                    "isSidechain": False,
                    "message": {"model": "claude-sonnet-5", "stop_reason": "tool_use",
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
    assert session.tokens_left == 14912201
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


if __name__ == "__main__":
    run()
