# self-compact

A Claude Code plugin that lets the model proactively issue `/compact` against its own session — useful for long, autonomous workflows where you aren't at the keyboard to type the slash command yourself.

## Why this exists

`/compact` in Claude Code summarizes the current conversation so the model can continue working with a leaner context. Today it can only be run by:

1. The user, typing `/compact` in the terminal.
2. Claude Code's auto-compact, which fires near the context limit.

Neither helps an autonomous run that has grown long but isn't yet at the limit, and where the user has stepped away. Anthropic's recommended pattern there is "start a new session" or "delegate to a subagent" — both of which discard the current run's working state.

This plugin is the third option: the model decides on its own that compaction is appropriate, and injects `/compact` + Enter into the terminal hosting the session via focus-free Win32 console input. Slash commands are processed by Claude Code (not the model), so the injected command is queued and executes once the model's current turn ends. A detached, windowless sidekick then types `compact done` a few seconds later, so the run resumes automatically with the fresh summary.

## Requirements

- **Windows.** Injection uses Win32 console APIs (`AttachConsole` / `WriteConsoleInputW`). macOS/Linux are not supported.
- **Claude Code running in a console** (Windows Terminal or legacy conhost). Works even when Claude Code runs elevated.
- **[Go](https://go.dev/dl/) 1.26+** to install the binary, with `~/go/bin` on your `PATH`.

## Install

Two steps: install the binary, then register the plugin.

**1. Install the `self-compact` binary** (puts `self-compact.exe` in `~/go/bin`):

```
go install github.com/neritina-ai/self-compact/cmd/self-compact@latest
```

Make sure `~/go/bin` (the default `GOBIN`) is on your `PATH` so the skill can find `self-compact`. To verify it runs, type `self-compact` outside a session — it should print `no claude.exe ancestor found`, which confirms the binary works.

**2. Register and install the plugin** inside Claude Code:

```
/plugin marketplace add neritina-ai/self-compact
/plugin install self-compact@self-compact
```

The first command points Claude Code at this repo's `.claude-plugin/marketplace.json`; the second installs the bundled `self-compact` skill. The next session picks up the skill via its `SKILL.md` and uses it when the description matches.

To update: re-run `go install …@latest` for the binary and `/plugin update` for the skill. To uninstall: `/plugin uninstall self-compact@self-compact`, then delete `~/go/bin/self-compact.exe`.

## How it works

1. Walk the current process's parent chain to the hosting `claude.exe` (the binary runs as a descendant: `claude.exe → shell → self-compact`).
2. `FreeConsole()` → `AttachConsole(claude_pid)` → open `CONIN$` → `WriteConsoleInputW()` the keystrokes `/compact` (plus the quoted prompt, if any) + Enter straight into that console's input buffer. No window focus, foreground switch, IME, or clipboard is involved — which is why it's invisible and works even against an elevated console.
3. The slash command lands in Claude Code's input queue and is processed once the current model turn ends — which is why the skill tells the model to **end its turn immediately after injecting**.
4. The binary spawns itself as a detached, windowless sidekick (`DETACHED_PROCESS | CREATE_NO_WINDOW`), handing it the resolved pid. The sidekick waits 8 seconds for the turn to end, then injects `compact done` so the run resumes with the fresh summary. It logs to `%TEMP%\self-compact-sidekick.log`.

Locating the host by process ancestry (rather than enumerating windows) is what makes the injection focus-free and elevation-tolerant.

## Caveats

- **Windows only.** No macOS/Linux support.
- **Pending text in the input box at injection time:** the typed `/compact` is appended after whatever is already there, so the input no longer starts with `/` and Claude Code treats it as a normal message instead of a slash command. Keep the input box empty (the typical autonomous case).
- **The turn must end promptly after invoking.** The sidekick's fixed 8-second wait assumes the model stops right after injecting; working past it can cause the `compact done` injection to land mid-turn and be lost.

## License

MIT — see `LICENSE`.
