import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import mojo_lsp as bridge


def position(line, character):
    return {"line": line, "character": character}


def span(line, start, end):
    return {"start": position(line, start), "end": position(line, end)}


def apply_edit(text, edit):
    return bridge.apply_changes(text, [{"range": edit["range"], "text": edit["newText"]}])


class EditingTests(unittest.TestCase):
    def test_utf16_incremental_edits(self):
        text = '# 😀\r\ndef main():\r\n    print("hello")\r\n'
        text = bridge.apply_changes(
            text,
            [{"range": span(0, 2, 4), "text": "é"}, {"range": span(2, 11, 16), "text": "world"}],
        )
        self.assertEqual(text, '# é\r\ndef main():\r\n    print("world")\r\n')
        self.assertEqual(bridge.apply_changes(text, [{"text": "replacement"}]), "replacement")

    def test_insertion_preserves_header_docstring_and_multiline_imports(self):
        text = '#!/usr/bin/env mojo\r\n# License\r\n"""Module docs.\r\nMore docs."""\r\nfrom std.math import (\r\n    sin,\r\n)  # keep this\r\n\r\ndef main():\r\n    sqrt(1.0)\r\n'
        result = apply_edit(text, bridge.import_edit(text, "std.math", "sqrt"))
        self.assertEqual(
            result,
            text.replace(")  # keep this\r\n", ")  # keep this\r\nfrom std.math import sqrt\r\n"),
        )

    def test_no_duplicate_imports_or_alias_bindings(self):
        for text in (
            "from std.math import sqrt\n",
            "from std.math import (sin, sqrt,)\n",
            "from other import value as sqrt\n",
            "from std.math import *\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(bridge.import_edit(text, "std.math", "sqrt"))
        self.assertIsNotNone(
            bridge.import_edit("from std.math import sqrt as root\n", "std.math", "sqrt")
        )

    def test_comments_empty_files_and_missing_final_newline(self):
        for text, expected in (
            ("", "from std.math import sqrt\n"),
            ("# License", "# License\nfrom std.math import sqrt\n"),
            ('"""Docs"""', '"""Docs"""\nfrom std.math import sqrt\n'),
            (
                "# header\ndef main():\n    pass\n",
                "# header\nfrom std.math import sqrt\ndef main():\n    pass\n",
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    apply_edit(text, bridge.import_edit(text, "std.math", "sqrt")), expected
                )

    def test_source_exports_ignore_methods_comments_and_docstrings(self):
        text = '''"""Docs:
def imaginary():
    pass
"""
# struct Imaginary:
struct Real:
    def method(self):
        pass

def function():
    var local = 1

comptime VALUE = 3
from .impl import (
    Thing as Renamed,
)
'''
        names, imports = bridge.source_exports(text, "pkg", True)
        self.assertEqual(names, {"Real", "function", "VALUE"})
        self.assertEqual(imports, [("pkg.impl", "Thing", "Renamed")])

    def test_relative_imports(self):
        _, imports = bridge.source_exports(
            "from ..core import Thing\nfrom . import child\n", "pkg.sub.module", False
        )
        self.assertEqual(imports, [("pkg.core", "Thing", "Thing"), ("pkg.sub", "child", "child")])


class IndexTests(unittest.TestCase):
    def test_source_packages_reexports_and_removed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pkg").mkdir()
            (root / "pkg/__init__.mojo").write_text(
                "from .impl import *\nfrom .impl import Thing as Renamed\n"
            )
            implementation = root / "pkg/impl.mojo"
            implementation.write_text(
                "struct Thing:\n    def method(self):\n        pass\nstruct _Private:\n    pass\n"
            )
            (root / ".pixi").mkdir()
            (root / ".pixi/hidden.mojo").write_text("struct NotImportable:\n    pass\n")
            index = bridge.ImportIndex([root], root / "no-compiler", root / ".cache")
            self.assertEqual(index.candidates("Thing"), ["pkg", "pkg.impl"])
            self.assertEqual(index.candidates("Thing", implementation.resolve().as_uri()), ["pkg"])
            self.assertEqual(index.candidates("Renamed"), ["pkg"])
            for name in ("method", "_Private", "NotImportable"):
                self.assertEqual(index.candidates(name), [])
            implementation.unlink()
            index.updated = 0
            self.assertEqual(index.candidates("Thing"), [])

    def test_doc_index_preserves_package_names_and_excludes_members(self):
        doc = {
            "structs": [{"name": "Color", "functions": [{"name": "method"}]}],
            "modules": [
                {"name": "__init__", "aliases": [{"name": "VERSION"}]},
                {"name": "tools", "functions": [{"name": "create"}]},
            ],
            "packages": [
                {"name": "nested", "modules": [{"name": "types", "traits": [{"name": "Widget"}]}]}
            ],
        }
        self.assertEqual(
            list(bridge.doc_exports(doc, "package")),
            [
                ("package", {"Color"}),
                ("package", {"VERSION"}),
                ("package.tools", {"create"}),
                ("package.nested", set()),
                ("package.nested.types", {"Widget"}),
            ],
        )

    def test_stdlib_docs_are_not_sent_to_crashing_compiler(self):
        index = bridge.ImportIndex([], Path(sys.executable), Path("unused"))
        with patch.object(subprocess, "run") as run:
            self.assertEqual(index.compiled_exports(Path("std.mojoc"), "std"), {})
            run.assert_not_called()


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.text = "def main():\n    sqrt(1.0)\n    Color()\n"
        self.diagnostic = {
            "message": "use of unknown declaration 'sqrt'; did you mean to import it from 'std.math'? Add 'from std.math import sqrt'",
            "source": "mojo",
            "range": span(1, 4, 8),
            "severity": 1,
        }
        self.params = {
            "textDocument": {"uri": "file:///test.mojo"},
            "range": span(1, 5, 5),
            "context": {"diagnostics": [self.diagnostic]},
        }
        self.index = unittest.mock.Mock()
        self.index.candidates.return_value = ["std.math", "std.math.math"]

    def test_diagnostic_hint_becomes_an_edit(self):
        actions = bridge.import_actions(self.params, self.text, self.index)
        self.assertEqual(
            [action["title"] for action in actions],
            ["Import sqrt from std.math", "Import sqrt from std.math.math"],
        )
        edit = actions[0]["edit"]["changes"]["file:///test.mojo"][0]
        self.assertTrue(apply_edit(self.text, edit).startswith("from std.math import sqrt\n"))

    def test_filter_kinds_ranges_stale_diagnostics_and_qualified_names(self):
        self.params["context"]["only"] = ["refactor"]
        self.assertEqual(bridge.import_actions(self.params, self.text, self.index), [])
        self.params["context"]["only"] = ["quickfix"]
        self.params["range"] = span(2, 5, 5)
        self.assertEqual(bridge.import_actions(self.params, self.text, self.index), [])
        self.params["range"] = span(1, 5, 5)
        self.assertEqual(
            bridge.import_actions(self.params, self.text.replace("sqrt", "sine"), self.index), []
        )
        self.assertEqual(
            bridge.import_actions(
                self.params, self.text.replace("    sqrt", "obj.sqrt"), self.index
            ),
            [],
        )


class SetupTests(unittest.TestCase):
    def test_custom_sources_and_arguments_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "sources/std").mkdir(parents=True)
            (root / "sources/std/__init__.mojo").touch()
            (root / "packages").mkdir()
            (root / "bin").mkdir()
            (root / "lib/mojo").mkdir(parents=True)
            init = {
                "params": {
                    "initializationOptions": {
                        "zed_mojo": {"stdlib_path": "sources", "import_paths": ["packages"]}
                    }
                }
            }
            with patch.object(subprocess, "run") as run:
                command, index, environment = bridge.prepare(
                    str(root / "bin/mojo-lsp-server"),
                    ["--check-docstrings", "-I", "custom"],
                    root,
                    root / "cache",
                    init,
                )
                run.assert_not_called()
            self.assertEqual(
                command,
                [
                    str(root / "bin/mojo-lsp-server"),
                    "-I",
                    str(root / "sources"),
                    "-I",
                    str(root / "packages"),
                    "--check-docstrings",
                    "-I",
                    "custom",
                    "-I",
                    str(root),
                    "-I",
                    str(root / "lib/mojo"),
                ],
            )
            self.assertIn(root / "packages", index.roots)
            self.assertEqual(
                environment["MODULAR_MOJO_MAX_IMPORT_PATH"].split(",")[0], str(root / "sources")
            )

    def test_modular_config_and_environment_search_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "modular.cfg").write_text("[mojo-max]\nimport_path = /sdk/lib/mojo,/packages\n")
            with patch.dict(os.environ, {"MODULAR_HOME": str(root)}, clear=True):
                self.assertEqual(bridge.configured_import_paths(), ["/sdk/lib/mojo", "/packages"])
                with patch.dict(os.environ, {"MODULAR_MOJO_MAX_IMPORT_PATH": "/custom,/other"}):
                    self.assertEqual(bridge.configured_import_paths(), ["/custom", "/other"])

    def test_nightly_never_downloads_unmatched_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.CompletedProcess([], 0, "Mojo 1.1.0.dev20260905\n", "")
            with patch.object(subprocess, "run", return_value=result) as run:
                with self.assertRaisesRegex(ValueError, "nightly/custom"):
                    bridge.ensure_stdlib("mojo-lsp-server", {}, root, [], root)
                self.assertEqual(run.call_count, 1)


