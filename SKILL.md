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

Run from any working directory (the script auto-locates the hosting Windows Terminal):

```
uv run --script "$env:USERPROFILE\.claude\skills\self-compact\compact.py" "<summary prompt>"
```

Bash / WSL form:

```
uv run --script "$HOME/.claude/skills/self-compact/compact.py" "<summary prompt>"
```

The summary prompt is optional but strongly recommended — it tells the post-compact model what to preserve. Example: `"keep the project goal, file layout, and remaining TODOs; drop tool output from earlier exploration"`.

### Useful flags

- `--dry-run` — print target + command, no injection. Use first if you're unsure.
- `--list` — list candidate terminal windows.
- `--hwnd N` — target a specific HWND (use `--list` to find it).
- `--no-enter` — paste but don't press Enter (test the paste mechanism).

## After invoking

The script reports `injected.` on success. At that point `/compact "..."` is sitting in Claude Code's input queue.

**You must end your turn immediately after a successful injection.** Do not make further tool calls, do not write more text. The queued slash command fires only when the current turn ends. If you keep working, you'll add to the transcript that's about to be compacted, wasting the injection.

A good last line is one sentence stating you've queued the compaction.

## Failure modes

- **"could not locate a Claude Code terminal window"** — running in a headless / non-Windows environment, or no visible terminal. Skill can't work here; surface the failure to the user.
- **Multiple candidates, no match** — script lists them and exits. Re-run with `--hwnd N` for the right one.
- **Injection succeeds but no compaction happens** — focus was stolen before Enter landed, or Claude Code's input wasn't empty. Retry once; if still failing, surface to the user.
