"""Self-check for codex_monitor.py: the file-parsing logic against synthetic
~/.codex-shaped fixtures, plus a couple of ctypes smoke checks against our own
process (deterministic ground truth -- no live `codex` session required to run
this).

Run: python test_codex_monitor.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from claude_session_feed import codex_monitor  # noqa: E402


def run_rollout_epoch_and_find_rollout():
    with tempfile.TemporaryDirectory() as td:
        codex_monitor.CODEX_DIR = Path(td)
        day_dir = codex_monitor.CODEX_DIR / "sessions" / "2026" / "09" / "24"
        day_dir.mkdir(parents=True)
        near = day_dir / "rollout-2026-09-24T10-32-06-01a0d28b-19e2-7731-8529-b0e48c13b155.jsonl"
        near.write_text("{}\n", encoding="utf-8")
        far = day_dir / "rollout-2026-09-24T09-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
        far.write_text("{}\n", encoding="utf-8")
        not_rollout = day_dir / "not-a-rollout.jsonl"
        not_rollout.write_text("{}\n", encoding="utf-8")

        near_epoch = codex_monitor._rollout_epoch(near)
        assert near_epoch is not None, "must parse a well-formed rollout filename"
        assert codex_monitor._rollout_epoch(not_rollout) is None, "must reject a non-matching filename"

        # process started 4s after the rollout file's embedded timestamp (matches
        # what was actually observed live: rollout created, then first append 4s later)
        found = codex_monitor.find_rollout(near_epoch + 4)
        assert found == near, f"expected {near}, got {found}"

        # nothing within tolerance -> no match, must not silently pick the wrong day
        assert codex_monitor.find_rollout(near_epoch + 10_000, tolerance_s=30.0) is None

    print("OK - rollout_epoch/find_rollout: nearest-in-time match, out-of-tolerance -> None")


def run_read_session_meta():
    with tempfile.TemporaryDirectory() as td:
        rollout = Path(td) / "rollout.jsonl"
        lines = [
            json.dumps({"type": "session_meta", "payload": {
                "session_id": "sid-1", "cwd": "C:\\proj", "originator": "codex-tui",
            }}),
            json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
        ]
        rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")

        meta = codex_monitor.read_session_meta(rollout)
        assert meta == {"session_id": "sid-1", "cwd": "C:\\proj", "originator": "codex-tui"}, meta

        # malformed/missing file must not raise
        assert codex_monitor.read_session_meta(Path(td) / "missing.jsonl") == {}

    print("OK - read_session_meta: first-line session_meta parsed, missing file tolerated")


def run_read_thread_name():
    with tempfile.TemporaryDirectory() as td:
        codex_monitor.CODEX_DIR = Path(td)
        index = codex_monitor.CODEX_DIR / "session_index.jsonl"
        index.write_text("\n".join([
            json.dumps({"id": "sid-1", "thread_name": "moin"}),
            json.dumps({"id": "sid-1", "thread_name": "Begrüße den Nutzer"}),
            json.dumps({"id": "sid-2", "thread_name": "other session"}),
        ]) + "\n", encoding="utf-8")

        # last line for the id wins (title gets refined once early in the conversation)
        assert codex_monitor.read_thread_name("sid-1") == "Begrüße den Nutzer"
        assert codex_monitor.read_thread_name("sid-2") == "other session"
        assert codex_monitor.read_thread_name("no-such-id") == ""
        assert codex_monitor.read_thread_name("") == ""

    print("OK - read_thread_name: last-line-wins across a shared, cross-session index")


def run_discover_codex_sessions_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        codex_monitor.CODEX_DIR = Path(td)
        day_dir = codex_monitor.CODEX_DIR / "sessions" / "2026" / "09" / "24"
        day_dir.mkdir(parents=True)
        rollout = day_dir / "rollout-2026-09-24T10-32-06-01a0d28b-19e2-7731-8529-b0e48c13b155.jsonl"
        started = codex_monitor._rollout_epoch(rollout) + 4  # process starts, rollout file follows a beat later
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "session_id": "01a0d28b-19e2-7731-8529-b0e48c13b155", "cwd": "C:\\proj",
            "originator": "codex-tui",
        }}) + "\n", encoding="utf-8")
        (codex_monitor.CODEX_DIR / "session_index.jsonl").write_text(
            json.dumps({"id": "01a0d28b-19e2-7731-8529-b0e48c13b155", "thread_name": "Plane morgen"}) + "\n",
            encoding="utf-8",
        )

        # monkeypatch discovery of live pids -- the OS-process part is exercised
        # separately below against our own process, not re-mocked here
        real_discover_live_pids = codex_monitor.discover_live_pids
        codex_monitor.discover_live_pids = lambda: {12345: started}
        try:
            sessions = codex_monitor.discover_codex_sessions()

            assert len(sessions) == 1, sessions
            info = sessions[0]
            assert info.pid == 12345
            assert info.cwd == "C:\\proj"
            assert info.label == "Plane morgen"
            assert info.rollout_path == rollout

            # a batch/non-interactive originator must be filtered out
            rollout.write_text(json.dumps({"type": "session_meta", "payload": {
                "session_id": "s", "cwd": "C:\\proj", "originator": "codex-exec",
            }}) + "\n", encoding="utf-8")
            assert codex_monitor.discover_codex_sessions() == []
        finally:
            codex_monitor.discover_live_pids = real_discover_live_pids

    print("OK - discover_codex_sessions: wires pid+rollout+title together, filters non-interactive")


def run_ctypes_smoke_against_own_process():
    # No live `codex` process required: our own interpreter process is a
    # deterministic, always-available ground truth for the ctypes plumbing itself.
    pid = os.getpid()
    path = codex_monitor._process_image_path(pid)
    assert path, "must resolve our own process's image path"
    assert "python" in path.lower(), path

    start = codex_monitor._process_start_epoch(pid)
    assert start is not None
    now = time.time()
    # sanity check the FILETIME->epoch conversion: a wrong 1601/1970 offset
    # constant would land decades off, not just "a bit early"
    assert now - 3600 < start <= now, f"start={start} now={now}"

    # must never raise even with no codex process anywhere on the machine
    assert isinstance(codex_monitor._codex_exe_pids(), list)
    assert isinstance(codex_monitor.discover_live_pids(), dict)

    print("OK - ctypes smoke: image path + FILETIME conversion correct against our own process")


def run_filters_internal_codex_binaries():
    # ~/.codex ships its OWN copies of codex.exe (sandbox helper, plugin host) --
    # those must never be mistaken for a real interactive user session. Real
    # sessions run from the npm global install, outside ~/.codex entirely.
    with tempfile.TemporaryDirectory() as td:
        codex_monitor.CODEX_DIR = Path(td)
        fake_paths = {
            111: str(Path(td) / ".sandbox-bin" / "codex.exe"),        # internal helper
            222: str(Path(td) / "plugins" / ".plugin-appserver" / "codex.exe"),  # internal helper
            333: r"C:\Users\User\AppData\Roaming\npm\node_modules\@openai\codex\codex.exe",  # real
        }
        real_pids_fn = codex_monitor._codex_exe_pids
        real_path_fn = codex_monitor._process_image_path
        real_start_fn = codex_monitor._process_start_epoch
        codex_monitor._codex_exe_pids = lambda: list(fake_paths)
        codex_monitor._process_image_path = lambda pid: fake_paths[pid]
        codex_monitor._process_start_epoch = lambda pid: 1000.0
        try:
            live = codex_monitor.discover_live_pids()
        finally:
            codex_monitor._codex_exe_pids = real_pids_fn
            codex_monitor._process_image_path = real_path_fn
            codex_monitor._process_start_epoch = real_start_fn

        assert live == {333: 1000.0}, live

    print("OK - discover_live_pids: internal ~/.codex helper binaries excluded, real session kept")


def run_demo_pid_bypass():
    # Unset: normal scan path runs, untouched (same assertion as the ctypes smoke test).
    os.environ.pop("CLAUDE_SESSION_FEED_CODEX_DEMO_PID", None)
    assert isinstance(codex_monitor.discover_live_pids(), dict)

    # Set: hands back exactly that one pid, skipping the Toolhelp32 scan/path filter --
    # proven by pointing it at a pid that would never pass the real path filter (0 is not
    # a real codex.exe process, but the bypass must not care).
    os.environ["CLAUDE_SESSION_FEED_CODEX_DEMO_PID"] = "4242"
    try:
        live = codex_monitor.discover_live_pids()
        assert set(live) == {4242}, live
        assert isinstance(live[4242], float)
    finally:
        os.environ.pop("CLAUDE_SESSION_FEED_CODEX_DEMO_PID", None)

    print("OK - discover_live_pids: demo pid bypass returns exactly the given pid, unset is a no-op")


if __name__ == "__main__":
    run_rollout_epoch_and_find_rollout()
    run_read_session_meta()
    run_read_thread_name()
    run_discover_codex_sessions_end_to_end()
    run_ctypes_smoke_against_own_process()
    run_filters_internal_codex_binaries()
    run_demo_pid_bypass()
