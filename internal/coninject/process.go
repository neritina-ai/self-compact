package coninject

import (
	"fmt"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

type procRow struct {
	ppid uint32
	name string
}

func snapshotProcs() (map[uint32]procRow, error) {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil, fmt.Errorf("CreateToolhelp32Snapshot: %w", err)
	}
	defer windows.CloseHandle(snap)

	var e windows.ProcessEntry32
	e.Size = uint32(unsafe.Sizeof(e))
	if err := windows.Process32First(snap, &e); err != nil {
		return nil, fmt.Errorf("Process32First: %w", err)
	}

	procs := make(map[uint32]procRow, 256)
	for {
		procs[e.ProcessID] = procRow{
			ppid: e.ParentProcessID,
			name: windows.UTF16ToString(e.ExeFile[:]),
		}
		if err := windows.Process32Next(snap, &e); err != nil {
			break
		}
	}
	return procs, nil
}

// FindClaudePID walks the parent chain from the current process up to the first
// claude.exe ancestor and returns its PID. self-compact runs as a descendant of
// the Claude Code session (claude.exe -> shell -> self-compact), so this
// resolves the console-owning process to inject into.
//
// It must be called from the still-living main process: once that process exits
// and a detached child is reparented, the chain back to claude.exe is broken,
// which is why the resolved PID is passed to the sidekick rather than re-derived.
func FindClaudePID() (uint32, error) {
	procs, err := snapshotProcs()
	if err != nil {
		return 0, err
	}
	pid := windows.GetCurrentProcessId()
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