class TransportTests(unittest.TestCase):
    def test_utf8_framing_and_multiple_messages(self):
        stream = io.BytesIO()
        messages = [{"jsonrpc": "2.0", "id": "a", "result": "😀"}, {"method": "exit"}]
        for message in messages:
            bridge.write_message(stream, message)
        stream.seek(0)
        self.assertEqual([bridge.read_message(stream), bridge.read_message(stream)], messages)
        self.assertIsNone(bridge.read_message(stream))

    def test_truncated_body(self):
        with self.assertRaises(EOFError):
            bridge.read_message(io.BytesIO(b"Content-Length: 10\r\n\r\n{}"))


class LspClient:
    def __init__(self, command, root, options):
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr
        )
        self.messages, self.diagnostics = queue.Queue(), {}
        self.sequence = 0

        def read():
            try:
                while (message := bridge.read_message(self.process.stdout)) is not None:
                    self.messages.put(message)
            finally:
                self.messages.put(None)

        threading.Thread(target=read, daemon=True).start()
        self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root.as_uri(),
                "capabilities": {
                    "textDocument": {
                        "definition": {"linkSupport": True},
                        "codeAction": {
                            "codeActionLiteralSupport": {
                                "codeActionKind": {"valueSet": ["quickfix"]}
                            }
                        },
                    }
                },
                "initializationOptions": options,
            },
        )
        self.notify("initialized", {})

    def receive(self):
        message = self.messages.get(timeout=45)
        if message is None:
            self.stderr.seek(0)
            raise AssertionError(self.stderr.read().decode())
        if message.get("method") == "textDocument/publishDiagnostics":
            self.diagnostics[message["params"]["uri"]] = message["params"]
        return message

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        bridge.write_message(
            self.process.stdin,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )
        while True:
            message = self.receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise AssertionError(message["error"])
                return message.get("result")
            if "method" in message and "id" in message:
                bridge.write_message(
                    self.process.stdin, {"jsonrpc": "2.0", "id": message["id"], "result": None}
                )

    def notify(self, method, params):
        bridge.write_message(
            self.process.stdin, {"jsonrpc": "2.0", "method": method, "params": params}
        )

    def open(self, path, text):
        uri = path.as_uri()
        self.notify(
            "textDocument/didOpen",
            {"textDocument": {"uri": uri, "languageId": "mojo", "version": 1, "text": text}},
        )
        while uri not in self.diagnostics:
            self.receive()
        return uri

    def close(self):
        try:
            self.request("shutdown", None)
            self.notify("exit", {})
            self.process.wait(timeout=10)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()


