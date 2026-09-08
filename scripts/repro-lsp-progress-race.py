#!/usr/bin/env python3
"""Exercise Mojo's parse/progress race without an editor or third-party modules.

With progress enabled, send hover before acknowledging progress creation.
Mojo 1.0.0 can dereference its not-yet-created parser context and SIGSEGV.
Compare --no-progress or --bridge. All artifacts use a temporary directory.
"""

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from mojo_lsp import read_message, write_message

SOURCE = '''from std.math import sin
def main():
    var s = String("hello")
    print(s.byte_length(), sin(1.0))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--stdlib-path", type=Path)
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--bridge", action="store_true")
    args = parser.parse_args()
    server = args.server.expanduser().resolve()
    root = Path(tempfile.mkdtemp(prefix="zed-mojo-progress-race-"))
    path = root / "main.mojo"
    path.write_text(SOURCE)
    document = {"uri": path.as_uri()}
    options = {"zed_mojo": {"download_stdlib": False, "restart_limit": 0}}
    environment = os.environ.copy()
    command = [str(server), "--log=error"]
    library = server.parent.parent / "lib/mojo"
    if library.is_dir():
        command.extend(["-I", str(library)])
    if args.stdlib_path:
        stdlib = str(args.stdlib_path.expanduser().resolve())
        options["zed_mojo"]["stdlib_path"] = stdlib
        if not args.bridge:
            environment["MODULAR_MOJO_MAX_IMPORT_PATH"] = stdlib
            command.extend(["-I", stdlib])
    if args.bridge:
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "server/mojo_lsp.py"),
            "--server",
            str(server),
            "--workspace",
            str(root),
            "--cache",
            str(root / "cache"),
            "--",
        ] + command[1:]
    symbolizer = server.with_name("llvm-symbolizer")
    if symbolizer.is_file():
        environment["LLVM_SYMBOLIZER_PATH"] = str(symbolizer)
    print(f"Artifacts: {root}", flush=True)
    messages = queue.Queue()
    with (root / "stderr.log").open("wb") as errors, (root / "protocol.jsonl").open("w") as trace:
        child = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )

        def read():
            try:
                while (message := read_message(child.stdout)) is not None:
                    messages.put(message)
            finally:
                messages.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def send(message):
            message = {"jsonrpc": "2.0", **message}
            trace.write(json.dumps({"client": message}) + "\n")
            trace.flush()
            write_message(child.stdin, message)

        def hover():
            send({
                "id": 2,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": document,
                    "position": {"line": 3, "character": 12},
                },
            })

        pending_progress = None
        hover_sent = False
        deadline = time.monotonic() + 20
        success = False
        try:
            send({
                "id": 1,
                "method": "initialize",
                "params": {
                    "processId": None,
                    "rootUri": root.as_uri(),
                    "capabilities": {"window": {"workDoneProgress": not args.no_progress}},
                    "initializationOptions": options,
                },
            })
            while True:
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError("server did not finish the reproduction within 20 seconds")
                if pending_progress is not None and now >= pending_progress[1]:
                    send({"id": pending_progress[0], "result": None})
                    pending_progress = None
                timeout = deadline - now
                if pending_progress is not None:
                    timeout = min(timeout, max(0, pending_progress[1] - now))
                try:
                    message = messages.get(timeout=timeout)
                except queue.Empty:
                    continue
                if message is None:
                    raise RuntimeError(f"server exited with status {child.wait(timeout=5)}")
                trace.write(json.dumps({"server": message}) + "\n")
                trace.flush()
                method = message.get("method")
                if method is None and "error" in message:
                    raise RuntimeError(str(message["error"]))
                if method == "window/workDoneProgress/create":
                    if not hover_sent:
                        hover()
                        hover_sent = True
                        # Legal client behavior: hover is independent of acknowledging
                        # a progress indicator. Hold the reply long enough to expose
                        # the native task-ordering bug; still acknowledge eventually.
                        pending_progress = (message["id"], time.monotonic() + 0.5)
                    else:
                        send({"id": message["id"], "result": None})
                elif method is not None and "id" in message:
                    send({"id": message["id"], "result": None})
                elif method == "textDocument/publishDiagnostics" and not hover_sent:
                    hover()
                    hover_sent = True
                elif method is None and message.get("id") == 1:
                    send({"method": "initialized", "params": {}})
                    send({
                        "method": "textDocument/didOpen",
                        "params": {
                            "textDocument": {
                                **document, "languageId": "mojo", "version": 1, "text": SOURCE,
                            },
                        },
                    })
                elif method is None and message.get("id") == 2:
                    if not message.get("result"):
                        raise RuntimeError("hover returned no result")
                    send({"id": 3, "method": "shutdown", "params": None})
                elif method is None and message.get("id") == 3:
                    send({"method": "exit", "params": {}})
                    if child.wait(timeout=5) != 0:
                        raise RuntimeError(f"server exited with status {child.returncode}")
                    success = True
                    break
        except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as error:
            print(f"FAIL: {error}", flush=True)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            child.stdin.close()
            reader.join(timeout=1)
            child.stdout.close()
    if success:
        print("PASS: hover returned and server shut down cleanly")
    else:
        print((root / "stderr.log").read_text(errors="replace"))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
