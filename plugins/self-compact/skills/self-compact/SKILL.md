---
name: self-compact
description: Proactively trigger Claude Code's /compact command. Use when the conversation has grown long and you want to continue working with a fresh summary instead of the full transcript, especially in long autonomous workflows.
---

# self-compact

Inject a `/compact` (optionally `/compact "<prompt>"`) command into the terminal hosting this Claude Code session, and also a "compact done" prompt in queue, which you will receive after `/compact` command is done.

This skill is useful for autonomous workflows where the user isn't sitting at the keyboard to type `/compact` themselves. Claude Code's auto-compact only fires near the context limit; this skill lets you compact earlier, on your own judgment.

## How to invoke

This skill is a native binary, `self-compact`, installed on `PATH`. Invocation has two public forms, `self-compact` and `self-compact -`, the latter allowing you to provide a custom summary prompt via stdin. Both forms inject the same `/compact` command, so they have the same effect on Claude Code's context. The difference is whether you want to steer the summarization with a prompt or just rely on its default behavior.

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

The prompt is read from **stdin** (the `-` form), not an argument — this avoids shell quoting, escaping, and length limits. Prefer the Bash-tool heredoc shown above; piping a prompt that contains CJK or special characters through PowerShell 5.1 can mangle the encoding. A bare `self-compact` never reads stdin, so it won't block waiting for input.

The summary prompt is optional. If you write one, stay at the category level — the example above is the right shape. A short category-level hint is often better than a long list of specific facts, paths or directives from the conversation.

## After invoking

The binary prints three lines once `/compact` is queued in Claude Code's input:

```
target claude pid=<N>
injected /compact via console (pid <N>).
sidekick spawned (windowless, background).
```

Then a detached sidekick is spawned to inject a "compact done" message into the queue, which arrives as your next user prompt, as the cue to resume work with the freshly summarized context.

## Troubleshooting

**`command not found` / `self-compact is not recognized`** — the binary isn't installed, or isn't on `PATH`.

If a stuck sidekick needs to be killed, list and kill it via:

```powershell
Get-CimInstance Win32_Process -Filter "Name='self-compact.exe'" |
  Where-Object { $_.CommandLine -like '*sidekick*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Its log is at `%TEMP%\self-compact-sidekick.log`.
