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

Fire-and-forget. The script types `/compact "<prompt>"` + Enter into the
hosting Windows Terminal and exits immediately. Claude Code processes the
slash command after the current model turn ends, so the model side must
end its turn right after this script returns.

Usage:
    uv run --script compact.py ["summary prompt"]
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


_SENDKEYS_META = {
    "{": "{{}",
    "}": "{}}",
    "(": "{(}",
    ")": "{)}",
    "+": "{+}",
    "^": "{^}",
    "%": "{%}",
    "~": "{~}",
    "[": "{[}",
    "]": "{]}",
}


def escape_for_send_keys(s: str) -> str:
    """Escape pywinauto.send_keys meta characters so the string types literally."""
    return "".join(_SENDKEYS_META.get(ch, ch) for ch in s)


def build_command(prompt: str) -> str:
    if not prompt:
        return "/compact"
    return f'/compact "{prompt}"'


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
        help="Type the command but do not press Enter (useful for testing)",
    )
    parser.add_argument(
        "--activate-delay",
        type=float,
        default=0.25,
        help="Seconds to wait after activating the window (default: 0.25)",
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

    activate(target.hwnd)
    time.sleep(args.activate_delay)

    keys = escape_for_send_keys(command)
    if not args.no_enter:
        keys += "{ENTER}"
    send_keys(keys, with_spaces=True)

    print("injected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
