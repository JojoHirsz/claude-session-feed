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


# Without explicit argtypes, ctypes marshals bare Python ints as 32-bit c_int. hWndInsertAfter
# (-1/-2 for HWND_TOPMOST/HWND_NOTOPMOST) then arrives corrupted on 64-bit Windows instead of
# sign-extended to a full pointer, so SetWindowPos silently failed (GetLastError 1400,
# ERROR_INVALID_WINDOW_HANDLE) on every call, including the very first one in on_shown — the
# window was never actually topmost, no matter what the pin button did. Confirmed by calling it
# both ways against the live window's real HWND: without argtypes ret=0/err=1400, with them
# ret=1 and GetWindowLongPtr(GWL_EXSTYLE) actually shows WS_EX_TOPMOST set afterwards.
#
# This must be its own bound prototype, NOT `ctypes.windll.user32.SetWindowPos.argtypes = [...]`:
# that attribute access returns a function object ctypes caches per-DLL-per-name and shares
# across the whole process. Setting argtypes on it broke pywebview's own internal window-move
# (winforms.py BrowserForm.move(), used for header dragging), which calls the very same shared
# SetWindowPos with None for the unused width/height args — fine with no argtypes declared, but
# a TypeError/ArgumentError once we forced c_int there. That exception is raised on a background
# dispatch thread with no console attached (pythonw), so it never surfaces anywhere: dragging
# just silently stopped working. Confirmed by reproducing pywebview's exact call shape after
# setting argtypes on the shared object (raises ArgumentError) vs. after removing them (works).
_set_window_pos = ctypes.WINFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
)(("SetWindowPos", ctypes.windll.user32))


def _set_topmost(hwnd: int, topmost: bool) -> None:
    insert_after = ctypes.c_void_p(_HWND_TOPMOST if topmost else _HWND_NOTOPMOST)
    _set_window_pos(ctypes.c_void_p(hwnd), insert_after, 0, 0, 0, 0,
                     _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)


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


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260),
    ]


