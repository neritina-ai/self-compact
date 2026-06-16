---
name: self-compact
description: Proactively trigger Claude Code's /compact command from inside the current turn. Use in long autonomous workflows when the conversation has grown long and you want to continue working with a fresh summary instead of the full transcript. Windows-only — injects keystrokes into the Claude Code terminal via focus-free console input. Do NOT use for short conversations, mid-tool-call when the next steps depend on the previous output, or when the user explicitly asked you to keep working uncompacted.
---

# self-compact

Inject `/compact` (optionally `/compact "<prompt>"`) + Enter into the terminal hosting this Claude Code session. Because slash commands are processed by the Claude Code app (not the model), the command is queued and executes after the current turn ends.

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

This skill is a native binary, `self-compact`, installed on `PATH` via `go install` (see the repo README). It has exactly two public forms.

Compact with no summary prompt:

```
self-compact
```

Compact with a summary prompt, read from stdin:

```bash
self-compact - <<'EOF'
keep the project goal, file layout, and remaining TODOs; drop tool output from earlier exploration
EOF
```

The prompt is read from **stdin** (the `-` form), not an argument — this avoids shell quoting, escaping, and length limits. Prefer the Bash-tool heredoc shown above; piping a prompt that contains CJK or special characters through PowerShell 5.1 can mangle the encoding. A bare `self-compact` never reads stdin, so it can't block waiting for input.

The summary prompt is optional. If you write one, stay at the category level — the example above is the right shape. Don't restate specific facts, paths, or directives from the conversation: the default summarization already preserves user instructions and pending work, so the prompt is for steering, not transcription. Shorter is safer — every detail you write here is a detail you can get wrong, and a short category-level hint is often better than a long specific one (or no prompt at all).

## After invoking

The binary prints three lines once `/compact` is queued in Claude Code's input:

```
target claude pid=<N>
injected /compact via console (pid <N>).
sidekick spawned (windowless, background).
```

It then spawns a detached, windowless sidekick (`DETACHED_PROCESS | CREATE_NO_WINDOW`, the same way the `continue-claude` watcher runs), so **no extra window appears**. The sidekick reports only to its log at `%TEMP%\self-compact-sidekick.log`.

The sidekick waits 8 seconds for your turn to end — long enough that `/compact` has dequeued and started compacting — then types `compact done` + Enter into the same terminal and exits. It does **not** watch for compaction to finish: text injected after your turn ends queues behind the running `/compact` and is delivered once the post-compaction idle state is reached. (Injecting *during* your turn would be swallowed by the active turn, which is why the sidekick waits first.) Treat that `compact done` message, which arrives as your next user prompt, as the cue to resume work with the freshly summarized context.

**End your turn immediately after invoking this skill.** The sidekick's fixed 8-second wait assumes your turn ends promptly; if you keep working past it, the `compact done` injection could land mid-turn and be lost. Don't queue more tool calls after the binary returns — just stop.

## Injection transport

Injection is focus-free. The binary walks its own process ancestry to the hosting `claude.exe`, then does `FreeConsole()` → `AttachConsole(claude_pid)` → `CreateFile("CONIN$")` → `WriteConsoleInputW()` straight into that console's input buffer. No window focus or foreground switch is needed, it doesn't go through an IME, and it works even when Claude Code runs in an **elevated** legacy conhost. The sidekick uses the same transport, but is handed the resolved pid as an argument — once the main process exits, the ancestry chain back to `claude.exe` is broken, so it cannot re-derive it.

## Failure modes

- **`command not found` / `self-compact is not recognized`** — the binary isn't installed, or `~/go/bin` isn't on `PATH`. Install it per the repo README (`go install github.com/neritina-ai/self-compact/cmd/self-compact@latest`); if it still can't be found, surface to the user.
- **`no claude.exe ancestor found`** — running outside a Claude Code session, or in a headless / non-Windows environment. The skill can't work here; surface the failure to the user.
- **`AttachConsole(...) failed`** — the target console couldn't be attached (rare). Surface to the user.
- **Injection succeeds but no compaction happens** — Claude Code's input box wasn't empty, so the typed `/compact` was appended after existing text and treated as a normal message. Keep the input empty (the typical autonomous case) and retry.
- **No `compact done` prompt arrives** — the sidekick's injection failed, or it fired mid-turn (you kept working past the 8-second delay) and was swallowed. Check its log at `%TEMP%\self-compact-sidekick.log`.
- **A stuck sidekick needs to be killed** — it has no window, so list and kill it via:
  ```powershell
  Get-CimInstance Win32_Process -Filter "Name='self-compact.exe'" |
    Where-Object { $_.CommandLine -like '*sidekick*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  ```
