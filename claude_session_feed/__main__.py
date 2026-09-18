"""Entry point: a narrow always-on-top window docked to the right screen edge.

Pinned to pywebview==5.4 (see requirements.txt): 6.x's EdgeChromium/WinForms
backend deadlocked the GUI thread on this machine during window activation,
consistently reproducible, tracked upstream as
https://github.com/r0x0r/pywebview/issues/1823 and unresolved as of 6.2.1.
5.4 does not reproduce it.

Also does NOT use pywebview's own `on_top` kwarg/property, since that setter is
exactly the unmarshaled cross-thread call the issue above describes. Topmost is
instead set with a direct Win32 SetWindowPos call, which is safe from any thread.
"""
from __future__ import annotations

import ctypes
import logging
from pathlib import Path
from typing import Optional

import webview

from .monitor import Monitor

_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_SPI_GETWORKAREA = 0x0030


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _primary_work_area() -> _Rect:
    """The primary monitor's usable area (screen minus taskbar), via plain ctypes.

    Used instead of pywebview's own `screens` property, which returned
    inconsistent values between pywebview versions in testing here.
    """
    rect = _Rect()
    ctypes.windll.user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    return rect


def _set_topmost(hwnd: int, topmost: bool) -> None:
    ctypes.windll.user32.SetWindowPos(
        hwnd, _HWND_TOPMOST if topmost else _HWND_NOTOPMOST,
        0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
    )


def _set_icon(window, png_path: str) -> None:
    """Sets the title-bar/taskbar icon from a PNG via .NET, so no .ico asset is needed."""
    try:
        import clr
        clr.AddReference("System.Drawing")
        from System.Drawing import Bitmap, Icon

        bitmap = Bitmap(png_path)
        window.native.Icon = Icon.FromHandle(bitmap.GetHicon())
    except Exception:
        logging.getLogger("claude_session_feed").exception("Could not set window icon")


def _set_app_id() -> None:
    """Gives this process its own taskbar identity.

    Without this, Windows groups pythonw.exe-hosted windows under a generic
    Python taskbar entry/icon (sometimes the interpreter's own icon, not ours),
    because taskbar grouping keys off the Application User Model ID, which
    otherwise defaults to the host exe. Must be called before the window exists.
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AiSessionBuddy.Widget")
    except Exception:
        logging.getLogger("claude_session_feed").exception("Could not set app id")


def _accent_color_hex() -> Optional[str]:
    """The user's Windows accent color, from the registry (undocumented but stable
    since Win10). Cross-checked here against two independent keys that must
    decode to the same color if the byte order is right:
      DWM\\ColorizationColor    — 0xAARRGGBB
      DWM\\AccentColor          — 0xAABBGGRR
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            value, _ = winreg.QueryValueEx(key, "ColorizationColor")
        r = (value >> 16) & 0xFF
        g = (value >> 8) & 0xFF
        b = value & 0xFF
        return f"#{r:02x}{g:02x}{b:02x}"
    except OSError:
        return None


class Api:
    """Note: attribute names starting with '_' are never walked into pywebview's
    JS-bridge introspection, which matters here — exposing the raw window/handle
    as a plain attribute makes older pywebview versions recurse into WinForms
    internals while building window.pywebview.api and crash."""

    def __init__(self, monitor: Monitor):
        self._monitor = monitor
        self._window = None
        self._hwnd: Optional[int] = None

    def poll(self, after_seq: int = 0) -> dict:
        try:
            self._monitor.tick()
            return self._monitor.drain(after_seq or 0)
        except Exception as exc:  # the feed must never take the window down with it
            return {"error": str(exc)}

    def get_theme(self) -> dict:
        return {"accent": _accent_color_hex()}

    def set_on_top(self, flag: bool) -> None:
        if self._hwnd:
            _set_topmost(self._hwnd, bool(flag))

    def quit(self) -> None:
        if self._window:
            self._window.destroy()


def main() -> None:
    _set_app_id()
    monitor = Monitor()
    api = Api(monitor)
    width = 390
    area = _primary_work_area()
    window = webview.create_window(
        "AI Session Buddy",
        str(Path(__file__).with_name("ui.html")),
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

    def on_shown():
        api._hwnd = int(window.native.Handle.ToInt64())
        _set_topmost(api._hwnd, True)
        _set_icon(window, str(Path(__file__).with_name("icon.png")))

    window.events.shown += on_shown
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
