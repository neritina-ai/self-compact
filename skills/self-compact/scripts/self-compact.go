// Command self-compact injects Claude Code's /compact slash command into the
// terminal hosting the current session, then spawns a detached, windowless
// "sidekick" that types "compact done" once the turn has ended -- so an
// autonomous workflow resumes automatically after compaction.
//
// It is meant to be run straight from source, from this directory:
//
//	go run .            compact with no summary prompt
//	go run . -          read the /compact summary prompt from stdin
//	go run . --dry-run  report what would be injected, and inject nothing
//
// One internal form, used only when the program re-launches itself:
//
//	self-compact-sidekick.exe sidekick <pid>
//
// Injection is focus-free: the program walks its own process ancestry to the
// hosting claude.exe, AttachConsole()s to it, and WriteConsoleInputW()s straight
// into that console's input buffer. Only the Windows standard library is used,
// so `go run` never has to fetch a module or a newer toolchain.
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
	"unsafe"
)

const (
	// Windows CreateProcess flags. DETACHED_PROCESS gives the child no console
	// at all; CREATE_NO_WINDOW suppresses a window for any console it might
	// otherwise get. Together, on a native console-subsystem exe, the sidekick
	// runs truly windowless.
	detachedProcess = 0x00000008
	createNoWindow  = 0x08000000

	// enterDelay is the pause between typing a line and pressing Enter, long
	// enough for Claude Code's input to register the text first.
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

	// sidekickExe is the name of the stable copy this program spawns as its
	// sidekick. See sidekickBinary for why a copy is needed.
	sidekickExe = "self-compact-sidekick.exe"
)

