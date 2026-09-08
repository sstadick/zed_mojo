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
    def test_immutable_stdlib_is_reused_without_scanning_across_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdlib, project = root / "stdlib", root / "project"
            (stdlib / "std/math").mkdir(parents=True)
            project.mkdir()
            (stdlib / "std/math/__init__.mojo").write_text("from .impl import *\n")
            (stdlib / "std/math/impl.mojo").write_text(
                "def square(x: Int) -> Int:\n    return x * x\n"
            )
            index = bridge.ImportIndex(
                [stdlib, project],
                root / "no-compiler",
                root / "cache",
                immutable_roots={stdlib: "release-tag"},
            )
            self.assertEqual(index.candidates("square"), ["std.math", "std.math.impl"])
            original = bridge.module_files

            def scan(path):
                self.assertEqual(path, project, "cached stdlib must not be walked")
                return original(path)

            with patch.object(bridge, "module_files", side_effect=scan):
                index.updated = 0
                with patch.object(
                    bridge, "resolve_reexports", side_effect=AssertionError("no rebuild")
                ):
                    self.assertEqual(index.candidates("square"), ["std.math", "std.math.impl"])
                (project / "local.mojo").write_text("from std.math import square as local_square\n")
                index.updated = 0
                self.assertEqual(index.candidates("local_square"), ["local"])
                restarted = bridge.ImportIndex(
                    [stdlib, project],
                    root / "no-compiler",
                    root / "cache",
                    immutable_roots={stdlib: "release-tag"},
                )
                self.assertEqual(restarted.candidates("square"), ["std.math", "std.math.impl"])
            # A different release is indexed afresh, as is a corrupt snapshot.
            for cached in (root / "cache").glob("stdlib-*.json"):
                cached.write_text('{"std.math": 42}')
            restarted = bridge.ImportIndex(
                [stdlib],
                root / "no-compiler",
                root / "cache",
                immutable_roots={stdlib: "release-tag"},
            )
            self.assertEqual(restarted.candidates("square"), ["std.math", "std.math.impl"])

    def test_batch_probe_rejects_incomplete_or_unrelated_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            index = bridge.ImportIndex([root], Path(sys.executable), root / "cache")

            def error(line, symbol, message=None):
                return json.dumps(
                    {
                        "kind": "error",
                        "message": message or f"package 'pkg' does not contain '{symbol}'",
                        "diagnostic": {
                            "file": str(root / "imports.mojo"),
                            "location": {"line": line},
                        },
                    }
                )

            sentinel = error(3, "_zed_mojo_probe_end_7c42b0a1")
            cases = [
                (error(2, "Hidden") + "\n" + sentinel, {"Public"}),
                (sentinel, {"Public", "Hidden"}),
                (error(2, "Hidden"), None),
                (error(1, "Public", "unexpected parser failure") + "\n" + sentinel, None),
                ("{malformed json", None),
            ]
            for stderr, expected in cases:
                with self.subTest(stderr=stderr), patch.object(
                    subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", stderr)
                ):
                    self.assertEqual(
                        index.probe_imports("pkg", ["Public", "Hidden"], root), expected
                    )

    def test_compiled_surfaces_persist_without_per_symbol_probes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "pkg.mojoc"
            package.touch()
            doc = {
                "decl": {
                    "modules": [
                        {"name": "__init__"},
                        {"name": "impl", "aliases": [{"name": "Public"}, {"name": "Hidden"}]},
                    ]
                }
            }

            def compile_docs(args, **kwargs):
                Path(args[args.index("-o") + 1]).write_text(json.dumps(doc))
                return subprocess.CompletedProcess(args, 0)

            for restart in (False, True):
                index = bridge.ImportIndex([root], Path(sys.executable), root / ".cache")
                with patch.object(subprocess, "run", side_effect=compile_docs) as run, patch.object(
                    index, "probe_imports", return_value={"Public"}
                ) as batch, patch.object(
                    index, "verified_reexport", side_effect=AssertionError("no per-symbol compiler")
                ):
                    self.assertEqual(index.candidates("Public"), ["pkg", "pkg.impl"])
                    self.assertEqual(index.candidates("Hidden"), ["pkg.impl"])
                    (root / "main.mojo").write_text(
                        "def main():\n    pass\n" + ("# edit\n" if restart else "")
                    )
                    index.updated = 0
                    self.assertEqual(index.candidates("Public"), ["pkg", "pkg.impl"])
                    self.assertEqual(run.call_count, 0 if restart else 1)
                    self.assertEqual(batch.call_count, 0 if restart else 1)

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

    def test_compiled_reexports_are_verified_ranked_and_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "pkg.mojoc"
            package.touch()
            environment = {**os.environ, "MODULAR_MOJO_MAX_IMPORT_PATH": str(root)}
            index = bridge.ImportIndex([root], Path(sys.executable), root / ".cache", environment)
            exports = {"pkg": [], "pkg.raw": [], "pkg.raw.types": ["KEY_Z", "Hidden"]}

            def check_import(args, **kwargs):
                text = Path(args[2]).read_text()
                self.assertIn(str(root), args)
                self.assertLessEqual(kwargs["timeout"], 2)
                self.assertEqual(kwargs["env"], environment)
                return subprocess.CompletedProcess(
                    args, 0 if text == "from pkg import KEY_Z\n" else 1
                )

            with patch.object(index, "compiled_exports", return_value=exports), patch.object(
                subprocess, "run", side_effect=check_import
            ) as run:
                self.assertEqual(index.candidates("KEY_Z"), ["pkg", "pkg.raw.types"])
                self.assertEqual(run.call_count, 2)
                index.updated = 0
                self.assertEqual(index.candidates("KEY_Z"), ["pkg", "pkg.raw.types"])
                self.assertEqual(run.call_count, 2)
                self.assertEqual(index.candidates("Hidden"), ["pkg.raw.types"])
                # A new build can change re-exports without changing documented names.
                package.write_bytes(b"new build")
                index.updated = 0
                run.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess([], 1)
                self.assertEqual(index.candidates("KEY_Z"), ["pkg.raw.types"])
                self.assertEqual(run.call_count, 6)

    def test_compiled_reexport_probe_failure_keeps_defining_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pkg.mojoc").touch()
            index = bridge.ImportIndex([root], Path(sys.executable), root / ".cache")
            with patch.object(
                index, "compiled_exports", return_value={"pkg.impl": ["Thing"]}
            ), patch.object(
                subprocess, "run", side_effect=subprocess.TimeoutExpired("mojo", 2)
            ) as run:
                self.assertEqual(index.candidates("Thing"), ["pkg.impl"])
                run.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
                self.assertEqual(index.candidates("Thing"), ["pkg", "pkg.impl"])

    def test_compiled_reexport_probes_have_a_total_time_budget(self):
        index = bridge.ImportIndex([], Path(sys.executable), Path("unused"))
        index.symbols = {"KEY_Z": {"pkg.raw.types"}}
        index.compiled_modules = {"pkg.raw.types"}
        with patch.object(index, "refresh"), patch.object(
            bridge.time, "monotonic", side_effect=[0, 0, 3]
        ), patch.object(index, "verified_reexport", return_value=True) as verify:
            self.assertEqual(index.candidates("KEY_Z"), ["pkg", "pkg.raw.types"])
            verify.assert_called_once_with("pkg", "KEY_Z", 2)


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

    def test_defining_module_hint_does_not_outrank_public_reexport(self):
        self.diagnostic["message"] = (
            "use of unknown declaration 'sqrt'; Add 'from std.math.math import sqrt'"
        )
        actions = bridge.import_actions(self.params, self.text, self.index)
        self.assertEqual(
            [action["title"] for action in actions],
            ["Import sqrt from std.math", "Import sqrt from std.math.math"],
        )

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
    def test_no_settings_adds_project_compiler_and_downloaded_source_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            library = root / ".pixi/envs/default/lib/mojo"
            library.mkdir(parents=True)
            server = root / ".pixi/envs/default/bin/mojo-lsp-server"
            sources = root / "cache/stdlib-1.0.0/mojo/stdlib"
            sources.mkdir(parents=True)
            with patch.object(bridge, "configured_import_paths", return_value=[]), patch.object(
                bridge, "ensure_stdlib", return_value=sources
            ) as download:
                command, index, environment = bridge.prepare(
                    str(server), [], root, root / "cache", {"params": {}}
                )
            download.assert_called_once()
            self.assertEqual(download.call_args.args[1], {})
            self.assertEqual(
                command, [str(server), "-I", str(sources), "-I", str(root), "-I", str(library)]
            )
            self.assertIn(root, index.roots)
            self.assertIn(library, index.roots)
            self.assertEqual(environment["MODULAR_MOJO_MAX_IMPORT_PATH"], str(sources))

    def test_default_download_uses_matching_release_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache = root / "cache"

            def run(command, **kwargs):
                if command[1:] == ["--mojo-version"]:
                    return subprocess.CompletedProcess(command, 0, "Mojo 1.0.0\n", "")
                if command[1] == "clone":
                    self.assertIn("mojo/v1.0.0", command)
                    self.assertIn("--sparse", command)
                    (Path(command[-1]) / "mojo/stdlib/std").mkdir(parents=True)
                    (Path(command[-1]) / "mojo/stdlib/std/__init__.mojo").touch()
                else:
                    self.assertEqual(command[3:5], ["sparse-checkout", "set"])
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(subprocess, "run", side_effect=run) as commands, patch.object(
                bridge.shutil, "which", return_value="/usr/bin/git"
            ):
                sources = bridge.ensure_stdlib("mojo-lsp-server", {}, cache, [], root)
                self.assertEqual(sources, cache / "stdlib-1.0.0/mojo/stdlib")
                self.assertTrue((sources / "std/__init__.mojo").is_file())
                commands.reset_mock()
                self.assertEqual(
                    bridge.ensure_stdlib("mojo-lsp-server", {}, cache, [], root), sources
                )
                commands.assert_called_once_with(
                    ["mojo-lsp-server", "--mojo-version"],
                    capture_output=True, text=True, timeout=10, check=True,
                )

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
            # Short-lived doc probes must retain the fast compiled-stdlib setup.
            self.assertEqual(index.environment, dict(os.environ))

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
    def test_pipe_reader_keeps_initialize_read_ahead(self):
        read_fd, write_fd = os.pipe()
        try:
            data = io.BytesIO()
            messages = [
                {"id": 1, "method": "initialize"},
                {"method": "initialized"},
                {"id": 2, "result": "😀"},
            ]
            for message in messages:
                bridge.write_message(data, message)
            os.write(write_fd, data.getvalue())
            os.close(write_fd)
            write_fd = None
            reader = bridge.PipeReader(read_fd)
            self.assertEqual([bridge.read_message(reader) for _ in messages], messages)
            self.assertIsNone(bridge.read_message(reader))
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

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
    def __init__(self, command, root, options, *, timeout=45, env=None):
        self.stderr = tempfile.TemporaryFile()
        self.timeout = timeout
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, env=env
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
                    "window": {"workDoneProgress": True},
                    "textDocument": {
                        "definition": {"linkSupport": True},
                        "codeAction": {
                            "codeActionLiteralSupport": {
                                "codeActionKind": {"valueSet": ["quickfix"]}
                            }
                        },
                    },
                },
                "initializationOptions": options,
            },
        )
        self.notify("initialized", {})

    def receive(self):
        message = self.messages.get(timeout=self.timeout)
        if message is None:
            self.stderr.seek(0)
            raise AssertionError(self.stderr.read().decode())
        if message.get("method") == "textDocument/publishDiagnostics":
            self.diagnostics[message["params"]["uri"]] = message["params"]
        if "method" in message and "id" in message:
            bridge.write_message(
                self.process.stdin, {"jsonrpc": "2.0", "id": message["id"], "result": None}
            )
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
            self.stderr.seek(0)
            errors = self.stderr.read().decode()
            self.assert_clean_exit(errors)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()

    def assert_clean_exit(self, errors):
        if self.process.returncode != 0 or "Fatal Python error" in errors:
            raise AssertionError(errors or f"Bridge exit status {self.process.returncode}")