def _process_parents() -> dict:
    """pid -> parent pid, for every running process (Toolhelp snapshot)."""
    kernel32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x00000002
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot:
        return {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        out = {}
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return out
        while True:
            out[entry.th32ProcessID] = entry.th32ParentProcessID
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return out
    finally:
        kernel32.CloseHandle(snapshot)


def _window_for_pid(pid: int) -> int:
    """First visible top-level window owned by this exact process, or 0."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        owner_pid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _window_by_title_substring(text: str, exclude_hwnd: int = 0) -> int:
    """First visible top-level window whose title contains `text` (case-insensitive).

    `exclude_hwnd` skips our own widget window. Without it this can match the widget
    itself: its title is "AI Session Buddy", which contains any session labeled
    e.g. "Session Buddy" as a substring — confirmed live as the actual cause of
    "jumping" to a same/similar-named session silently doing nothing (it was
    foregrounding the widget onto itself, a no-op since it's already visible/topmost).
    """
    user32 = ctypes.windll.user32
    found = []
    needle = text.lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if hwnd == exclude_hwnd or not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _foreground(hwnd: int) -> None:
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg_hwnd = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
    cur_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(cur_thread, fg_thread, True)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.AttachThreadInput(cur_thread, fg_thread, False)


def _jump_to_process_tree(pid: int) -> None:
    """Finds a window for `pid` with no title involved at all: walks up the process
    ancestry for the first visible top-level window, then falls back to
    AttachConsole for a classic console. Used when title matching isn't available
    or came up empty -- e.g. an unlabeled Codex session (no fixed placeholder title
    the way Claude Code has "Claude Code"), see jump_to_session.

    NOTE: confirmed unreliable in practice, same as documented in _jump_to_pid
    below — the intermediate ancestor between a shell and its real terminal window
    is commonly already dead by the time we look (Windows' ParentProcessID is a
    creation-time snapshot, not a maintained live link), and AttachConsole can find
    a real but *hidden* console (Windows auto-allocates one for native
    console-subsystem children even under a pty-less shell) whose foregrounding is
    a silent no-op. Kept as the best available signal, not a guaranteed jump.
    """
    kernel32 = ctypes.windll.kernel32

    parents = _process_parents()
    walk_pid, seen = pid, set()
    while walk_pid and walk_pid not in seen:
        seen.add(walk_pid)
        hwnd = _window_for_pid(walk_pid)
        if hwnd:
            _foreground(hwnd)
            return
        walk_pid = parents.get(walk_pid, 0)

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return
    hwnd = kernel32.GetConsoleWindow()
    kernel32.FreeConsole()
    if hwnd:
        _foreground(hwnd)


def _jump_to_pid(pid: int, label: str = "", own_hwnd: int = 0) -> None:
    """Brings the terminal window hosting a Claude Code process to the foreground.

    The pid from the session registry is the CLI process itself, which has no window
    of its own. In rough order of reliability:
    - Claude Code sets the terminal's title to the session's own AI-generated label
      (confirmed live: a session named "ERNTER" shows up as a window titled
      "✳ ERNTER"). If we know the label, matching the window title directly skips
      process-tree guessing entirely and is exact.
    - Before naming happens, Claude Code sets the same title to the literal
      placeholder "Claude Code" instead (confirmed live: a freshly opened, still
      unnamed session shows up as "✳ Claude Code"). Try that when there's no label
      yet. Ambiguous if more than one session is unnamed at once — picks whichever
      matching window EnumWindows finds first.
    - Otherwise fall back to _jump_to_process_tree (ancestor walk, then
      AttachConsole) — see its own docstring for why that's a best-effort fallback,
      not a guarantee.
    """
    hwnd = (_window_by_title_substring(label, own_hwnd) if label
            else _window_by_title_substring("Claude Code", own_hwnd))
    if hwnd:
        _foreground(hwnd)
        return
    _jump_to_process_tree(pid)


def _pick_jump_session(sessions, pid: int):
    """Picks which Session a pid shared by more than one refers to.

    /clear keeps the same terminal (same pid) but starts a fresh session id;
    the old Session object lingers with status="ended" until archived. Among
    sessions sharing a pid, the ended one is stale — prefer a live one, and
    among ties the most recently updated, so the jump targets the window's
    current title instead of a name Claude Code has already overwritten.
    """
    candidates = [s for s in sessions if s.pid == pid]
    if not candidates:
        return None
    live = [s for s in candidates if s.status != "ended"]
    return max(live or candidates, key=lambda s: s.last_ts)


def _pick_jump_label(sessions, pid: int) -> str:
    session = _pick_jump_session(sessions, pid)
    return session.label if session else ""


def _light_mode() -> bool:
    """Whether Windows apps are set to light mode (Settings > Colors > Choose your mode)."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(value)
    except OSError:
        return False


class Api:
    """Note: attribute names starting with '_' are never walked into pywebview's
    JS-bridge introspection, which matters here — exposing the raw window/handle
    as a plain attribute makes older pywebview versions recurse into WinForms
    internals while building window.pywebview.api and crash."""

    def __init__(self, monitor: Monitor):
        self._monitor = monitor
        self._window = None
        self._hwnd: Optional[int] = None
        self._on_top = True

    def poll(self, after_seq: int = 0) -> dict:
        try:
            self._monitor.tick()
            return self._monitor.drain(after_seq or 0)
        except Exception as exc:  # the feed must never take the window down with it
            return {"error": str(exc)}

    def get_theme(self) -> dict:
        return {"accent": _accent_color_hex(), "light": _light_mode()}

    def set_on_top(self, flag: bool) -> None:
        self._on_top = bool(flag)
        try:
            if self._hwnd:
                _set_topmost(self._hwnd, self._on_top)
        except Exception:
            logging.getLogger("claude_session_feed").exception("set_on_top failed")

    def reassert_on_top(self) -> None:
        """WinForms can silently drop the native topmost style across a minimize/maximize/
        restore cycle without updating anything we track — reapply our last known desired
        state. Called from the restored/maximized window events (see main())."""
        if self._hwnd and self._on_top:
            _set_topmost(self._hwnd, True)

    def jump_to_session(self, pid: int) -> None:
        try:
            pid = int(pid)
            session = _pick_jump_session(self._monitor.sessions.values(), pid)
            label = session.label if session else ""
            if session is not None and session.source == "codex":
                # Codex never calls the Win32 SetConsoleTitleW API (confirmed
                # empirically, see codex_monitor.py), but under a pty-hosted
                # terminal it still sets the window title itself via an ANSI OSC
                # escape written over the pty -- confirmed live: a session
                # labeled "Plane morgen" showed up as a Git-Bash/mintty window
                # titled "Plane morgen | Projects". Unlike Claude Code there is
                # no fixed placeholder title for an unlabeled session, so only
                # try this when we actually have a label.
                hwnd = _window_by_title_substring(label, self._hwnd or 0) if label else 0
                if hwnd:
                    _foreground(hwnd)
                    return
                _jump_to_process_tree(pid)
                return
            _jump_to_pid(pid, label, own_hwnd=self._hwnd or 0)
        except Exception:
            logging.getLogger("claude_session_feed").exception("jump_to_session failed for pid %s", pid)

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        if not self._window:
            return
        self._maximized = not getattr(self, "_maximized", False)
        if self._maximized:
            self._window.maximize()
        else:
            self._window.restore()

    def resize_width(self, width: int) -> None:
        """Resizes by dragging the left edge, keeping the top-right corner fixed
        (the window stays docked to the right screen edge)."""
        if not self._window:
            return
        from webview.window import FixPoint

        self._window.resize(max(320, int(width)), self._window.height, FixPoint.NORTH | FixPoint.EAST)

    def quit(self) -> None:
        if self._window:
            self._window.destroy()


def main() -> None:
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")
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
    window.events.restored += lambda *_: api.reassert_on_top()
    window.events.maximized += lambda *_: api.reassert_on_top()
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
