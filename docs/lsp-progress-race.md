# Native Mojo LSP crash investigation — 2026-09-08

A native parse/progress race is reproducible independently of Zed and the
Python bridge. It explains how ordinary editing followed by hover or
go-to-definition can segfault. The extension now prevents this race by setting
`capabilities.window.workDoneProgress` to `false` when initializing the native
server, including after automatic recovery.

## Cause

The native server's document task queue is intended to serialize parsing and
semantic queries. The progress implementation breaks that ordering:

1. `MojoDocument::startDocumentParse` queues a task calling `parseDocument`.
2. `parseDocument` passes the actual parser work to
   `ProgressManager::withProgress`.
3. With progress enabled, `withProgress` stores the callback, sends
   `window/workDoneProgress/create`, and returns without parsing. It waits for
   the editor's response before invoking the callback.
4. The document task returns, and `startTask` marks its chain complete.
5. A queued hover/definition request can now run, even though parsing has not
   started. `onHoverSync` and `onDefinitionSync` access
   `context->symbolIndex`; `context` is still null.

The client is allowed to send semantic requests while a progress-creation
request is outstanding. Responding quickly to progress creation does not
eliminate the race, since a request may already be queued. Reopening documents
after a restart repeats the same vulnerable lifecycle.

With progress disabled, `withProgress` invokes the parser callback immediately
inside the document's task. The task chain then waits for parsing as intended.
The extension suppresses native parsing progress indicators; hover, completion,
diagnostics, definitions, and import fixes continue to work. Restart recovery
remains available for unrelated native failures.

An upstream fix should keep the parse task pending until the actual parser
callback finishes, including when progress creation is delayed or rejected.
The server should also avoid accessing absent parser state. Merely replying to
progress requests faster, changing stdlib sources, or restarting cannot repair
the task-ordering bug.

## Evidence

Tested on Linux x86-64 with the installed Mojo 1.0.0 server. The test program
uses `String`, `byte_length`, and `sin`; it is valid Mojo and contains no user
project code. Restarts were disabled during bridge reproductions.

| Experiment | Result |
| --- | --- |
| Native server, edit followed by hover | SIGSEGV in about 2 seconds |
| Native server, edit followed by definition | SIGSEGV in about 3 seconds |
| Full-document updates instead of incremental edits | Still SIGSEGV |
| Hover before acknowledging first progress creation | SIGSEGV before first parse |
| Same progress ordering with source stdlib | Still SIGSEGV |
| Same reproducer with progress disabled | Hover succeeds; clean shutdown |
| Reproducer through patched bridge, source stdlib | Hover succeeds; clean shutdown |
| Patched bridge, client advertises progress, compiled stdlib, repeated edits and six semantic requests per edit | 188 cycles over 85 seconds; clean shutdown |
| Patched bridge, client advertises progress, source stdlib, same workload | 71 cycles over 85 seconds; clean shutdown |

An unmodified progress-enabled source-stdlib run also survived 86 seconds of
editing; deliberately delaying the acknowledgement still crashes that setup.
This is a scheduling race, so surviving one editing session does not rule it out.

The release executable is stripped. LLDB caught its fault at address `0x430`
on a native worker thread. A locally built `1.1.0.dev0` executable reproduced
the controlled progress race and provided this symbolized stack (intermediate
template frames omitted):

```text
signal SIGSEGV: address not mapped to object (fault address=0x470)
llvm::IntervalMap<...>::branched                 IntervalMap.h:1034
llvm::IntervalMap<...>::const_iterator::find     IntervalMap.h:1486
SymbolIndex::getSymbolAt                        MojoServer.cpp:434
MojoDocument::onHoverSync                       MojoServer.cpp:1456
MojoDocument::onHover(...) callback             MojoServer.cpp:1451
MojoDocument::startTask(...) callback           MojoDocument.h:344
```

The source also shows the early task completion in `MojoDocument.h::startTask`,
the deferred parser callback in `MojoServer.cpp::ProgressManager::withProgress`,
and allocation of `context` inside that callback in `parseDocument`.

This establishes a specific native crash and a prevention for it. The original
editor-session native backtrace was not available on this machine, so it does
not establish that every previously observed exit has this cause. There is no
one-minute timeout involved in this reproduction.

## Reproduce and compare

Run from this repository in an activated Mojo environment:

```sh
python3 scripts/repro-lsp-progress-race.py --server "$(command -v mojo-lsp-server)"
python3 scripts/repro-lsp-progress-race.py --server "$(command -v mojo-lsp-server)" --no-progress
python3 scripts/repro-lsp-progress-race.py --server "$(command -v mojo-lsp-server)" --bridge
```

The first command is expected to report status `-11` on the affected Linux
server. The next two should print `PASS`. To compare source stdlib, add
`--stdlib-path /path/to/matching/mojo/stdlib` to any command. The script delays
acknowledging progress creation by 500 ms and issues hover during that interval.
It saves the generated program, bidirectional protocol transcript, and native
stderr in the printed temporary directory. It never opens user documents.

Regression coverage verifies that initialization preserves the other client
capabilities and options, that recovery uses the same disabled progress
capability, and that the real compiler returns a hover through the bridge with
both stdlib setups. The existing real-server integration test also checks
stdlib/local navigation, import fixes, and compiling the resulting program.

Validation completed: all 32 Python tests, including both real-server tests;
`cargo fmt --check`; native and `wasm32-wasip1` checks; and the release
`wasm32-wasip2` extension build.
