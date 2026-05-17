# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "pywinauto>=0.6.8",
#   "psutil>=5.9",
#   "pywin32>=308",
# ]
# ///
"""
self-compact: inject `/compact` into the active Claude Code terminal.

The script identifies the Windows Terminal (or legacy conhost) window hosting
the Claude Code session that spawned this process, brings it to the foreground,
and pastes `/compact "<prompt>"` followed by Enter. Because slash commands are
processed by Claude Code (not the model), the command is queued and executed
once the current turn ends.

Usage:
    uv run --script compact.py ["summary prompt"] [--hwnd N] [--list] [--dry-run]
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from dataclasses import dataclass

import psutil

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

import win32clipboard
import win32con
import win32gui
import win32process
from pywinauto.keyboard import send_keys


TERMINAL_CLASSES = {
    "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
    "ConsoleWindowClass",  # legacy conhost
}


@dataclass
class Candidate:
    hwnd: int
    pid: int
    proc_name: str
    title: str
    cls: str

    def describe(self) -> str:
        return (
            f"hwnd={self.hwnd} pid={self.pid} proc={self.proc_name} "
            f"class={self.cls} title={self.title!r}"
        )


def enumerate_terminal_windows() -> list[Candidate]:
    out: list[Candidate] = []

    def cb(hwnd: int, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            cls = win32gui.GetClassName(hwnd)
            if cls not in TERMINAL_CLASSES:
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name = "?"
            title = win32gui.GetWindowText(hwnd)
            out.append(Candidate(hwnd=hwnd, pid=pid, proc_name=name, title=title, cls=cls))
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return out


def pick_candidate(candidates: list[Candidate]) -> Candidate | None:
    """Choose the most likely Claude Code terminal among visible terminal windows."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    fg = win32gui.GetForegroundWindow()
    for c in candidates:
        if c.hwnd == fg:
            return c

    cwd_base = os.path.basename(os.getcwd()).lower()
    keywords = ("claude", cwd_base) if cwd_base else ("claude",)
    for c in candidates:
        low = c.title.lower()
        if any(k and k in low for k in keywords):
            return c

    return None


def activate(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_thread = kernel32.GetCurrentThreadId()

    attached = False
    try:
        if fg_thread and fg_thread != cur_thread:
            attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        try:
            user32.SetFocus(hwnd)
        except Exception:
            pass
    finally:
        if attached:
            user32.AttachThreadInput(cur_thread, fg_thread, False)


def get_clipboard_text() -> str | None:
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return None
    try:
        try:
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        except TypeError:
            return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def set_clipboard_text(text: str) -> None:
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                return
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("could not open clipboard after 5 retries")


def build_command(prompt: str) -> str:
    if not prompt:
        return "/compact"
    escaped = prompt.replace("\\", "\\\\").replace('"', '\\"')
    return f'/compact "{escaped}"'


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject /compact into Claude Code")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="Optional summary prompt passed to /compact",
    )
    parser.add_argument(
        "--hwnd",
        type=int,
        default=None,
        help="Target a specific window HWND (skip auto-detection)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List terminal-window candidates and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command and target without injecting",
    )
    parser.add_argument(
        "--no-enter",
        action="store_true",
        help="Paste the command but do not press Enter (useful for testing)",
    )
    parser.add_argument(
        "--activate-delay",
        type=float,
        default=0.25,
        help="Seconds to wait after activating the window",
    )
    parser.add_argument(
        "--paste-delay",
        type=float,
        default=0.10,
        help="Seconds to wait between paste and Enter",
    )
    args = parser.parse_args()

    candidates = enumerate_terminal_windows()

    if args.list:
        if not candidates:
            print("(no terminal windows found)")
        for c in candidates:
            print(c.describe())
        return 0

    if args.hwnd:
        target = next((c for c in candidates if c.hwnd == args.hwnd), None)
        if not target:
            target = Candidate(hwnd=args.hwnd, pid=0, proc_name="?", title="?", cls="?")
    else:
        target = pick_candidate(candidates)

    if not target:
        print("ERROR: could not locate a Claude Code terminal window.", file=sys.stderr)
        if candidates:
            print("Candidates seen (none disambiguated):", file=sys.stderr)
            for c in candidates:
                print(f"  {c.describe()}", file=sys.stderr)
            print(
                "Re-run with --hwnd <N> to pick one explicitly, or focus the "
                "right window before retrying.",
                file=sys.stderr,
            )
        return 2

    command = build_command(args.prompt)
    print(f"target: {target.describe()}")
    print(f"inject: {command!r}")

    if args.dry_run:
        return 0

    backup = get_clipboard_text()
    try:
        set_clipboard_text(command)
        activate(target.hwnd)
        time.sleep(args.activate_delay)
        send_keys("^v")
        time.sleep(args.paste_delay)
        if not args.no_enter:
            send_keys("{ENTER}")
            time.sleep(0.05)
    finally:
        if backup is not None:
            try:
                set_clipboard_text(backup)
            except Exception:
                pass

    print("injected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