class ProxyTests(unittest.TestCase):
    def test_native_progress_handshake_disabled_on_launch_and_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
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
            options = {"zed_mojo": {"download_stdlib": False}}
            client = LspClient(command, root, options)
            try:
                params = client.request("test/initializeParams", {})
                self.assertIs(params["capabilities"]["window"]["workDoneProgress"], False)
                self.assertTrue(
                    params["capabilities"]["textDocument"]["definition"]["linkSupport"]
                )
                self.assertEqual(params["initializationOptions"], options)
                self.assertEqual(params["rootUri"], root.as_uri())
                client.notify("test/crash", {})
                while True:
                    message = client.receive()
                    if (
                        message.get("method") == "window/logMessage"
                        and "unsaved documents restored" in message["params"]["message"]
                    ):
                        break
                self.assertEqual(client.request("test/initializeParams", {}), params)
            finally:
                client.close()

    def test_cancelled_worker_cannot_reply_to_reused_request_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            gate = root / "release-worker"
            wrapper = (
                f"import sys; sys.path.insert(0, {str(Path(bridge.__file__).parent)!r})\n"
                "import mojo_lsp, time\n"
                "from pathlib import Path\n"
                "def slow_actions(*args):\n"
                "    deadline = time.monotonic() + 10\n"
                f"    while not Path({str(gate)!r}).exists() and time.monotonic() < deadline:\n"
                "        time.sleep(0.01)\n"
                "    return []\n"
                "mojo_lsp.import_actions = slow_actions\n"
                "sys.exit(mojo_lsp.main() or 0)\n"
            )
            command = [
                sys.executable,
                "-c",
                wrapper,
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
                uri = client.open(root / "main.mojo", "# unsaved\n")
                ident = "reused-action-id"
                params = {
                    "textDocument": {"uri": uri},
                    "range": span(0, 0, 0),
                    "context": {"diagnostics": []},
                }
                for marker in ("cancelled", "current"):
                    bridge.write_message(
                        client.process.stdin,
                        {
                            "jsonrpc": "2.0",
                            "id": ident,
                            "method": "textDocument/codeAction",
                            "params": {**params, "testMarker": marker},
                        },
                    )
                    if marker == "cancelled":
                        # The fake server replies in order: echo is a barrier for
                        # the native code-action result being queued for augmentation.
                        client.request("test/echo", {})
                        client.notify("$/cancelRequest", {"id": ident})
                    else:
                        client.request("test/echo", {})
                        gate.touch()
                    while True:
                        response = client.receive()
                        if response.get("id") == ident:
                            break
                    if marker == "cancelled":
                        self.assertEqual(response["error"]["code"], -32800)
                    else:
                        self.assertEqual(response["result"][0]["data"]["marker"], "current")
            finally:
                gate.touch()
                client.close()

    def test_recovery_handshake_timeout_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            wrapper = (
                f"import sys; sys.path.insert(0, {str(Path(bridge.__file__).parent)!r}); "
                "import mojo_lsp; mojo_lsp.RECOVERY_TIMEOUT = 0.2; "
                "sys.exit(mojo_lsp.main() or 0)"
            )
            command = [
                sys.executable,
                "-c",
                wrapper,
                "--server",
                sys.executable,
                "--workspace",
                str(root),
                "--cache",
                str(root / ".cache"),
                "--",
                str(Path(__file__).with_name("fake_server.py")),
            ]
            client = LspClient(
                command,
                root,
                {
                    "zed_mojo": {"download_stdlib": False},
                    "test_restart_marker": str(root / "crashed"),
                },
            )
            try:
                client.notify("test/crash", {})
                self.assertEqual(client.process.wait(timeout=10), 1)
                client.stderr.seek(0)
                errors = client.stderr.read().decode()
                self.assertIn("timed out while reinitializing", errors)
                self.assertNotIn("Fatal Python error", errors)
            finally:
                if client.process.poll() is None:
                    client.process.kill()
                    client.process.wait()
                client.process.stdin.close()
                client.process.stdout.close()
                client.stderr.close()

    def test_backend_failure_does_not_abort_bridge_with_stdin_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
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
            client = LspClient(
                command, root, {"zed_mojo": {"download_stdlib": False, "restart_limit": 0}}
            )
            try:
                client.notify("test/crash", {})
                # Keep the editor side of stdin open, just like Zed after a crash.
                code = client.process.wait(timeout=10)
                client.stderr.seek(0)
                errors = client.stderr.read().decode()
                self.assertNotIn("Fatal Python error", errors)
                self.assertEqual(code, 1, errors)
                self.assertIn("23", errors)
            finally:
                if client.process.poll() is None:
                    client.process.kill()
                    client.process.wait()
                client.process.stdin.close()
                client.process.stdout.close()
                client.stderr.close()

    def test_recovery_restores_unsaved_documents_and_stops_crash_loops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
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
                uri = client.open(root / "unsaved.mojo", "def main():\n    pass\n")
                closed = client.open(root / "closed.mojo", "# close me\n")
                client.notify("textDocument/didClose", {"textDocument": {"uri": closed}})
                settings = {"settings": {"keep": "configuration"}}
                client.notify("workspace/didChangeConfiguration", settings)
                for version in (2, 3):
                    client.sequence += 1
                    ident = client.sequence
                    bridge.write_message(
                        client.process.stdin,
                        {"jsonrpc": "2.0", "id": ident, "method": "test/hang", "params": {}},
                    )
                    client.notify("test/startProgress", {})
                    client.notify("test/crash", {})
                    text = f"# unsaved edit {version} 😀\ndef main():\n    pass\n"
                    client.notify(
                        "textDocument/didChange",
                        {
                            "textDocument": {"uri": uri, "version": version},
                            "contentChanges": [{"text": text}],
                        },
                    )
                    replies, ended = [], False
                    while True:
                        message = client.receive()
                        if message.get("id") == ident:
                            replies.append(message)
                        if (
                            message.get("method") == "$/progress"
                            and message["params"]["value"]["kind"] == "end"
                        ):
                            ended = True
                        if (
                            message.get("method") == "window/logMessage"
                            and "unsaved documents restored" in message["params"]["message"]
                        ):
                            break
                    self.assertEqual(len(replies), 1)
                    self.assertIn("error", replies[0])
                    self.assertTrue(ended)
                    state = client.request("test/state", {})
                    self.assertEqual(state["configuration"], settings)
                    self.assertEqual(set(state["documents"]), {uri})
                    self.assertEqual(state["documents"][uri]["version"], version)
                    self.assertEqual(state["documents"][uri]["text"], text)
                    self.assertEqual(
                        client.request("test/echo", {"healthy": True}), {"healthy": True}
                    )
                client.notify("test/crash", {})
                self.assertEqual(client.process.wait(timeout=10), 1)
                client.stderr.seek(0)
                errors = client.stderr.read().decode()
                self.assertIn("limit reached", errors)
                self.assertNotIn("Fatal Python error", errors)
            finally:
                if client.process.poll() is None:
                    client.process.kill()
                    client.process.wait()
                client.process.stdin.close()
                client.process.stdout.close()
                client.stderr.close()

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
    os.environ.get("MOJO_LSP_SERVER"),
    "set MOJO_LSP_SERVER for the cold-cache, zero-configuration integration test",
)
class ZeroConfigurationIntegrationTests(unittest.TestCase):
    def test_cold_start_and_restart_without_settings_or_activation(self):
        server = Path(os.environ["MOJO_LSP_SERVER"]).resolve()
        with tempfile.TemporaryDirectory(prefix="zed mojo fresh project ") as temporary:
            root = Path(temporary).resolve()
            cache = root / ".cache"
            (root / "helpers.mojo").write_text(
                'def greeting() -> String:\n    return "hello"\n'
            )
            text = (
                "from std.testing import assert_equal\n"
                "from helpers import greeting\n\n"
                "def main() raises:\n"
                "    var message: String = greeting()\n"
                '    assert_equal(message, "hello")\n'
            )
            main = root / "main.mojo"
            main.write_text(text)
            environment = os.environ.copy()
            # Do not inherit a developer's activated environment or source paths.
            environment.update({
                "MODULAR_HOME": str(root / "no-global-configuration"),
                "MOJO_IMPORT_PATH": "",
                "MODULAR_MOJO_MAX_IMPORT_PATH": "",
            })
            command = [
                sys.executable, str(Path(bridge.__file__)),
                "--server", str(server), "--workspace", str(root),
                "--cache", str(cache), "--",
            ]
            self.assertFalse(cache.exists())
            for cold in (True, False):
                with self.subTest(cold_cache=cold):
                    client = LspClient(command, root, {}, timeout=180, env=environment)
                    try:
                        uri = client.open(main, text)
                        self.assertEqual(client.diagnostics[uri]["diagnostics"], [])
                        for line, symbol, target in (
                            (0, "assert_equal", "/std/testing/testing.mojo"),
                            (4, "String", "/std/collections/string/string.mojo"),
                            (4, "greeting", "/helpers.mojo"),
                        ):
                            locations = client.request("textDocument/definition", {
                                "textDocument": {"uri": uri},
                                "position": position(line, text.splitlines()[line].index(symbol)),
                            })
                            self.assertTrue(
                                any(item["uri"].endswith(target) for item in (locations or [])),
                                (symbol, locations),
                            )
                        completion = client.request("textDocument/completion", {
                            "textDocument": {"uri": uri},
                            "position": position(5, len("    assert_")),
                            "context": {"triggerKind": 1},
                        })
                        items = completion.get("items", []) if isinstance(completion, dict) else completion
                        self.assertTrue(any("assert_equal" in item["label"] for item in items))
                        tokens = client.request("textDocument/semanticTokens/full", {
                            "textDocument": {"uri": uri},
                        })
                        self.assertTrue(tokens["data"])
                        broken = client.open(root / "broken.mojo", "def main():\n    missing_value()\n")
                        self.assertTrue(any(
                            "missing_value" in item["message"]
                            for item in client.diagnostics[broken]["diagnostics"]
                        ))
                        self.assertTrue(list(cache.glob("stdlib-*/mojo/stdlib/std/__init__.mojo")))
                        client.stderr.seek(0)
                        log = client.stderr.read().decode()
                        self.assertEqual("Downloading stdlib sources" in log, cold)
                        self.assertNotIn("Stdlib source navigation unavailable", log)
                        self.assertFalse((root / ".zed").exists())
                    finally:
                        client.close()
            build = subprocess.run([
                str(server.with_name("mojo")), "build", str(main),
                "-I", str(root), "-I", str(server.parent.parent / "lib/mojo"),
                # Compile without linking: Pixi activation is only needed for
                # the executable's runtime/linker paths, not language features.
                "--emit", "llvm", "-o", str(root / "smoke.ll"),
            ], capture_output=True, text=True, timeout=90, env=environment)
            self.assertEqual(build.returncode, 0, build.stderr)