class ProxyTests(unittest.TestCase):
    def test_native_fixes_requests_and_unsaved_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "library.mojo").write_text("struct Thing:\n    pass\nstruct Ghost:\n    pass\n")
            command = [
                sys.executable,
                str(Path(bridge.__file__)),
                "--server",
                sys.executable,
                "--workspace",
                str(root),
                "--cache",
                str(root / ".cache"),
                "--",
                str(Path(__file__).with_name("fake_server.py")),
            ]
            client = LspClient(command, root, {"zed_mojo": {"download_stdlib": False}})
            try:
                text = "def main():\n    Thing()\n"
                uri = client.open(root / "main.mojo", text)
                self.assertEqual(
                    client.request("test/echo", {"nested": [1, "😀"]}), {"nested": [1, "😀"]}
                )
                self.assertEqual(
                    client.request("test/serverRequest", {}),
                    {"jsonrpc": "2.0", "id": "server-request", "result": None},
                )
                for version, symbol in ((1, "Thing"), (2, "Ghost")):
                    if version == 2:
                        client.notify(
                            "textDocument/didChange",
                            {
                                "textDocument": {"uri": uri, "version": version},
                                "contentChanges": [{"range": span(1, 4, 9), "text": "Ghost"}],
                            },
                        )
                    diagnostic = {
                        "message": f"use of unknown declaration '{symbol}'",
                        "range": span(1, 4, 9),
                        "source": "mojo",
                    }
                    actions = client.request(
                        "textDocument/codeAction",
                        {
                            "textDocument": {"uri": uri},
                            "range": span(1, 5, 5),
                            "context": {"diagnostics": [diagnostic]},
                        },
                    )
                    self.assertEqual(
                        actions[0],
                        {"title": "Native Mojo fix", "kind": "quickfix", "data": {"opaque": 42}},
                    )
                    self.assertEqual(actions[1]["title"], f"Import {symbol} from library")
            finally:
                client.close()


