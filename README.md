# self-compact

A Claude Code skill that lets the model proactively issue `/compact` against its
own session — useful for long, autonomous workflows where you aren't at the
keyboard to type the slash command yourself.

## Why this exists

`/compact` in Claude Code summarizes the current conversation so the model can
continue working with a leaner context. Today it can only be run by:

1. The user, typing `/compact` in the terminal.
2. Claude Code's auto-compact, which fires near the context limit.

Neither helps an autonomous run that has grown long but isn't yet at the limit,
and where the user has stepped away. Anthropic's recommended pattern there is
"start a new session" or "delegate to a subagent" — both of which discard the
current run's working state.

This skill is the third option: the model decides on its own that compaction is
appropriate, and injects `/compact` + Enter into the terminal hosting the session
via focus-free Win32 console input. Slash commands are processed by Claude Code
(not the model), so the injected command is queued and executes once the model's
current turn ends. A detached, windowless sidekick then types `compact done` a
few seconds later, so the run resumes automatically with the fresh summary.

## Requirements

- **Windows.** Injection uses Win32 console APIs (`AttachConsole` /
  `WriteConsoleInputW`). macOS/Linux are not supported.
- **Claude Code**, running in a console (Windows Terminal or legacy conhost).
  Works even when Claude Code runs elevated. No other agent will do: the program
  locates its host by walking up the process tree to a `claude.exe` ancestor, and
  the whole design rests on Claude Code queueing an injected slash command until
  the current turn ends.
- **[Go](https://go.dev/dl/) 1.21+** on your `PATH`. The skill runs from source
  with `go run`; there is no separate binary to install or keep up to date.
- **[Node.js](https://nodejs.org/)** to run `npx skills` for the install.

## Install

```
npx skills add neritina-ai/self-compact --skill self-compact -a claude-code -g -y
```

The [skills CLI](https://github.com/vercel-labs/skills) can install a skill into
any agent it knows about; the flags above pin it to a global Claude Code
install, which is the only one this skill can work in. Repeat the `-g` on the
update and uninstall commands below: without it they act on the current project
instead, and `remove` reports that there is nothing to remove.

That drops the skill in `~/.claude/skills/self-compact/`. The next session picks
it up, and the model uses it when the description matches — or you can invoke it
directly with `/self-compact`.

Nothing is compiled at install time. The first invocation runs
`go run .` on `scripts/self-compact.go`, which takes a few seconds; Go caches the
build, so later invocations take about a third of a second.

### To update

```
npx skills update self-compact -g
```

Go rebuilds by itself whenever the source changes, so there is no second step.

### To uninstall

```
npx skills remove self-compact -g
```

Everything self-compact writes lives in one directory, which nothing else uses
and which you can delete at any time:

```powershell
Remove-Item -Recurse -Force $env:LOCALAPPDATA\self-compact
```

### Coming from 0.3.x

Version 0.3 shipped as a Claude Code plugin plus a `go install`ed binary. Both
are replaced by the single `npx skills add` above, so remove them: open the
`/plugins` menu in Claude Code, select **self-compact**, choose **Uninstall**,
and drop its marketplace entry from the same menu — then delete the old binary
with `rm ~/go/bin/self-compact.exe`.

## How it works

1. Walk the current process's parent chain to the hosting `claude.exe` (the
   program runs as a descendant: `claude.exe → shell → go run → self-compact`).
2. `FreeConsole()` → `AttachConsole(claude_pid)` → open `CONIN$` →
   `WriteConsoleInputW()` the keystrokes `/compact` (plus the quoted prompt, if
   any) + Enter straight into that console's input buffer. No window focus,
   foreground switch, IME, or clipboard is involved — which is why it's invisible
   and works even against an elevated console.
3. The slash command lands in Claude Code's input queue and is processed once the
   current model turn ends — which is why the skill tells the model to **end its
   turn immediately after injecting**.
4. The program copies its own executable to
   `%LOCALAPPDATA%\self-compact\self-compact-sidekick.exe` and starts that copy
   detached and windowless (`DETACHED_PROCESS | CREATE_NO_WINDOW`), handing it the
   resolved pid. The sidekick waits 8 seconds for the turn to end, then injects
   `compact done` so the run resumes with the fresh summary. It logs to
   `sidekick.log` in that same directory.

Locating the host by process ancestry (rather than enumerating windows) is what
makes the injection focus-free and elevation-tolerant.

The sidekick runs from a copy because the program cannot count on its own
executable outliving it. Whenever the source has changed, `go run` links into a
fresh temporary directory and deletes it the moment the program exits: a sidekick
started from there would hold that file open, `go run`'s cleanup would fail, and
the command would exit non-zero even though the injection had worked. When the
source is unchanged Go runs the linked executable straight out of its build
cache, which does survive — but the program has no way to tell the two cases
apart, and the build cache is not somewhere anything should be spawned from in
any event. So it keeps its own copy. That copy is a single file, rewritten on
each run; copying ~2 MB costs about 3 ms.

## Caveats

- **Windows only.** No macOS/Linux support.
- **Sessions started with `claude --bg` cannot be reached.** They live behind a
  private ConPTY; `AttachConsole` still succeeds, so the injection reports
  success while nothing actually happens. Only a Claude Code with its own console
  can be injected into.
- **Pending text in the input box at injection time:** the typed `/compact` is
  appended after whatever is already there, so the input no longer starts with
  `/` and Claude Code treats it as a normal message instead of a slash command.
  Keep the input box empty (the typical autonomous case).
- **The turn must end promptly after invoking.** The sidekick's fixed 8-second
  wait assumes the model stops right after injecting; working past it can cause
  the `compact done` injection to land mid-turn and be lost.

## Development

The skill is a single Go file, `skills/self-compact/scripts/self-compact.go`,
with no dependencies beyond the Windows standard library. Install a working copy
straight from a clone:

```
npx skills add . --skill self-compact -a claude-code -g -y
```

To check that it can find the host console without actually compacting anything:

```
go -C skills/self-compact/scripts run . --dry-run
```

Written by **Claude Opus 5.0** (model ID `claude-opus-5`), Anthropic's Claude
Opus 5, driving Claude Code.

## License

MIT — see `LICENSE`.
