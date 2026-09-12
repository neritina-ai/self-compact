---
name: self-compact
description: Proactively trigger Claude Code's /compact command. Use when the conversation has grown long and you want to continue working with a fresh summary instead of the full transcript, especially in long autonomous workflows.
---

# self-compact

Inject a `/compact` (optionally `/compact "<prompt>"`) command into the terminal hosting this Claude Code session, together with a queued "compact done" prompt that reaches you once `/compact` has finished.

This skill is useful for autonomous workflows where the user isn't sitting at the keyboard to type `/compact` themselves. Claude Code's auto-compact only fires near the context limit; this skill lets you compact earlier, on your own judgment.

## How to invoke

The skill is a small Go program in this skill's `scripts/` directory, run straight from source with `go run .` — nothing is installed or pre-built.

Run it from that directory. In the commands below, replace `<skill-dir>` with this skill's base directory, the one reported when the skill was loaded; forward slashes work on Windows, so `.../.claude/skills/self-compact` is a fine way to write it.

Compact with no summary prompt:

```bash
cd "<skill-dir>/scripts" && go run .
```

Compact with a summary prompt, read from stdin:

```bash
cd "<skill-dir>/scripts" && go run . - <<'EOF'
keep the project goal, file layout, and remaining TODOs; drop tool output from earlier exploration
EOF
```

Both forms inject the same `/compact` command, so they have the same effect on Claude Code's context. The difference is whether you want to steer the summarization with a prompt or just rely on its default behavior.

The prompt is read from **stdin** (the `-` form), not an argument — this avoids shell quoting, escaping, and length limits. Prefer the Bash-tool heredoc shown above; piping a prompt that contains CJK or special characters through PowerShell 5.1 can mangle the encoding. A bare `go run .` never reads stdin, so it won't block waiting for input.

The summary prompt is optional. If you write one, stay at the category level — the example above is the right shape. A short category-level hint is often better than a long list of specific facts, paths or directives from the conversation.

The first run compiles the program, which takes a few seconds; Go caches the build, so later runs take about a third of a second.

## After invoking

The program prints three lines once `/compact` is queued in Claude Code's input:

```
target claude pid=<N>
injected /compact via console (pid <N>).
sidekick spawned (windowless, background).
```

**End your turn as soon as you see them.** The queued `/compact` only runs once your turn ends, and the sidekick waits a fixed 8 seconds before typing its continuation — work past that and the "compact done" prompt can land mid-turn and be lost.

That continuation arrives as your next user prompt, and is the cue to resume work with the freshly summarized context.

## Troubleshooting

**`go: command not found` / `'go' is not recognized`** — [Go](https://go.dev/dl/) 1.21 or newer must be installed and on `PATH`.

**Check the target without compacting.** This resolves the hosting console and reports what it would do, injecting nothing:

```bash
cd "<skill-dir>/scripts" && go run . --dry-run
```

**`no claude.exe ancestor found`** — the command isn't running as a descendant of a console-hosted `claude.exe`. Sessions started with `claude --bg` live behind a private ConPTY and cannot be reached; note that injecting into one of those reports success while nothing actually happens.

If a stuck sidekick needs to be killed:

```powershell
Get-CimInstance Win32_Process -Filter "Name='self-compact-sidekick.exe'" |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Its log is at `%TEMP%\self-compact-sidekick.log`.
