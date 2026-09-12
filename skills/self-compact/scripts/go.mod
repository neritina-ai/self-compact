// This file exists so that `go run .` resolves in this directory's own context
// rather than that of whatever project the Claude Code session is working in,
// and so the `go` line below -- never newer than the installed toolchain -- is
// the one that applies. Without it, a session whose project pins a newer Go
// would make this skill download a toolchain, or fail outright.
module self-compact

go 1.21
