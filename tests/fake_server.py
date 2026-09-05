"""Small protocol peer used to test the bridge independently of Mojo."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from mojo_lsp import read_message, write_message

waiting = None
while (message := read_message(sys.stdin.buffer)) is not None:
    method = message.get("method")
    if method == "exit":
        break
    if method == "textDocument/didOpen":
        document = message["params"]["textDocument"]
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
        result = {
            "capabilities": {
                "textDocumentSync": 2,
                "codeActionProvider": True,
                "hoverProvider": True,
            }
        }
    elif method == "textDocument/codeAction":
        result = [{"title": "Native Mojo fix", "kind": "quickfix", "data": {"opaque": 42}}]
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
