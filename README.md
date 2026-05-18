# self-compact

A Claude Code skill that lets the model proactively issue `/compact` against its own session — useful for long, autonomous workflows where you aren't at the keyboard to type the slash command yourself.

## Why this exists

`/compact` in Claude Code summarizes the current conversation so the model can continue working with a leaner context. Today it can only be run by:

1. The user, typing `/compact` in the terminal.
2. Claude Code's auto-compact, which fires near the context limit.

Neither helps an autonomous run that has grown long but isn't yet at the limit, and where the user has stepped away. Anthropic's recommended pattern there is "start a new session" or "delegate to a subagent" — both of which discard the current run's working state.

This skill is the third option: the model decides on its own that compaction is appropriate, and uses [pywinauto](https://github.com/pywinauto/pywinauto) to inject `/compact "<summary prompt>"` + Enter into the hosting Windows Terminal. Slash commands are processed by Claude Code (not the model), so the injected command is queued and executes once the model's current turn ends.

## Requirements

- **Windows.** The injection path uses Win32 APIs (pywinauto, pywin32). macOS/Linux are not supported.
- **Windows Terminal** (`CASCADIA_HOSTING_WINDOW_CLASS`) or legacy conhost (`ConsoleWindowClass`). The script identifies the terminal window by class.
- **[uv](https://docs.astral.sh/uv/)** on `PATH`. The script declares its dependencies inline (PEP 723), so `uv run --script` creates an ephemeral environment on first use — no manual `pip install`.
- Python 3.9+. `uv` will fetch one if needed.

## Install

Inside Claude Code, register this repo as a plugin marketplace and install the skill:

```
/plugin marketplace add neritina-ai/self-compact
/plugin install self-compact@self-compact
```

The first command points Claude Code at this repo's `.claude-plugin/marketplace.json`; the second installs the bundled `self-compact` skill. The next session will pick up the skill via its `SKILL.md` frontmatter and use it when the description matches.

To update later, re-run `/plugin marketplace add neritina-ai/self-compact` (it refreshes) or use `/plugin update`. To uninstall, use `/plugin uninstall self-compact@self-compact`.

## Manual use

You can also invoke the script directly without going through the skill. The installed path depends on the marketplace name — find it with:

```powershell
Get-ChildItem -Recurse -Filter compact.py "$env:USERPROFILE\.claude\plugins\marketplaces" | Select-Object FullName
```

Then run it with `uv`:

```powershell
uv run --script "<full-path-from-above>" "keep the design notes; drop tool output"
```

Flags:

| Flag | Effect |
| --- | --- |
| `--dry-run` | Print the resolved target window and the command that would be injected, but don't inject. |
| `--list` | List all visible terminal windows the script can see. |
| `--hwnd N` | Skip auto-detection and target HWND `N`. |
| `--no-enter` | Type the command but don't press Enter — useful for verifying injection without firing the slash command. |
| `--activate-delay` | Seconds to wait after activating the window (default `0.25`). Raise if injection happens before the window has focus. |

## How it works

1. Enumerate visible top-level windows and keep ones whose window class is `CASCADIA_HOSTING_WINDOW_CLASS` or `ConsoleWindowClass`.
2. Pick a candidate: single match wins; otherwise prefer the foreground window, else a window whose title contains `claude` or the current cwd basename.
3. Bring the target window forward with `SetForegroundWindow` (using `AttachThreadInput` to bypass focus-stealing restrictions), then type `/compact "<prompt>"` + Enter via `pywinauto.keyboard.send_keys` (a single atomic `SendInput` batch — no clipboard involvement).
4. The slash command lands in Claude Code's input queue. Claude Code processes it once the current model turn ends, which is why the skill instructions tell the model to **end its turn immediately after a successful injection**.

The process tree doesn't help locate the terminal in modern Windows Terminal because shells launched via the "Default Terminal Application" setting are children of `explorer.exe`, not `WindowsTerminal.exe` — Windows Terminal hosts them via ConPTY rather than spawning them. That's why detection is window-class based rather than process-ancestor based.

## Caveats

- Multiple Windows Terminal windows: detection falls back to "foreground window" or "title contains keyword". If neither disambiguates, the script exits with the candidate list — re-run with `--hwnd N`.
- Pending text in Claude Code's input box at injection time: the typed `/compact` is appended after whatever is already there, so the input no longer starts with `/` and Claude Code treats it as a normal user message instead of a slash command. Workflows using this skill should keep the input box empty (the typical autonomous case).
- Focus theft: another foreground app between activation and keystroke send will swallow the keys. Raise `--activate-delay` if you see flaky results.

## License

MIT — see `LICENSE`.
