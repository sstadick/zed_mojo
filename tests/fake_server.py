"""Small protocol peer used to test the bridge independently of Mojo."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from mojo_lsp import read_message, write_message, apply_changes

waiting = None
documents = {}
configuration = None
restart_marker = None
while (message := read_message(sys.stdin.buffer)) is not None:
    method = message.get("method")
    if method == "exit":
        break
    if method == "test/crash":
        if restart_marker is not None:
            restart_marker.touch()
        sys.exit(23)
    if method == "test/startProgress":
        write_message(
            sys.stdout.buffer,
            {
                "jsonrpc": "2.0",
                "method": "$/progress",
                "params": {
                    "token": "fake-progress",
                    "value": {"kind": "begin", "title": "Parsing"},
                },
            },
        )
    if method == "workspace/didChangeConfiguration":
        configuration = message["params"]
    if method == "textDocument/didChange":
        document = message["params"]["textDocument"]
        old = documents[document["uri"]]
        documents[document["uri"]] = {
            **old,
            "version": document["version"],
            "text": apply_changes(old["text"], message["params"]["contentChanges"]),
        }
    if method == "textDocument/didClose":
        documents.pop(message["params"]["textDocument"]["uri"], None)
    if method == "textDocument/didOpen":
        document = message["params"]["textDocument"]
        documents[document["uri"]] = document
        write_message(
            sys.stdout.buffer,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": document["uri"],
                    "version": document["version"],
                    "diagnostics": [],
                },
            },
        )
    if "id" not in message:
        continue
    if method == "initialize":
        marker = message["params"].get("initializationOptions", {}).get("test_restart_marker")
        if marker:
            restart_marker = Path(marker)
            if restart_marker.exists():
                continue
        result = {
            "capabilities": {
                "textDocumentSync": 2,
                "codeActionProvider": True,
                "hoverProvider": True,
            }
        }
    elif method == "textDocument/codeAction":
        result = [{"title": "Native Mojo fix", "kind": "quickfix", "data": {"opaque": 42}}]
        if "testMarker" in message["params"]:
            result[0]["data"]["marker"] = message["params"]["testMarker"]
    elif method == "test/state":
        result = {"documents": documents, "configuration": configuration}
    elif method == "test/hang":
        continue
    elif method == "test/serverRequest":
        waiting = message["id"]
        write_message(
            sys.stdout.buffer,
            {
                "jsonrpc": "2.0",
                "id": "server-request",
                "method": "workspace/configuration",
                "params": {"items": []},
            },
        )
        continue
    elif method is None and message["id"] == "server-request":
        write_message(sys.stdout.buffer, {"jsonrpc": "2.0", "id": waiting, "result": message})
        continue
    else:
        result = message.get("params")
    write_message(sys.stdout.buffer, {"jsonrpc": "2.0", "id": message["id"], "result": result})
