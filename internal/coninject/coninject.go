// Package coninject injects keystrokes into a target console's input buffer on
// Windows, bypassing the windowing/UIPI layer via AttachConsole +
// WriteConsoleInputW. This works across the normal->elevated integrity boundary
// in the common case where the elevated console was launched by the same
// interactive user.
package coninject

import (
	"fmt"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	keyEvent     = 0x0001
	vkReturn     = 0x0D
	genericRW    = 0x80000000 | 0x40000000
	shareRW      = 0x00000001 | 0x00000002
	openExisting = 3
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

var (
	kernel32              = windows.NewLazySystemDLL("kernel32.dll")
	procFreeConsole       = kernel32.NewProc("FreeConsole")
	procAttachConsole     = kernel32.NewProc("AttachConsole")
	procWriteConsoleInput = kernel32.NewProc("WriteConsoleInputW")
)

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

func writeRecords(handle windows.Handle, recs []inputRecord) error {
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

// Step is one unit of an injection sequence: either Text (typed as printable
// characters), an Enter key, or a pause before the next step.
type Step struct {
	Text  string
	Enter bool
	Delay time.Duration
}

// Text returns a step that types the given string.
func Text(s string) Step { return Step{Text: s} }

// Enter returns a step that presses Return.
func Enter() Step { return Step{Enter: true} }

// Delay returns a step that pauses before the following step runs.
func Delay(d time.Duration) Step { return Step{Delay: d} }

// Inject attaches to the target process's console and plays the given steps
// into its input buffer. The calling process must NOT depend on its own console
// afterward: FreeConsole tears down stdout/stderr. Run this in a short-lived /
// detached process, or in a process whose stdout/stderr are redirected to pipes
// or files (which FreeConsole leaves intact).
func Inject(targetPID uint32, steps []Step) error {
	// A process can be attached to only one console; release ours first.
	procFreeConsole.Call()

	r, _, err := procAttachConsole.Call(uintptr(targetPID))
	if r == 0 {
		return fmt.Errorf("AttachConsole(%d) failed: %w", targetPID, err)
	}
	defer procFreeConsole.Call()

	name, _ := windows.UTF16PtrFromString("CONIN$")
	handle, err := windows.CreateFile(name, genericRW, shareRW, nil, openExisting, 0, 0)
	if err != nil {
		return fmt.Errorf("open CONIN$: %w", err)
	}
	defer windows.CloseHandle(handle)

	for _, step := range steps {
		if step.Delay > 0 {
			time.Sleep(step.Delay)
		}
		if step.Enter {
			if err := writeRecords(handle, keyRecords('\r', vkReturn)); err != nil {
				return err
			}
		}
		if step.Text != "" {
			var recs []inputRecord
			for _, r := range step.Text {
				recs = append(recs, keyRecords(uint16(r), 0)...)
			}
			if err := writeRecords(handle, recs); err != nil {
				return err
			}
		}
	}
	return nil
}
