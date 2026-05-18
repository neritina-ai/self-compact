# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "pywinauto>=0.6.8",
#   "psutil>=5.9",
#   "pywin32>=308",
# ]
# ///
"""
self-compact: inject `/compact` into the active Claude Code terminal, then
inject a `compact done` follow-up once the compaction finishes.

Flow:
  1. Find the live session JSONL (the most-recently-modified file under
     ~/.claude/projects/<sanitized-cwd>/) and record its current byte size.
  2. Type `/compact "<prompt>"` + Enter into the hosting Windows Terminal.
  3. Spawn a watcher (this same script with --watch) in a new console
     window. The watcher prints an explanatory banner so the user knows
     what the extra window is for, then tails the session JSONL past the
     recorded offset looking for a `compact_boundary` / `isCompactSummary`
     marker.
  4. Parent exits immediately so Claude Code can process the queued
     slash command at end-of-turn.
  5. When the watcher finds the marker, it types `compact done` + Enter
     into the same terminal so the model is re-prompted after the
     post-/compact idle state.

Usage:
    uv run --script compact.py ["summary prompt"]
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
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

DEFAULT_CONTINUATION_PROMPT = "compact done"
DEFAULT_WATCH_TIMEOUT = 600.0

COMPACT_MARKERS = (
    '"subtype":"compact_boundary"',
    '"isCompactSummary":true',
)


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


def _get_clipboard_text() -> str | None:
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


def _set_clipboard_text(text: str) -> None:
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


def inject_text(hwnd: int, text: str, with_enter: bool, activate_delay: float = 0.25) -> None:
    # Clipboard-paste path: pasting via Ctrl+V is a single atomic input event,
    # which avoids the per-character keystroke racing we saw with SendInput
    # typing (long prompts could be truncated mid-stream).
    backup = _get_clipboard_text()
    try:
        _set_clipboard_text(text)
        activate(hwnd)
        time.sleep(activate_delay)
        send_keys("^v")
        if with_enter:
            time.sleep(0.2)
            send_keys("{ENTER}")
    finally:
        if backup is not None:
            try:
                _set_clipboard_text(backup)
            except Exception:
                pass


def build_command(prompt: str) -> str:
    if not prompt:
        return "/compact"
    return f'/compact "{prompt}"'


def project_transcript_dir(cwd: str) -> str:
    """Map cwd to ~/.claude/projects/<sanitized>/ — Claude Code's per-project JSONL dir."""
    sanitized = cwd.replace("\\", "-").replace(":", "-").replace("/", "-")
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", sanitized)


def session_jsonl_path(project_dir: str) -> str | None:
    """Return the most-recently-modified .jsonl in project_dir (the live session)."""
    if not os.path.isdir(project_dir):
        return None
    best: tuple[float, str] | None = None
    for name in os.listdir(project_dir):
        if not name.endswith(".jsonl"):
            continue
        p = os.path.join(project_dir, name)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best else None


def watcher_log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "self-compact-watcher.log")


