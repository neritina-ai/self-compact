---
name: self-compact
description: Proactively trigger Claude Code's /compact command from inside the current turn. Use in long autonomous workflows when the conversation has grown long and you want to continue working with a fresh summary instead of the full transcript. Windows-only — injects keystrokes into the Claude Code terminal via pywinauto. Do NOT use for short conversations, mid-tool-call when the next steps depend on the previous output, or when the user explicitly asked you to keep working uncompacted.
---

# self-compact

Inject `/compact "<prompt>"` + Enter into the Windows Terminal hosting this Claude Code session. Because slash commands are processed by the Claude Code app (not the model), the command is queued and executes after the current turn ends.

This skill exists for autonomous workflows where the user isn't sitting at the keyboard to type `/compact` themselves. Claude Code's auto-compact only fires near the context limit; this skill lets you compact earlier, on your own judgment.

## When to use

- Long autonomous run where context is bloating with stale tool output
- Finished a self-contained sub-task (debugging, exploration, scaffolding) and the next sub-task doesn't need that detail
- Conversation has > ~50% context used and the upcoming work is large

## When NOT to use

- Short conversation — compacting adds latency and discards useful nuance
- Mid-debug where the next step references prior tool output
- User explicitly asked you to keep working uncompacted
- You can spawn a subagent or open a fresh session instead

## How to invoke

The script lives next to this `SKILL.md` as `compact.py`. Use the **base directory for this skill** (Claude Code shows it in the skill's system reminder when the skill loads) as `<skill-dir>` below, and run from any working directory — the script auto-locates the hosting Windows Terminal.

PowerShell:

```
uv run --script "<skill-dir>/compact.py" "<summary prompt>"
```

Bash / WSL:

```
uv run --script "<skill-dir>/compact.py" "<summary prompt>"
```

Do not hardcode `~/.claude/skills/self-compact/compact.py` — when installed as a plugin the skill lives under `~/.claude/plugins/marketplaces/<marketplace>/skills/self-compact/`, not the bare skills dir.

The summary prompt is optional. If you write one, stay at the category level — `"keep the project goal, file layout, and remaining TODOs; drop tool output from earlier exploration"` is the right shape. Don't restate specific facts, paths, or directives from the conversation: the default summarization already preserves user instructions and pending work, so the prompt is for steering, not transcription. Shorter is safer — every detail you write here is a detail you can get wrong, and a short category-level hint is often better than a long specific one (or no prompt at all).

### Useful flags

- `--dry-run` — print target + command, no injection. Use first if you're unsure.
- `--list` — list candidate terminal windows.
- `--hwnd N` — target a specific HWND (use `--list` to find it).
- `--no-enter` — type the command but don't press Enter (test injection without firing).
- `--no-console` — skip console (AttachConsole) injection and use only the clipboard path.
- `--sidekick-delay N` — seconds the sidekick waits before injecting `compact done` (default 8). Raise it if your turn won't end promptly.
- `--no-sidekick` — inject `/compact` only; don't spawn the resume sidekick (fire-and-forget).

## After invoking

The script prints `injected via console (pid N).` (or `injected via clipboard fallback (...)`) once `/compact "..."` is queued in Claude Code's input, then `sidekick spawned in its own console.` — a separate console window opens that explains itself. The extra window is the price of reliability; we tried hiding it with pythonw and it was too fragile.

The sidekick waits a few seconds (default 8) for your turn to end — long enough that `/compact` has dequeued and started compacting — then types `compact done` + Enter into the same terminal and closes itself. It does **not** watch for compaction to finish: text injected after your turn ends queues behind the running `/compact` and is delivered once the post-compaction idle state is reached. (Injecting *during* your turn would be swallowed by the active turn, which is why the sidekick waits first.) Treat that `compact done` message, which arrives as your next user prompt, as the cue to resume work with the freshly summarized context.

**End your turn immediately after invoking this skill.** The sidekick's fixed wait assumes your turn ends promptly; if you keep working past the delay, the `compact done` injection could land mid-turn and be lost. Don't queue more tool calls after the skill returns — just stop.

## Injection transport

Keystrokes reach Claude Code by one of two paths, tried in order:

1. **Console injection (primary, focus-free).** The script finds the hosting `claude.exe` by walking its own process ancestry, then in a short-lived isolated subprocess does `AttachConsole(claude_pid)` + `WriteConsoleInputW` straight into that console's input buffer. No window focus or foreground switch is needed, it doesn't go through an IME, and it works even when Claude Code runs in an **elevated** legacy conhost (where the clipboard path silently fails because a background process can't `SetForegroundWindow`). The attach happens in a subprocess because `AttachConsole` requires `FreeConsole` first, which would otherwise kill the parent's stdout.
2. **Clipboard paste (fallback).** If no console pid can be resolved or the console write fails, it falls back to clipboard `Ctrl+V` + `SetForegroundWindow`. Use `--no-console` to force this path.

Either way the script verifies the write succeeded before reporting success, instead of blindly printing `injected.`.

## Failure modes

- **"could not locate a Claude Code terminal window"** — running in a headless / non-Windows environment, or no visible terminal. Skill can't work here; surface the failure to the user.
- **Multiple candidates, no match** — script lists them and exits. Re-run with `--hwnd N` for the right one.
- **Injection succeeds but no compaction happens** — focus was stolen before Enter landed, or Claude Code's input wasn't empty. Retry once; if still failing, surface to the user.
- **No `compact done` prompt arrives** — the sidekick's injection failed, or it fired mid-turn (you kept working past the delay) and was swallowed. Its log lives at `%TEMP%\self-compact-sidekick.log`.
- **A stuck sidekick needs to be killed manually** — just close its console window (it has a self-describing title). Or list and kill via:
  ```powershell
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*compact.py*--sidekick*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  ```
