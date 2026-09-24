"""One-off: launches a demo instance of the widget against synthetic data
(never the real ~/.claude) and saves a light and a dark screenshot.

Theme is switched via window.evaluate_js("setThemeChoice(...)") -> the same
JS function the logo-click dropdown calls -> no OS mouse click needed, so
this can never focus-steal into an unrelated window.

Usage: python demo/screenshot_demo.py
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT_LIGHT = REPO / "demo_screenshot_hell.png"
OUT_DARK = REPO / "demo_screenshot_dunkel.png"


_PW_RENDERFULLCONTENT = 2  # plain BitBlt-style capture comes back solid black for
# GPU-accelerated/Chromium (WebView2) windows - this flag makes PrintWindow render
# the window's real content into our DC instead of whatever the desktop compositor
# happens to have redirected, confirmed live: without it both screenshots here were
# a few KB of solid black.


def _shoot(hwnd: int, out_path: Path) -> None:
    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    try:
        ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), _PW_RENDERFULLCONTENT)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
        )
        img.save(out_path)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def main() -> None:
    ctypes.windll.user32.SetProcessDPIAware()

    # read_registry() only keeps entries whose pid is a live process (real
    # OpenProcess check) - this holder just needs to stay alive long enough.
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        demo_dir = Path(tempfile.mkdtemp(prefix="session_buddy_demo_"))
        import make_demo_data

        make_demo_data.build(demo_dir, holder.pid)
        codex_dir = demo_dir / "codex_home"
        make_demo_data.build_codex(codex_dir, holder.pid)
        os.environ["CLAUDE_SESSION_FEED_DEMO_DIR"] = str(demo_dir)
        os.environ["CLAUDE_SESSION_FEED_CODEX_DEMO_DIR"] = str(codex_dir)
        os.environ["CLAUDE_SESSION_FEED_CODEX_DEMO_PID"] = str(holder.pid)

        import webview

        from claude_session_feed.__main__ import Api, _primary_work_area, _set_app_id, _set_topmost
        from claude_session_feed.monitor import Monitor

        _set_app_id()
        monitor = Monitor()
        api = Api(monitor)
        width = 390
        area = _primary_work_area()
        window = webview.create_window(
            "AI Session Buddy - DEMO SCREENSHOT",
            str(REPO / "claude_session_feed" / "ui.html"),
            js_api=api,
            width=width,
            height=area.bottom - area.top,
            x=area.right - width,
            y=area.top,
            frameless=True,
            resizable=True,
            min_size=(320, 480),
        )
        api._window = window
        state: dict = {}

        def on_shown() -> None:
            state["hwnd"] = int(window.native.Handle.ToInt64())
            _set_topmost(state["hwnd"], True)

        window.events.shown += on_shown

        def set_theme(choice: str) -> None:
            # setThemeChoice() itself lives inside ui.html's top-level IIFE, so it
            # isn't reachable from evaluate_js's global scope - replicate its two
            # visible effects (persisted choice + the data-theme attribute the
            # CSS keys off) directly instead.
            window.evaluate_js(
                "localStorage.setItem('sessionBuddyTheme', '%s');"
                "document.documentElement.dataset.theme = '%s';" % (choice, choice)
            )

        def worker() -> None:
            try:
                time.sleep(3)  # let the feed poll and render the demo sessions
                hwnd = state["hwnd"]
                set_theme("light")
                time.sleep(1)
                _shoot(hwnd, OUT_LIGHT)
                set_theme("dark")
                time.sleep(1)
                _shoot(hwnd, OUT_DARK)
            finally:
                window.destroy()

        webview.start(worker, gui="edgechromium", debug=False)
    finally:
        holder.terminate()


if __name__ == "__main__":
    main()