def spawn_watcher(
    hwnd: int,
    session_file: str,
    baseline_offset: int,
    timeout: float,
    continuation_prompt: str,
) -> None:
    """Spawn the watcher in its own visible console window so the user can see
    what it's waiting on. The watcher survives parent exit via its own process
    group and its own console."""
    args = [
        sys.executable,
        os.path.abspath(__file__),
        "--watch",
        "--hwnd", str(hwnd),
        "--session-file", session_file,
        "--baseline-offset", str(baseline_offset),
        "--timeout", str(timeout),
        "--continuation-prompt", continuation_prompt,
    ]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        )

    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def run_watcher(
    hwnd: int,
    session_file: str,
    baseline_offset: int,
    timeout: float,
    continuation_prompt: str,
) -> int:
    """Tail `session_file` past `baseline_offset` looking for a /compact marker,
    then inject the continuation prompt. Prints progress to its own console and
    appends to the log file."""

    # CREATE_NEW_CONSOLE gives the watcher its own console window, but Python's
    # sys.stdout/stderr were inherited from the parent (the shell that ran
    # `uv run`) — they point back at the launcher, not the new console. Reopen
    # them against CONOUT$ so the banner and progress prints actually appear in
    # the new window.
    try:
        _conout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stdout = _conout
        sys.stderr = _conout
    except Exception:
        pass

    log_path = watcher_log_path()

    def emit(msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [watcher pid={os.getpid()}] {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # Banner — explains the extra window to the user.
    try:
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(f"self-compact watcher (pid {os.getpid()})")
        except Exception:
            pass
        print("=" * 70, flush=True)
        print(" self-compact watcher", flush=True)
        print("=" * 70, flush=True)
        print(" Waiting for Claude Code's /compact to finish, then I'll type", flush=True)
        print(f" {continuation_prompt!r} + Enter into the Claude Code terminal", flush=True)
        print(" so the model resumes work.", flush=True)
        print("", flush=True)
        print(" You can leave this window alone — it closes itself when done.", flush=True)
        print(f" If it's still here long after /compact finished, close it manually.", flush=True)
        print(f"   target hwnd: {hwnd}", flush=True)
        print(f"   session:     {session_file}", flush=True)
        print(f"   timeout:     {timeout:.0f}s", flush=True)
        print(f"   log:         {log_path}", flush=True)
        print("=" * 70, flush=True)
    except Exception:
        pass

    emit(f"started hwnd={hwnd} session_file={session_file} baseline_offset={baseline_offset} timeout={timeout}s")
    emit(f"continuation prompt: {continuation_prompt!r}")

    start = time.time()
    poll_interval = 2.0
    settle_delay = 2.0
    offset = baseline_offset

    while time.time() - start < timeout:
        try:
            if os.path.exists(session_file):
                size = os.path.getsize(session_file)
                if size > offset:
                    with open(session_file, "rb") as fh:
                        fh.seek(offset)
                        chunk = fh.read(size - offset)
                    text = chunk.decode("utf-8", errors="replace")
                    hit = next((m for m in COMPACT_MARKERS if m in text), None)
                    if hit:
                        emit(f"compact marker detected: {hit!r} (read {len(chunk)} bytes)")
                        time.sleep(settle_delay)
                        if not win32gui.IsWindow(hwnd):
                            emit(f"target hwnd {hwnd} no longer valid; giving up")
                            return 2
                        try:
                            inject_text(hwnd, continuation_prompt, with_enter=True)
                        except Exception as e:
                            emit(f"inject failed: {e!r}")
                            return 3
                        emit("continuation prompt injected. exiting.")
                        return 0
                    offset = size
        except Exception as e:
            emit(f"poll error: {e!r}")
        time.sleep(poll_interval)

    emit(f"timed out after {timeout}s with no compact marker")
    return 1


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
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Skip spawning the post-/compact watcher (old fire-and-forget behavior)",
    )
    parser.add_argument(
        "--continuation-prompt",
        default=DEFAULT_CONTINUATION_PROMPT,
        help=f"Prompt the watcher types after /compact finishes (default: {DEFAULT_CONTINUATION_PROMPT!r})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_WATCH_TIMEOUT,
        help=f"Watcher: max seconds to wait for /compact (default: {DEFAULT_WATCH_TIMEOUT})",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Internal: run as the watcher subprocess",
    )
    parser.add_argument(
        "--session-file",
        default=None,
        help="Internal (watcher): JSONL session file to tail for compact markers",
    )
    parser.add_argument(
        "--baseline-offset",
        type=int,
        default=0,
        help="Internal (watcher): byte offset in session-file to start tailing from",
    )
    args = parser.parse_args()

    if args.watch:
        if args.hwnd is None or args.session_file is None:
            print("ERROR: --watch requires --hwnd and --session-file", file=sys.stderr)
            return 2
        return run_watcher(
            args.hwnd,
            args.session_file,
            args.baseline_offset,
            args.timeout,
            args.continuation_prompt,
        )

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

    project_dir = project_transcript_dir(os.getcwd())
    session_file = session_jsonl_path(project_dir)
    baseline_offset = os.path.getsize(session_file) if session_file else 0

    inject_text(
        target.hwnd,
        command,
        with_enter=not args.no_enter,
        activate_delay=args.activate_delay,
    )
    print("injected.")

    if args.no_enter or args.no_watch:
        return 0

    if session_file is None:
        print(
            "WARNING: could not locate a session JSONL in "
            f"{project_dir!r}; watcher not spawned.",
            file=sys.stderr,
        )
        return 0

    spawn_watcher(
        target.hwnd,
        session_file,
        baseline_offset,
        args.timeout,
        args.continuation_prompt,
    )
    print(f"watcher spawned in its own console. log: {watcher_log_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
