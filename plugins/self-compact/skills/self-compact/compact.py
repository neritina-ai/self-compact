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

Injection transport:
  Primary path is focus-free console injection -- AttachConsole() to the
  hosting `claude.exe` process plus WriteConsoleInputW() into its CONIN$
  input buffer. This needs no foreground/focus switch, doesn't go through an
  IME, and works even when Claude Code runs in an elevated legacy conhost
  (where the old clipboard-paste-via-SetForegroundWindow path failed
  silently). Because AttachConsole requires FreeConsole first -- which kills
  the caller's stdout -- the actual attach+write happens in an isolated
  subprocess (this script with --console-inject) whose result is read back
  from a temp file, so the parent's and watcher's own consoles survive.
  If console injection can't run (no console pid resolved) or fails, we fall
  back to the legacy clipboard Ctrl+V + SetForegroundWindow path.

Usage:
    uv run --script compact.py ["summary prompt"]
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
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


# --- console-injection (AttachConsole + WriteConsoleInputW) plumbing ---------

KEY_EVENT = 0x0001
VK_RETURN = 0x0D


class _CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", wt.WCHAR), ("AsciiChar", ctypes.c_char)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("uChar", _CHAR_UNION),
        ("dwControlKeyState", wt.DWORD),
    ]


class _EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", _KEY_EVENT_RECORD)]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", _EVENT_UNION)]


def _key_records(ch: str, vk: int = 0) -> list:
    recs = []
    for down in (True, False):
        r = _INPUT_RECORD()
        r.EventType = KEY_EVENT
        r.Event.KeyEvent.bKeyDown = down
        r.Event.KeyEvent.wRepeatCount = 1
        r.Event.KeyEvent.wVirtualKeyCode = vk
        r.Event.KeyEvent.wVirtualScanCode = 0
        r.Event.KeyEvent.uChar.UnicodeChar = ch
        r.Event.KeyEvent.dwControlKeyState = 0
        recs.append(r)
    return recs


def _write_records(k32, hin, records) -> tuple[int, int]:
    arr = (_INPUT_RECORD * len(records))(*records)
    written = wt.DWORD(0)
    ok = k32.WriteConsoleInputW(hin, arr, len(records), ctypes.byref(written))
    return ok, written.value


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


def resolve_claude_pid() -> int | None:
    """Walk this process's ancestry to find the hosting `claude.exe`.

    The skill always runs as a tool subprocess of the Claude Code session that
    invoked it, so the nearest `claude.exe` ancestor is exactly the process
    attached to the target terminal's console -- which is what we want to
    AttachConsole() to. This is more reliable than the terminal window's pid:
    for Windows Terminal that pid is WindowsTerminal.exe (hosting many tabs),
    not the shell/claude on the ConPTY we care about.
    """
    try:
        cur = psutil.Process(os.getpid())
    except Exception:
        return None
    while cur is not None:
        try:
            if cur.name().lower() == "claude.exe":
                return cur.pid
        except Exception:
            pass
        try:
            cur = cur.parent()
        except Exception:
            cur = None
    return None


def resolve_console_pid(target: Candidate | None) -> int | None:
    """PID whose console we attach to for focus-free injection."""
    pid = resolve_claude_pid()
    if pid:
        return pid
    # Legacy conhost fallback: for ConsoleWindowClass the window pid IS the
    # attached console app (shell), which shares its console with claude.
    if target and target.cls == "ConsoleWindowClass" and target.pid:
        return target.pid
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