const usage = `usage (run from this directory):
  go run .            inject /compact with no summary prompt
  go run . -          read the /compact summary prompt from stdin
  go run . --dry-run  report what would be injected, and inject nothing
                      (combines with the forms above)`

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
	if len(args) == 1 && (args[0] == "-h" || args[0] == "--help") {
		fmt.Println(usage)
		return nil
	}

	var (
		prompt string
		dryRun bool
	)
	rest := args
	if len(rest) > 0 && rest[0] == "--dry-run" {
		dryRun, rest = true, rest[1:]
	}
	switch {
	case len(rest) == 0:
		// No prompt. Never touches stdin, so a bare invocation cannot block
		// waiting for input that will never come.
	case len(rest) == 1 && rest[0] == "-":
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

	pid, err := findClaudePID()
	if err != nil {
		return err
	}
	fmt.Printf("target claude pid=%d\n", pid)

	command := compactCommand(prompt)
	if dryRun {
		fmt.Printf("dry run: would submit %s to pid %d, then spawn the sidekick.\n", command, pid)
		fmt.Println("nothing was injected.")
		return nil
	}

	// Inject /compact in-process. injectLine calls FreeConsole, which detaches us
	// from the console -- harmless here because this program is invoked via a
	// tool whose stdout/stderr are pipes (FreeConsole leaves redirected handles
	// intact), and we exit right after. A slash command is handled by the Claude
	// Code app, not the model, so it queues and fires once this turn ends.
	if err := injectLine(pid, command); err != nil {
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

// compactCommand builds the line to submit. With no prompt it is just
// "/compact"; with one it is /compact "<prompt>" (the prompt in double quotes).
func compactCommand(prompt string) string {
	if prompt == "" {
		return "/compact"
	}
	return `/compact "` + prompt + `"`
}

// runSidekick waits for the model's turn to end, then injects the continuation
// prompt into the target console and exits. It detects nothing: text injected
// after turn-end queues behind the running /compact and is delivered at the
// post-compaction idle state. It runs windowless, so its only trace is the log.
func runSidekick(args []string) error {
	if len(args) != 1 {
		return fmt.Errorf("usage: %s sidekick <pid>", sidekickExe)
	}
	pid64, err := strconv.ParseUint(args[0], 10, 32)
	if err != nil {
		return fmt.Errorf("bad pid %q: %w", args[0], err)
	}
	pid := uint32(pid64)

	time.Sleep(sidekickDelay)

	injErr := injectLine(pid, continuationText)
	logSidekick(pid, injErr)
	return injErr
}

// logSidekick appends the sidekick's outcome to the log in %TEMP%. The sidekick
// is windowless with no usable stdout/stderr, so the log file is the only way to
// see whether the continuation injection succeeded.
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

// --- Spawning the sidekick --------------------------------------------------

// spawnSidekick launches a detached, windowless copy of this program that will
// wait and then inject the continuation into pid.
func spawnSidekick(pid uint32) error {
	exe, err := sidekickBinary()
	if err != nil {
		return err
	}
	cmd := exec.Command(exe, "sidekick", strconv.FormatUint(uint64(pid), 10))
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: detachedProcess | createNoWindow}
	return cmd.Start()
}

// sidekickBinary returns a path to this program's executable that will outlive
// this process, copying the running image there if needed.
//
// The copy is the price of `go run`: it builds to a temporary directory and
// deletes that directory as soon as the program exits. A sidekick launched from
// there would hold the file open, so the cleanup would fail ("Access is denied")
// and `go run` would exit non-zero even though the injection worked. Spawning
// from a stable copy leaves the temporary tree free to be removed on time.
func sidekickBinary() (string, error) {
	src, err := os.Executable()
	if err != nil {
		return "", err
	}
	base, err := os.UserCacheDir() // %LOCALAPPDATA% on Windows
	if err != nil {
		base = os.TempDir()
	}
	dir := filepath.Join(base, "self-compact")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}

	dst := filepath.Join(dir, sidekickExe)
	if err := copyFile(src, dst); err != nil {
		// A sidekick from a recent invocation is still running and holds the
		// file open. That copy is this same program, so reuse it as it is.
		if _, statErr := os.Stat(dst); statErr == nil {
			return dst, nil
		}
		return "", fmt.Errorf("copy sidekick binary to %s: %w", dst, err)
	}
	return dst, nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// --- Console keystroke injection --------------------------------------------
//
// Text is delivered by attaching to the target console and writing straight into
// its input buffer, which bypasses the windowing/UIPI layer entirely: no window
// focus, foreground switch, IME or clipboard is involved. That is also why it
// works across the normal->elevated integrity boundary, in the common case where
// the elevated console was launched by the same interactive user.

const (
	keyEvent  = 0x0001
	vkReturn  = 0x0D
	genericRW = syscall.GENERIC_READ | syscall.GENERIC_WRITE
	shareRW   = syscall.FILE_SHARE_READ | syscall.FILE_SHARE_WRITE
)

var (
	kernel32              = syscall.NewLazyDLL("kernel32.dll")
	procFreeConsole       = kernel32.NewProc("FreeConsole")
	procAttachConsole     = kernel32.NewProc("AttachConsole")
	procWriteConsoleInput = kernel32.NewProc("WriteConsoleInputW")
)

// keyEventRecord mirrors KEY_EVENT_RECORD. Field order and sizes must match the
// C struct exactly so the bytes land correctly in WriteConsoleInputW.
type keyEventRecord struct {
	bKeyDown          int32 // BOOL
	wRepeatCount      uint16
	wVirtualKeyCode   uint16
	wVirtualScanCode  uint16
	unicodeChar       uint16
	dwControlKeyState uint32
}

// inputRecord mirrors INPUT_RECORD for a KEY_EVENT. The uint16 padding after
// eventType aligns the union (which starts with a 4-byte BOOL) to a 4-byte
// boundary, matching the C layout (20 bytes total).
type inputRecord struct {
	eventType uint16
	_         uint16
	keyEvent  keyEventRecord
}

// injectLine types s into the console owned by targetPID, pauses for the input
// to register, then presses Enter to submit it.
//
// The calling process must NOT depend on its own console afterward: FreeConsole
// tears down stdout/stderr. Run this in a short-lived or detached process, or in
// one whose stdout/stderr are redirected to pipes or files, which FreeConsole
// leaves intact.
func injectLine(targetPID uint32, s string) error {
	// A process can be attached to only one console; release ours first.
	procFreeConsole.Call()

	r, _, err := procAttachConsole.Call(uintptr(targetPID))
	if r == 0 {
		return fmt.Errorf("AttachConsole(%d) failed: %w", targetPID, err)
	}
	defer procFreeConsole.Call()

	name, err := syscall.UTF16PtrFromString("CONIN$")
	if err != nil {
		return err
	}
	handle, err := syscall.CreateFile(name, genericRW, shareRW, nil, syscall.OPEN_EXISTING, 0, 0)
	if err != nil {
		return fmt.Errorf("open CONIN$: %w", err)
	}
	defer syscall.CloseHandle(handle)

	var recs []inputRecord
	for _, ch := range s {
		recs = append(recs, keyRecords(uint16(ch), 0)...)
	}
	if err := writeRecords(handle, recs); err != nil {
		return fmt.Errorf("type %q: %w", s, err)
	}

	time.Sleep(enterDelay)
	if err := writeRecords(handle, keyRecords('\r', vkReturn)); err != nil {
		return fmt.Errorf("press Enter: %w", err)
	}
	return nil
}

// keyRecords returns the down+up INPUT_RECORD pair for a single character.
// Pass vk=0 for ordinary printable text, or a virtual-key code (e.g. vkReturn).
func keyRecords(ch uint16, vk uint16) []inputRecord {
	recs := make([]inputRecord, 0, 2)
	for _, down := range []bool{true, false} {
		var b int32
		if down {
			b = 1
		}
		recs = append(recs, inputRecord{
			eventType: keyEvent,
			keyEvent: keyEventRecord{
				bKeyDown:        b,
				wRepeatCount:    1,
				wVirtualKeyCode: vk,
				unicodeChar:     ch,
			},
		})
	}
	return recs
}

func writeRecords(handle syscall.Handle, recs []inputRecord) error {
	if len(recs) == 0 {
		return nil
	}
	var written uint32
	r, _, err := procWriteConsoleInput.Call(
		uintptr(handle),
		uintptr(unsafe.Pointer(&recs[0])),
		uintptr(len(recs)),
		uintptr(unsafe.Pointer(&written)),
	)
	if r == 0 {
		return fmt.Errorf("WriteConsoleInputW failed: %w", err)
	}
	if int(written) != len(recs) {
		return fmt.Errorf("WriteConsoleInputW wrote %d of %d records", written, len(recs))
	}
	return nil
}

// --- Finding the target console ---------------------------------------------
//
// Walking the parent chain, rather than enumerating windows, is what keeps the
// injection focus-free and elevation-tolerant.

type procRow struct {
	ppid uint32
	name string
}

func snapshotProcs() (map[uint32]procRow, error) {
	snap, err := syscall.CreateToolhelp32Snapshot(syscall.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil, fmt.Errorf("CreateToolhelp32Snapshot: %w", err)
	}
	defer syscall.CloseHandle(snap)

	var e syscall.ProcessEntry32
	e.Size = uint32(unsafe.Sizeof(e))
	if err := syscall.Process32First(snap, &e); err != nil {
		return nil, fmt.Errorf("Process32First: %w", err)
	}

	procs := make(map[uint32]procRow, 256)
	for {
		procs[e.ProcessID] = procRow{
			ppid: e.ParentProcessID,
			name: syscall.UTF16ToString(e.ExeFile[:]),
		}
		if err := syscall.Process32Next(snap, &e); err != nil {
			break
		}
	}
	return procs, nil
}

// findClaudePID walks the parent chain from the current process up to the first
// claude.exe ancestor and returns its PID. This program runs as a descendant of
// the Claude Code session (claude.exe -> shell -> go run -> self-compact), so
// that ancestor is the console-owning process to inject into.
//
// It must be called from the still-living main process: once that process exits
// and a detached child is reparented, the chain back to claude.exe is broken,
// which is why the resolved PID is passed to the sidekick rather than re-derived.
func findClaudePID() (uint32, error) {
	procs, err := snapshotProcs()
	if err != nil {
		return 0, err
	}
	pid := uint32(os.Getpid())
	for i := 0; i < 64; i++ {
		row, ok := procs[pid]
		if !ok {
			break
		}
		if strings.EqualFold(row.name, "claude.exe") {
			return pid, nil
		}
		pid = row.ppid
	}
	return 0, fmt.Errorf("no claude.exe ancestor found (is this running inside a Claude Code session?)")
}