@unittest.skipUnless(
    os.environ.get("MOJO_LSP_SERVER") and os.environ.get("MOJO_STDLIB_PATH"),
    "set MOJO_LSP_SERVER and MOJO_STDLIB_PATH for real-server integration tests",
)
class IntegrationTests(unittest.TestCase):
    def test_navigation_and_import_fixes_with_real_compiler(self):
        server = Path(os.environ["MOJO_LSP_SERVER"]).resolve()
        compiler = server.with_name("mojo")
        with tempfile.TemporaryDirectory(prefix="zed-mojo-integration-") as temporary:
            root = Path(temporary).resolve()
            packages = root / ".packages"
            packages.mkdir()
            source = root / ".package-source/geometry"
            source.mkdir(parents=True)
            (source / "__init__.mojo").write_text(
                "struct Widget(Copyable, Movable):\n    var value: Int\n    def __init__(out self, value: Int):\n        self.value = value\n"
            )
            compiled = packages / "geometry.mojoc"
            subprocess.run(
                [
                    str(compiler),
                    "precompile",
                    str(source),
                    "-o",
                    str(compiled),
                    "-I",
                    str(server.parent.parent / "lib/mojo"),
                ],
                check=True,
                capture_output=True,
            )
            (root / "local_helpers.mojo").write_text("def helper() -> Int:\n    return 1\n")
            command = [
                sys.executable,
                str(Path(bridge.__file__)),
                "--server",
                str(server),
                "--workspace",
                str(root),
                "--cache",
                str(root / ".cache"),
                "--",
                "--log=error",
                "-I",
                str(packages),
            ]
            client = LspClient(
                command, root, {"zed_mojo": {"stdlib_path": os.environ["MOJO_STDLIB_PATH"]}}
            )
            try:
                text = 'from std.math import sin\nfrom local_helpers import helper\ndef main():\n    var s = String("hello")\n    print(s.byte_length(), sin(1.0), helper())\n'
                uri = client.open(root / "navigation.mojo", text)
                self.assertFalse(
                    [d for d in client.diagnostics[uri]["diagnostics"] if d.get("severity") == 1]
                )
                for line, symbol, expected in (
                    (0, "sin", "/std/math/math.mojo"),
                    (3, "String", "/std/collections/string/string.mojo"),
                    (4, "byte_length", "/std/collections/string/string.mojo"),
                    (4, "helper", "/local_helpers.mojo"),
                ):
                    column = text.splitlines()[line].index(symbol)
                    locations = client.request(
                        "textDocument/definition",
                        {"textDocument": {"uri": uri}, "position": position(line, column)},
                    )
                    self.assertTrue(
                        any(location["uri"].endswith(expected) for location in locations),
                        (line, column, locations),
                    )
                broken = "def main():\n    print(sqrt(1.0))\n    var widget = Widget(7)\n    print(widget.value)\n"
                broken_uri = client.open(root / "missing.mojo", broken)
                diagnostics = client.diagnostics[broken_uri]["diagnostics"]
                fixed = broken
                for symbol, line, column, expected in (
                    ("sqrt", 1, 11, "std.math"),
                    ("Widget", 2, 17, "geometry"),
                ):
                    actions = client.request(
                        "textDocument/codeAction",
                        {
                            "textDocument": {"uri": broken_uri},
                            "range": span(line, column, column),
                            "context": {"diagnostics": diagnostics, "only": ["quickfix"]},
                        },
                    )
                    action = next(
                        action
                        for action in actions
                        if action["title"] == f"Import {symbol} from {expected}"
                    )
                    fixed = apply_edit(fixed, action["edit"]["changes"][broken_uri][0])
                fixed_path = root / "fixed.mojo"
                fixed_path.write_text(fixed)
                fixed_uri = client.open(fixed_path, fixed)
                self.assertFalse(
                    [
                        d
                        for d in client.diagnostics[fixed_uri]["diagnostics"]
                        if d.get("severity") == 1
                    ],
                    client.diagnostics[fixed_uri],
                )
                build = subprocess.run(
                    [
                        str(compiler),
                        "build",
                        str(fixed_path),
                        "-o",
                        str(root / "fixed"),
                        "-I",
                        str(packages),
                        "-I",
                        str(server.parent.parent / "lib/mojo"),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(build.returncode, 0, build.stderr)
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
