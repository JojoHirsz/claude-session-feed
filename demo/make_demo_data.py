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
    "Live-Feed laufender Claude-Code-Sitzungen, Status kommt aus Claude Codes eigener Prozess-Registry statt geraten zu werden.",
    "Filterleiste All/Active/Waiting/Error/Inactive: die Punkte leuchten in Farbe nur, wenn in der Kategorie auch etwas liegt.",
    "Doppelklick auf eine Sitzungskarte springt direkt zum echten Terminalfenster dieser Sitzung.",
    "Subagenten-Tracking je Sitzung: hoechstens 5 gleichzeitig sichtbar, fertige verschwinden automatisch nach 10 Minuten.",
    "Immer-im-Vordergrund-Pin, haelt auch ueber Minimieren und Maximieren hinweg.",
    "Hell/Dunkel folgt automatisch dem Windows-Systemthema oder laesst sich von Hand umstellen.",
    "Fenster dockt rechts am Bildschirmrand an und laesst sich in der Breite frei ziehen.",
    "Token-, Kosten- und Modellanzeige je Sitzung in Echtzeit.",
]

# (session label, cwd, status, prompt, [(feature_index, state), ...])
SESSIONS = [
    ("Landing page redesign", r"C:\Demo\landing-page", "busy",
     "Bau mir eine neue Startseite mit hellerem Header.",
     [(0, "done"), (1, "done"), (2, "running")]),
    ("API refactor", r"C:\Demo\billing-api", "idle",
     "Teile den Billing-Endpunkt in kleinere Handler auf.",
     [(3, "done"), (4, "running")]),
    ("Data migration", r"C:\Demo\warehouse-etl", "busy",
     "Migriere die alte Kundentabelle auf das neue Schema.",
     [(5, "done"), (6, "done"), (7, "done")]),
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
                "message": {"content": [{"type": "text", "text": description}]},
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
                    f"<summary>{description[:60]}</summary>\n"
                    "</task-notification>"
                )
                _write(transcript, {
                    "type": "queue-operation", "operation": "enqueue",
                    "timestamp": now_iso, "content": notification,
                })


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python make_demo_data.py <target_dir> <pid>")
        raise SystemExit(2)
    build(Path(sys.argv[1]), int(sys.argv[2]))
    print("demo data written to", sys.argv[1])
