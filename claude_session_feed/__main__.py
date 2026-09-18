"""Entry point: a narrow always-on-top window docked to the right screen edge."""
from __future__ import annotations

from pathlib import Path

import webview

from .monitor import Monitor


class Api:
    def __init__(self, monitor: Monitor):
        self._monitor = monitor
        self.window = None

    def poll(self, after_seq: int = 0) -> dict:
        try:
            self._monitor.tick()
            return self._monitor.drain(after_seq or 0)
        except Exception as exc:  # the feed must never take the window down with it
            return {"error": str(exc)}

    def set_on_top(self, flag: bool) -> None:
        if self.window:
            self.window.on_top = bool(flag)

    def quit(self) -> None:
        if self.window:
            self.window.destroy()


def main() -> None:
    monitor = Monitor()
    api = Api(monitor)
    screen = webview.screens[0]
    width = 390
    window = webview.create_window(
        "Claude Session Feed",
        str(Path(__file__).with_name("ui.html")),
        js_api=api,
        width=width,
        height=screen.height - 48,
        x=screen.width - width,
        y=0,
        frameless=True,
        on_top=True,
        resizable=True,
        min_size=(320, 480),
    )
    api.window = window
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
