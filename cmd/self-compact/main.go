// Command self-compact injects Claude Code's /compact slash command into the
// terminal hosting the current session, then spawns a detached, windowless
// "sidekick" that types "compact done" once the turn has ended -- so an
// autonomous workflow resumes automatically after compaction.
//
// Two public forms:
//
//	self-compact        compact with no summary prompt
//	self-compact -      read the /compact summary prompt from stdin
//
// One internal form (spawned by the above):
//
//	self-compact sidekick <pid>   wait, then inject "compact done" into <pid>
//
// Injection is focus-free: it walks its own process ancestry to the hosting
// claude.exe, AttachConsole()s to it, and WriteConsoleInputW()s straight into
// that console's input buffer -- the same transport as the continue-claude
// watcher, and the reason no window focus is needed. The sidekick is launched
// with DETACHED_PROCESS|CREATE_NO_WINDOW on this native console-subsystem
// binary, so the OS never gives it a console window.
package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/neritina-ai/self-compact/internal/coninject"
)

const (
	// Windows CreateProcess flags. DETACHED_PROCESS gives the child no console
	// at all; CREATE_NO_WINDOW suppresses a window for any console it might
	// otherwise get. Together, on a native console-subsystem exe, the sidekick
	// runs truly windowless -- the same flags the continue-claude watcher uses.
	detachedProcess = 0x00000008
	createNoWindow  = 0x08000000

	// enterDelay is the pause between typing the command text and pressing Enter,
	// long enough for Claude Code's input to register the text first.
	enterDelay = 150 * time.Millisecond

	// sidekickDelay is how long the sidekick waits before injecting the
	// continuation. It must outlast the gap between this process returning and
	// the model ending its turn; once the turn ends, a later injection (even
	// mid-compaction) survives and is delivered at the post-/compact idle state.
	sidekickDelay = 8 * time.Second

	// continuationText is injected by the sidekick after compaction. It arrives
	// as the next user prompt and is the model's cue to resume with the fresh
	// summary.
	continuationText = "compact done"
)

const usage = `usage:
  self-compact        inject /compact with no summary prompt
  self-compact -      read the /compact summary prompt from stdin`

func main() {
	args := os.Args[1:]
	if len(args) >= 1 && args[0] == "sidekick" {
		fail("sidekick", runSidekick(args[1:]))
		return
	}
	fail("self-compact", runCompact(args))
}

func fail(name string, err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, name+":", err)
		os.Exit(1)
	}
}

// runCompact injects /compact (optionally with a stdin-supplied prompt) into the
// hosting Claude Code console, then spawns the resume sidekick.
func runCompact(args []string) error {
	var prompt string
	switch {
	case len(args) == 0:
		// No prompt. Never touches stdin, so a bare invocation can't block
		// waiting for input that will never come.
	case len(args) == 1 && args[0] == "-":
		data, err := io.ReadAll(os.Stdin)
		if err != nil {
			return fmt.Errorf("read prompt from stdin: %w", err)
		}
		// /compact must stay on a single line; a stray newline would submit the
		// command early, before the rest of the prompt is typed.
		prompt = strings.NewReplacer("\r\n", " ", "\r", " ", "\n", " ").
			Replace(strings.TrimSpace(string(data)))
	default:
		return fmt.Errorf("unrecognized arguments %v\n%s", args, usage)
	}

	pid, err := coninject.FindClaudePID()
	if err != nil {
		return err
	}
	fmt.Printf("target claude pid=%d\n", pid)

	// Inject /compact in-process. coninject.Inject calls FreeConsole, which
	// detaches us from the console -- harmless here because this skill is invoked
	// via a tool whose stdout/stderr are pipes (FreeConsole leaves redirected
	// handles intact), and we exit right after. A slash command is handled by the
	// Claude Code app, not the model, so it queues and fires once this turn ends.
	if err := coninject.Inject(pid, compactSteps(prompt)); err != nil {
		return fmt.Errorf("inject /compact: %w", err)
	}
	fmt.Printf("injected /compact via console (pid %d).\n", pid)

	// Then spawn the windowless resume sidekick, handing it the pid we just
	// resolved (it cannot re-derive it: once we exit, the ancestry chain back to
	// claude.exe is broken).
	if err := spawnSidekick(pid); err != nil {
		return fmt.Errorf("spawn sidekick: %w", err)
	}
	fmt.Println("sidekick spawned (windowless, background).")
	return nil
}

// compactSteps builds the keystroke sequence for the /compact command. With no
// prompt it is just "/compact"; with one it is /compact "<prompt>" (the prompt
// wrapped in double quotes).
func compactSteps(prompt string) []coninject.Step {
	cmd := "/compact"
	if prompt != "" {
		cmd = `/compact "` + prompt + `"`
	}
	return []coninject.Step{
		coninject.Text(cmd),
		coninject.Delay(enterDelay),
		coninject.Enter(),
	}
}

// spawnSidekick launches this same binary as a detached, windowless sidekick
// that will wait and then inject the continuation into pid.
func spawnSidekick(pid uint32) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	cmd := exec.Command(exe, "sidekick", strconv.FormatUint(uint64(pid), 10))
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: detachedProcess | createNoWindow}
	return cmd.Start()
}

// runSidekick waits for the model's turn to end, then injects the continuation
// prompt into the target console and exits. It detects nothing: text injected
// after turn-end queues behind the running /compact and is delivered at the
// post-compaction idle state. It runs windowless, so its only trace is the log.
func runSidekick(args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("usage: self-compact sidekick <pid>")
	}
	pid64, err := strconv.ParseUint(args[0], 10, 32)
	if err != nil {
		return fmt.Errorf("bad pid %q: %w", args[0], err)
	}
	pid := uint32(pid64)

	time.Sleep(sidekickDelay)

	injErr := coninject.Inject(pid, []coninject.Step{
		coninject.Text(continuationText),
		coninject.Delay(enterDelay),
		coninject.Enter(),
	})
	logSidekick(pid, injErr)
	return injErr
}

// logSidekick appends the sidekick's outcome to %TEMP%\self-compact-sidekick.log.
// The sidekick is windowless with no usable stdout/stderr, so the log file is
// the only way to see whether the continuation injection succeeded.
func logSidekick(pid uint32, injErr error) {
	path := filepath.Join(os.TempDir(), "self-compact-sidekick.log")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	stamp := time.Now().Format("2006-01-02 15:04:05")
	if injErr != nil {
		fmt.Fprintf(f, "%s\tpid=%d\tinject failed: %v\n", stamp, pid, injErr)
		return
	}
	fmt.Fprintf(f, "%s\tpid=%d\tinjected %q\n", stamp, pid, continuationText)
}