@unittest.skipUnless(
    os.environ.get("MOJO_LSP_SERVER") and os.environ.get("MOJO_STDLIB_PATH"),
    "set MOJO_LSP_SERVER and MOJO_STDLIB_PATH for real-server integration tests",
)
class IntegrationTests(unittest.TestCase):
    def test_progress_race_reproducer_through_bridge(self):
        script = Path(__file__).resolve().parents[1] / "scripts/repro-lsp-progress-race.py"
        for use_sources in (False, True):
            with self.subTest(stdlib_sources=use_sources):
                command = [
                    sys.executable,
                    str(script),
                    "--bridge",
                    "--server",
                    os.environ["MOJO_LSP_SERVER"],
                ]
                if use_sources:
                    command.extend(["--stdlib-path", os.environ["MOJO_STDLIB_PATH"]])
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS: hover returned", result.stdout)

    def test_navigation_and_import_fixes_with_real_compiler(self):
        server = Path(os.environ["MOJO_LSP_SERVER"]).resolve()
        compiler = server.with_name("mojo")
        with tempfile.TemporaryDirectory(prefix="zed-mojo-integration-") as temporary:
            root = Path(temporary).resolve()
            packages = root / ".packages"
            packages.mkdir()
            source = root / ".package-source/geometry"
            source.mkdir(parents=True)
            (source / "__init__.mojo").write_text("from .impl import Widget\nfrom .keys import *\n")
            (source / "impl.mojo").write_text(
                "struct Widget(Copyable, Movable):\n    var value: Int\n    def __init__(out self, value: Int):\n        self.value = value\n"
            )
            (source / "keys.mojo").write_text("comptime KEY_Z = 90\n")
            (source / "internal.mojo").write_text("comptime NOT_REEXPORTED = 12\n")
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
            index = bridge.ImportIndex(
                [packages, server.parent.parent / "lib/mojo"], compiler, root / ".index"
            )
            self.assertEqual(index.candidates("Widget"), ["geometry", "geometry.impl"])
            self.assertEqual(
                [name for name in index.candidates("KEY_Z") if name.split(".")[0] == "geometry"],
                ["geometry", "geometry.keys"],
            )
            self.assertEqual(index.candidates("NOT_REEXPORTED"), ["geometry.internal"])
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
                broken = "def main():\n    print(sqrt(1.0))\n    var widget = Widget(7)\n    print(widget.value)\n    print(KEY_Z)\n"
                broken_uri = client.open(root / "missing.mojo", broken)
                diagnostics = client.diagnostics[broken_uri]["diagnostics"]
                fixed = broken
                for symbol, line, column, expected in (
                    ("sqrt", 1, 11, "std.math"),
                    ("Widget", 2, 17, "geometry"),
                    ("KEY_Z", 4, 11, "geometry"),
                ):
                    actions = client.request(
                        "textDocument/codeAction",
                        {
                            "textDocument": {"uri": broken_uri},
                            "range": span(line, column, column),
                            "context": {"diagnostics": diagnostics, "only": ["quickfix"]},
                        },
                    )
                    action = actions[0]
                    self.assertEqual(action["title"], f"Import {symbol} from {expected}")
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