def run_console_inject(
    console_pid: int,
    text: str,
    with_enter: bool,
    enter_delay: float,
    result_file: str,
) -> int:
    """Subprocess body: AttachConsole(console_pid) + WriteConsoleInputW(text).

    Runs FreeConsole first (required by AttachConsole) which destroys this
    process's stdout, so all findings are written to `result_file` for the
    parent to read. Enter is sent as a separate write after a short delay so
    a slash-command autocomplete popup can't swallow it.
    """
    k32 = ctypes.windll.kernel32
    res: dict[str, object] = {"console_pid": console_pid}
    rc = 1
    try:
        k32.FreeConsole()
        attach_ok = k32.AttachConsole(console_pid)
        res["attach_ok"] = int(bool(attach_ok))
        if not attach_ok:
            res["attach_err"] = ctypes.get_last_error()
        else:
            GENERIC_READ = 0x80000000
            GENERIC_WRITE = 0x40000000
            FILE_SHARE_READ = 1
            FILE_SHARE_WRITE = 2
            OPEN_EXISTING = 3
            hin = k32.CreateFileW(
                "CONIN$",
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            res["conin_handle"] = hin
            if hin in (0, -1):
                res["conin_err"] = ctypes.get_last_error()
                res["success"] = 0
            else:
                text_records: list = []
                for ch in text:
                    text_records.extend(_key_records(ch))
                ok1, w1 = _write_records(k32, hin, text_records)
                res["text_write_ok"] = int(bool(ok1))
                res["text_written"] = w1
                res["text_expected"] = len(text_records)

                ok2, w2, expected2 = 1, 0, 0
                if with_enter:
                    time.sleep(enter_delay)
                    enter_records = _key_records("\r", VK_RETURN)
                    expected2 = len(enter_records)
                    ok2, w2 = _write_records(k32, hin, enter_records)
                    res["enter_write_ok"] = int(bool(ok2))
                    res["enter_written"] = w2
                    res["enter_expected"] = expected2

                success = (
                    bool(ok1)
                    and w1 == len(text_records)
                    and (not with_enter or (bool(ok2) and w2 == expected2))
                )
                res["success"] = int(success)
                rc = 0 if success else 1
    except Exception as e:  # pragma: no cover - defensive
        res["exception"] = repr(e)
    finally:
        try:
            k32.FreeConsole()
        except Exception:
            pass
        try:
            with open(result_file, "w", encoding="utf-8") as f:
                for kk, vv in res.items():
                    f.write(f"{kk}={vv}\n")
        except Exception:
            pass
    return rc


def console_inject(
    console_pid: int,
    text: str,
    with_enter: bool,
    enter_delay: float = 0.15,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """Spawn the isolated --console-inject subprocess and read its result.

    Returns (ok, detail). `detail` is the flattened result file (or an error)
    for logging.
    """
    result_file = os.path.join(
        tempfile.gettempdir(),
        f"self-compact-inject-{os.getpid()}-{int(time.time() * 1000)}.txt",
    )
    args = [
        sys.executable,
        os.path.abspath(__file__),
        "--console-inject",
        "--console-pid", str(console_pid),
        "--result-file", result_file,
        "--enter-delay", str(enter_delay),
        "--text", text,
    ]
    if with_enter:
        args.append("--enter")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=creationflags,
        )
    except Exception as e:
        return False, f"subprocess error: {e!r}"

    detail = ""
    try:
        with open(result_file, "r", encoding="utf-8") as f:
            detail = f.read().strip().replace("\n", " ")
    except Exception:
        return False, "no result file written"
    finally:
        try:
            os.remove(result_file)
        except Exception:
            pass

    return ("success=1" in detail), detail


def clipboard_inject(
    hwnd: int, text: str, with_enter: bool, activate_delay: float = 0.25
) -> bool:
    """Fallback transport: clipboard Ctrl+V + SetForegroundWindow.

    Pasting via Ctrl+V is a single atomic input event, which avoids the
    per-character keystroke racing we saw with SendInput typing (long prompts
    could be truncated mid-stream). Fails silently if foreground activation is
    denied (e.g. elevated conhost) -- which is exactly why console injection is
    preferred.
    """
    backup = _get_clipboard_text()
    try:
        _set_clipboard_text(text)
        activate(hwnd)
        time.sleep(activate_delay)
        send_keys("^v")
        if with_enter:
            time.sleep(0.2)
            send_keys("{ENTER}")
        return True
    except Exception:
        return False
    finally:
        if backup is not None:
            try:
                _set_clipboard_text(backup)
            except Exception:
                pass


def inject(
    hwnd: int,
    console_pid: int | None,
    text: str,
    with_enter: bool,
    activate_delay: float = 0.25,
    enter_delay: float = 0.15,
) -> tuple[bool, str]:
    """Inject `text` into the target Claude Code terminal.

    Tries focus-free console injection first (works elevated, no foreground
    steal, IME-safe), then falls back to clipboard paste. Returns (ok, how).
    """
    if console_pid:
        ok, detail = console_inject(console_pid, text, with_enter, enter_delay=enter_delay)
        if ok:
            return True, f"console (pid {console_pid})"
        clip_ok = clipboard_inject(hwnd, text, with_enter, activate_delay)
        return clip_ok, f"clipboard fallback (console failed: {detail})"

    clip_ok = clipboard_inject(hwnd, text, with_enter, activate_delay)
    return clip_ok, "clipboard (no console pid resolved)"


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
    console_pid: int | None,
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
        "--console-pid", str(console_pid if console_pid else 0),
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
    console_pid: int | None,
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
        print(f"   console pid: {console_pid}", flush=True)
        print(f"   session:     {session_file}", flush=True)
        print(f"   timeout:     {timeout:.0f}s", flush=True)
        print(f"   log:         {log_path}", flush=True)
        print("=" * 70, flush=True)
    except Exception:
        pass

    emit(f"started hwnd={hwnd} console_pid={console_pid} session_file={session_file} baseline_offset={baseline_offset} timeout={timeout}s")
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
                            ok, how = inject(
                                hwnd, console_pid, continuation_prompt, with_enter=True
                            )
                        except Exception as e:
                            emit(f"inject failed: {e!r}")
                            return 3
                        if not ok:
                            emit(f"inject did not succeed (via {how})")
                            return 3
                        emit(f"continuation prompt injected via {how}. exiting.")
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
        help="Seconds to wait after activating the window, clipboard path (default: 0.25)",
    )
    parser.add_argument(
        "--enter-delay",
        type=float,
        default=0.15,
        help="Seconds between the text write and the Enter write, console path (default: 0.15)",
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
        "--no-console",
        action="store_true",
        help="Skip console (AttachConsole) injection; use only the clipboard path",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Internal: run as the watcher subprocess",
    )
    parser.add_argument(
        "--console-inject",
        action="store_true",
        help="Internal: run as the AttachConsole+WriteConsoleInputW subprocess",
    )
    parser.add_argument(
        "--console-pid",
        type=int,
        default=None,
        help="Internal: pid whose console to attach to for injection",
    )
    parser.add_argument(
        "--text",
        default=None,
        help="Internal (--console-inject): literal text to type",
    )
    parser.add_argument(
        "--enter",
        action="store_true",
        help="Internal (--console-inject): press Enter after the text",
    )
    parser.add_argument(
        "--result-file",
        default=None,
        help="Internal (--console-inject): file to write the injection result to",
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

    if args.console_inject:
        if args.console_pid is None or args.result_file is None or args.text is None:
            print("ERROR: --console-inject requires --console-pid, --text, --result-file", file=sys.stderr)
            return 2
        return run_console_inject(
            args.console_pid,
            args.text,
            with_enter=args.enter,
            enter_delay=args.enter_delay,
            result_file=args.result_file,
        )

    if args.watch:
        if args.hwnd is None or args.session_file is None:
            print("ERROR: --watch requires --hwnd and --session-file", file=sys.stderr)
            return 2
        return run_watcher(
            args.hwnd,
            args.console_pid,
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
    console_pid = None if args.no_console else resolve_console_pid(target)
    print(f"target: {target.describe()}")
    print(f"console pid: {console_pid}")
    print(f"inject: {command!r}")

    if args.dry_run:
        return 0

    project_dir = project_transcript_dir(os.getcwd())
    session_file = session_jsonl_path(project_dir)
    baseline_offset = os.path.getsize(session_file) if session_file else 0

    ok, how = inject(
        target.hwnd,
        console_pid,
        command,
        with_enter=not args.no_enter,
        activate_delay=args.activate_delay,
        enter_delay=args.enter_delay,
    )
    if not ok:
        print(f"ERROR: injection failed (via {how}).", file=sys.stderr)
        return 3
    print(f"injected via {how}.")

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
        console_pid,
        session_file,
        baseline_offset,
        args.timeout,
        args.continuation_prompt,
    )
    print(f"watcher spawned in its own console. log: {watcher_log_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
