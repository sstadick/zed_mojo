"""Mojo LSP bridge: version-matched stdlib sources and missing-import fixes.

Uses only Python's standard library. Zed embeds this file in extension.wasm.
The Mojo server remains responsible for all semantic language features.
"""

import argparse
import ast
import concurrent.futures
import configparser
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import uuid
from urllib.parse import unquote, urlparse

IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]*"
UNKNOWN = re.compile(r"use of unknown declaration '(" + IDENTIFIER + r")'")
IMPORT_HINT = re.compile(r"Add 'from ([A-Za-z_][\w.]*) import (" + IDENTIFIER + r")'")
RECOVERY_TIMEOUT = 15
SKIP_DIRS = {
    "target",
    "build",
    "node_modules",
    "__pycache__",
    "tests",
    "test",
    "examples",
    "benchmarks",
}


def log(message):
    print(f"[zed-mojo] {message}", file=sys.stderr, flush=True)


class PipeReader:
    """Chunked pipe reads without holding Python's buffered-stdio locks.

    Zed can leave stdin open after the backend exits. A daemon reader must not
    hold sys.stdin.buffer's lock when Python finalizes its standard streams.
    The same reader is used for initialize and subsequent messages so any
    read-ahead bytes are retained.
    """

    def __init__(self, fd):
        self.fd = fd
        self.buffer = bytearray()

    def readline(self):
        while True:
            end = self.buffer.find(b"\n")
            if end >= 0:
                result = bytes(self.buffer[: end + 1])
                del self.buffer[: end + 1]
                return result
            chunk = os.read(self.fd, 65536)
            if not chunk:
                result = bytes(self.buffer)
                self.buffer.clear()
                return result
            self.buffer.extend(chunk)

    def read(self, size):
        if self.buffer:
            result = bytes(self.buffer[:size])
            del self.buffer[:size]
            return result
        return os.read(self.fd, size)


def read_message(stream):
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    length = int(headers["content-length"])
    if not 0 <= length <= 64 * 1024 * 1024:
        raise ValueError("invalid LSP message length")
    chunks = bytearray()
    while len(chunks) < length:
        chunk = stream.read(length - len(chunks))
        if not chunk:
            raise EOFError("truncated LSP message")
        chunks.extend(chunk)
    return json.loads(chunks)


def write_message(stream, message):
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
    stream.flush()


def file_path(uri):
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    path = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return Path(path)


def offset(text, position):
    """Translate LSP's UTF-16 coordinates to Python character offsets."""
    lines = text.splitlines(keepends=True)
    line, character = position["line"], position["character"]
    if line == len(lines) and character == 0:
        return len(text)
    if line < 0 or line >= len(lines) or character < 0:
        raise ValueError("position outside document")
    units = 0
    for column, char in enumerate(lines[line]):
        if units == character:
            return sum(map(len, lines[:line])) + column
        units += 2 if ord(char) > 0xFFFF else 1
    if units == character:
        return sum(map(len, lines[: line + 1]))
    raise ValueError("position outside line or inside surrogate pair")


def apply_changes(text, changes):
    for change in changes:
        if "range" not in change:
            text = change["text"]
        else:
            span = change["range"]
            start, end = offset(text, span["start"]), offset(text, span["end"])
            text = text[:start] + change["text"] + text[end:]
    return text


def top_level_statements(text):
    """Tokenize Mojo's Python-style imports without reading comments/docstrings.

    Function bodies and struct members are excluded; multiline import lists
    remain one statement. Incomplete editor buffers yield their complete prefix.
    """
    statement, depth = [], 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.INDENT:
                depth += 1
            elif token.type == tokenize.DEDENT:
                depth -= 1
            elif token.type == tokenize.NEWLINE:
                if statement:
                    yield statement
                statement = []
            elif depth == 0 and token.type not in (
                tokenize.NL,
                tokenize.COMMENT,
                tokenize.ENDMARKER,
            ):
                statement.append(token)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass


def import_node(statement):
    if not statement or statement[0].string not in ("from", "import"):
        return None
    try:
        return ast.parse(" ".join(token.string for token in statement)).body[0]
    except SyntaxError:
        return None


def source_exports(text, module, is_package):
    names, imports = set(), []
    for statement in top_level_statements(text):
        words = [token.string for token in statement]
        if len(words) >= 2 and words[0] in (
            "def",
            "fn",
            "struct",
            "trait",
            "alias",
            "comptime",
            "var",
        ):
            if re.fullmatch(IDENTIFIER, words[1]) and words[1] not in ("if", "for", "assert"):
                names.add(words[1])
        node = import_node(statement)
        if isinstance(node, ast.ImportFrom):
            base = module.split(".") if is_package else module.split(".")[:-1]
            if node.level:
                if node.level > len(base):
                    continue
                target = ".".join(
                    base[: len(base) - node.level + 1] + ([node.module] if node.module else [])
                )
            else:
                target = node.module or ""
            for alias in node.names:
                imports.append((target, alias.name, alias.asname or alias.name))
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return names, imports


def import_edit(text, module, symbol):
    """Insert a separate import after the leading docstring/import block.

    Existing comments, shebangs, multiline imports, and line endings survive.
    Avoid a duplicate binding, including imports aliased to the requested name.
    """
    insertion_line = 0
    for statement in top_level_statements(text):
        node = import_node(statement)
        if node:
            for alias in node.names:
                bound = alias.asname or (
                    alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
                )
                if bound == symbol or (
                    isinstance(node, ast.ImportFrom) and node.module == module and alias.name == "*"
                ):
                    return None
        if node or all(token.type == tokenize.STRING for token in statement):
            insertion_line = statement[-1].end[0]
        else:
            if insertion_line == 0:
                insertion_line = statement[0].start[0] - 1
            break
    lines = text.splitlines(keepends=True)
    if not any(top_level_statements(text)):
        insertion_line = len(lines)
    newline = "\r\n" if "\r\n" in text else "\n"
    inserted = f"from {module} import {symbol}{newline}"
    if insertion_line == len(lines) and text and not text.endswith(("\n", "\r")):
        position = {"line": len(lines) - 1, "character": len(lines[-1].encode("utf-16-le")) // 2}
        inserted = newline + inserted
    else:
        position = {"line": insertion_line, "character": 0}
    return {"range": {"start": position, "end": position}, "newText": inserted}


def unique_paths(paths, workspace):
    result = []
    for value in paths:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = workspace / path
        path = path.resolve()
        if path.is_dir() and path not in result:
            result.append(path)
    return result


def argument_paths(args):
    paths = []
    for index, arg in enumerate(args):
        if arg == "-I" and index + 1 < len(args):
            paths.append(args[index + 1])
        elif arg.startswith("-I") and len(arg) > 2:
            paths.append(arg[2:])
    return paths


def configured_import_paths():
    """Mojo's configuration import paths precede command-line -I directories."""
    value = os.environ.get("MODULAR_MOJO_MAX_IMPORT_PATH")
    if value is None:
        directory = Path(os.environ.get("MODULAR_HOME", Path.home() / ".modular"))
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read(directory / "modular.cfg")
            value = config.get("mojo-max", "import_path", fallback="")
        except configparser.Error as error:
            log(f"Cannot read Mojo import paths: {error}")
            value = ""
    # This setting uses commas, unlike MOJO_IMPORT_PATH's OS path separator.
    return [item.strip() for item in value.split(",") if item.strip()]


def stdlib_root(root):
    for path in (
        root,
        root.parent if root.name == "std" else root,
        root / "mojo/stdlib",
        root / "Mojo/stdlib",
    ):
        if (path / "std/__init__.mojo").is_file():
            return path
    return None


def ensure_stdlib(server, config, cache, paths, workspace):
    if config.get("stdlib_path"):
        path = Path(config["stdlib_path"]).expanduser()
        path = path if path.is_absolute() else workspace / path
        root = stdlib_root(path.resolve())
        if not root:
            raise ValueError(f"stdlib_path does not contain std/__init__.mojo: {path}")
        return root
    for path in paths:
        if root := stdlib_root(path):
            return root
    if not config.get("download_stdlib", True):
        return None
    version_output = subprocess.run(
        [server, "--mojo-version"], capture_output=True, text=True, timeout=10, check=True
    ).stdout
    match = re.fullmatch(r"Mojo (\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)\s*", version_output)
    if not match:
        raise ValueError(
            "cannot match this compiler to a release; set zed_mojo.stdlib_path for nightly/custom Mojo builds"
        )
    version = match[1]
    destination = cache / f"stdlib-{version}"
    if root := stdlib_root(destination):
        return root
    if not shutil.which("git"):
        raise ValueError(
            "Git is required to download stdlib sources; alternatively set zed_mojo.stdlib_path"
        )
    log(f"Downloading stdlib sources for Mojo {version} (once per version)")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stdlib-download-", dir=cache) as temporary:
        checkout = Path(temporary) / "modular"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--sparse",
                "--branch",
                f"mojo/v{version}",
                "https://github.com/modular/modular.git",
                str(checkout),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "sparse-checkout", "set", "mojo/stdlib", "Mojo/stdlib"],
            capture_output=True,
            check=True,
            timeout=60,
        )
        if not stdlib_root(checkout):
            raise ValueError(
                f"release mojo/v{version} does not contain expected stdlib sources; set zed_mojo.stdlib_path"
            )
        try:
            checkout.rename(destination)
        except OSError:
            if not stdlib_root(destination):
                raise
    return stdlib_root(destination)


def module_files(root):
    count = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(
            name for name in dirs if not name.startswith(".") and name not in SKIP_DIRS
        )
        for name in sorted(files):
            if not name.endswith((".mojo", ".mojoc", ".mojopkg")):
                continue
            path = Path(directory) / name
            parts = list(path.relative_to(root).with_suffix("").parts)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts.pop()
            if not parts or any(not re.fullmatch(IDENTIFIER, part) for part in parts):
                continue
            yield path, ".".join(parts), is_package
            count += 1
            if count >= 20000:
                log(f"Import index reached its 20,000-file limit for {root}")
                return


def doc_exports(declaration, module):
    """Read compiler-generated declarations without treating members as imports."""
    names = set()
    for key in ("aliases", "functions", "structs", "traits", "globals"):
        names.update(item["name"] for item in declaration.get(key, []) if "name" in item)
    if module:
        yield module, names
    for child in declaration.get("modules", []) + declaration.get("packages", []):
        child_module = module if child["name"] == "__init__" else f"{module}.{child['name']}"
        yield from doc_exports(child, child_module)


def import_rank(module):
    return module.count("."), module


def resolve_reexports(modules, references):
    for _ in range(len(references) + 1):
        changed = False
        for module, imports in references.items():
            for target, original, exported in imports:
                available = modules.get(target, set())
                additions = (
                    {name for name in available if not name.startswith("_")}
                    if original == "*"
                    else (
                        {exported}
                        if original in available or f"{target}.{original}" in modules
                        else set()
                    )
                )
                if additions - modules[module]:
                    modules[module].update(additions)
                    changed = True
        if not changed:
            break


def valid_exports(exports):
    return isinstance(exports, dict) and all(
        isinstance(name, str)
        and isinstance(symbols, list)
        and all(isinstance(symbol, str) for symbol in symbols)
        for name, symbols in exports.items()
    )


class ImportIndex:
    def __init__(self, roots, compiler, cache, environment=None, immutable_roots=None):
        self.roots, self.compiler, self.cache = roots, compiler, cache
        self.environment = environment
        self.parsed, self.symbols = {}, {}
        self.compiled_modules, self.reexports = set(), {}
        self.completed_packages = {}
        self.package_context = ()
        self.immutable_roots = immutable_roots or {}
        self.immutable_modules = {}
        self.file_signature = None
        self.updated = 0

    def immutable_exports(self, root):
        if root in self.immutable_modules:
            return self.immutable_modules[root]
        key = hashlib.sha256(
            repr(("source-imports-v1", str(root), self.immutable_roots[root])).encode()
        ).hexdigest()
        cached = self.cache / f"stdlib-{key}.json"
        try:
            exports = json.loads(cached.read_text())
            if not valid_exports(exports):
                raise ValueError("invalid stdlib index")
        except (OSError, ValueError):
            modules, references = {}, {}
            for path, module, package in module_files(root):
                if path.suffix == ".mojo":
                    names, imports = source_exports(
                        path.read_text(encoding="utf-8"), module, package
                    )
                    modules[module], references[module] = names, imports
            resolve_reexports(modules, references)
            exports = {module: sorted(names) for module, names in modules.items()}
            self.cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="source-index-", dir=self.cache) as temporary:
                staging = Path(temporary) / "index.json"
                staging.write_text(json.dumps(exports))
                os.replace(staging, cached)
        self.immutable_modules[root] = exports
        return exports

    def compiled_exports(self, path, module):
        # Mojo 1.0's doc tool crashes on std.mojoc. Index the matching source tree.
        if module == "std" or not self.compiler.is_file():
            return {}
        stat, compiler_stat = path.stat(), self.compiler.stat()
        key = hashlib.sha256(
            repr(
                (
                    "compiled-imports-v2",
                    str(path),
                    stat.st_mtime_ns,
                    stat.st_size,
                    str(self.compiler),
                    compiler_stat.st_mtime_ns,
                    [str(p) for p in self.roots],
                    self.package_context,
                    {
                        name: (self.environment or os.environ).get(name)
                        for name in (
                            "MODULAR_HOME",
                            "MODULAR_MOJO_MAX_IMPORT_PATH",
                            "MOJO_IMPORT_PATH",
                        )
                    },
                )
            ).encode()
        ).hexdigest()
        self.cache.mkdir(parents=True, exist_ok=True)
        cached = self.cache / f"{key}.json"
        self.completed_packages[path] = set()
        if cached.is_file():
            try:
                document = json.loads(cached.read_text())
                exports = document["exports"]
                if (
                    not valid_exports(exports)
                    or not isinstance(document["complete"], list)
                    or not all(
                        isinstance(name, str) and name in exports for name in document["complete"]
                    )
                ):
                    raise ValueError("invalid package index")
                self.completed_packages[path] = set(document["complete"])
                return exports
            except (OSError, ValueError, KeyError, TypeError):
                log(f"Rebuilding invalid cached import index for {module}")
        with tempfile.TemporaryDirectory(prefix="package-docs-", dir=self.cache) as temporary:
            output = Path(temporary) / "docs.json"
            args = [str(self.compiler), "doc", str(path), "-o", str(output)]
            for root in self.roots:
                args.extend(["-I", str(root)])
            subprocess.run(args, capture_output=True, timeout=30, check=True, env=self.environment)
            document = json.loads(output.read_text())
            exports = {}
            for name, symbols in doc_exports(document["decl"], module):
                exports[name] = sorted(set(exports.get(name, ())) | symbols)
            # Materialize each public package surface once, not once per symbol.
            # A trailing failing import proves that the compiler reached every
            # probe even when some names are intentionally not re-exported.
            deadline = time.monotonic() + 10
            for parent in sorted(exports, key=import_rank):
                if time.monotonic() >= deadline:
                    break
                descendants = {
                    symbol
                    for child, symbols in exports.items()
                    if child.startswith(parent + ".")
                    for symbol in symbols
                    if re.fullmatch(IDENTIFIER, symbol) and not symbol.startswith("_")
                } - set(exports[parent])
                if not descendants:
                    continue
                verified = self.probe_imports(
                    parent,
                    sorted(descendants),
                    Path(temporary),
                    max(0.01, deadline - time.monotonic()),
                )
                if verified is not None:
                    exports[parent] = sorted(set(exports[parent]) | verified)
                    self.completed_packages[path].add(parent)
            staging = Path(temporary) / "index.json"
            staging.write_text(
                json.dumps({"exports": exports, "complete": sorted(self.completed_packages[path])})
            )
            os.replace(staging, cached)
            return exports

    def probe_imports(self, module, symbols, temporary, timeout=10):
        sentinel = "_zed_mojo_probe_end_7c42b0a1"
        names = symbols + [sentinel]
        source = temporary / "imports.mojo"
        source.write_text(
            "".join(
                f"from {module} import {name} as _zed_import_{index}\n"
                for index, name in enumerate(names)
            ),
            encoding="utf-8",
        )
        args = [
            str(self.compiler),
            "doc",
            str(source),
            "--diagnostic-format",
            "json",
            "-o",
            str(temporary / "imports.json"),
        ]
        for root in self.roots:
            args.extend(["-I", str(root)])
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, env=self.environment
            )
            if result.returncode != 1:
                return None
            rejected = set()
            for line in result.stderr.splitlines():
                if not line.startswith("{"):
                    continue
                diagnostic = json.loads(line)
                if not isinstance(diagnostic, dict):
                    return None
                if diagnostic.get("kind") != "error":
                    continue
                detail = diagnostic.get("diagnostic", {})
                if not isinstance(detail, dict) or not isinstance(detail.get("location", {}), dict):
                    return None
                if not detail and diagnostic.get("message") == "could not generate documentation":
                    continue
                number = detail.get("location", {}).get("line", 0)
                if Path(
                    detail.get("file", "")
                ).resolve() != source.resolve() or not 1 <= number <= len(names):
                    return None
                symbol = names[number - 1]
                message = diagnostic.get("message", "")
                if not isinstance(message, str) or not (
                    message.endswith(f"does not contain '{symbol}'")
                    or message.endswith(f"has no declaration '{symbol}'")
                ):
                    return None
                rejected.add(symbol)
            if sentinel not in rejected:
                return None
            return set(symbols) - rejected
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
            log(f"Cannot index public imports for {module}: {error}")
            return None

    def verified_reexport(self, module, symbol, timeout):
        """Docs omit compiled imports. Ask the compiler instead of assuming a re-export."""
        key = (module, symbol)
        if key in self.reexports:
            return self.reexports[key]
        if not self.compiler.is_file():
            return False
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="import-probe-", dir=self.cache) as temporary:
                source = Path(temporary) / "probe.mojo"
                source.write_text(f"from {module} import {symbol}\n", encoding="utf-8")
                args = [
                    str(self.compiler),
                    "doc",
                    str(source),
                    "-o",
                    str(Path(temporary) / "doc.json"),
                ]
                for root in self.roots:
                    args.extend(["-I", str(root)])
                # Parse/resolve only: no package code is executed or linked.
                result = subprocess.run(
                    args, capture_output=True, timeout=timeout, env=self.environment
                )
                self.reexports[key] = result.returncode == 0
        except (OSError, subprocess.SubprocessError) as error:
            # Transient failures must not poison subsequent requests.
            log(f"Cannot check re-export {module}.{symbol}: {error}")
            return False
        return self.reexports[key]

    def refresh(self):
        if time.monotonic() - self.updated < 2:
            return
        modules, references, seen = {}, {}, set()
        compiled_modules = set()
        previous = {path: value[0] for path, value in self.parsed.items()}
        files, package_stamps = [], []
        for root in self.roots:
            if root not in self.immutable_roots:
                files.extend(module_files(root))
        stamps = {}
        for path, _, _ in files:
            try:
                stat = path.stat()
                stamps[path] = (stat.st_mtime_ns, stat.st_size)
                if path.suffix != ".mojo":
                    package_stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                pass
        signature = tuple(stamps.items())
        if signature == self.file_signature:
            self.updated = time.monotonic()
            return
        for root in self.immutable_roots:
            for module, names in self.immutable_exports(root).items():
                modules.setdefault(module, set(names))
        self.package_context = tuple(package_stamps)
        for path, module, is_package in files:
            if path in seen or path not in stamps:
                continue
            seen.add(path)
            try:
                stamp = stamps[path]
                if path.suffix != ".mojo":
                    stamp += (self.package_context,)
                cached = self.parsed.get(path)
                if cached and cached[0] == stamp:
                    exports = cached[1]
                elif path.suffix == ".mojo":
                    exports = source_exports(path.read_text(encoding="utf-8"), module, is_package)
                else:
                    exports = self.compiled_exports(path, module)
                self.parsed[path] = (stamp, exports)
                if isinstance(exports, dict):
                    for name, symbols in exports.items():
                        if name not in modules:
                            modules[name] = set(symbols)
                            compiled_modules.add(name)
                elif module not in modules:
                    modules[module], references[module] = set(exports[0]), exports[1]
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                log(f"Cannot index {path.name}: {error}")
                self.parsed[path] = (stamp, {})
        resolve_reexports(modules, references)
        symbols = {}
        for module, names in modules.items():
            if any(part.startswith("_") for part in module.split(".")):
                continue
            for name in names:
                if re.fullmatch(IDENTIFIER, name) and not name.startswith("_"):
                    symbols.setdefault(name, set()).add(module)
        self.symbols = symbols
        self.compiled_modules = compiled_modules
        self.completed_packages = {
            path: value for path, value in self.completed_packages.items() if path in seen
        }
        self.parsed = {path: value for path, value in self.parsed.items() if path in seen}
        if previous != {path: value[0] for path, value in self.parsed.items()}:
            self.reexports.clear()
        self.updated = time.monotonic()
        self.file_signature = signature

    def candidates(self, name, uri=None):
        self.refresh()
        excluded = set()
        document = file_path(uri) if uri else None
        if document:
            for root in self.roots:
                try:
                    parts = list(
                        document.resolve().relative_to(root.resolve()).with_suffix("").parts
                    )
                    if parts[-1] == "__init__":
                        parts.pop()
                    excluded.add(".".join(parts))
                except ValueError:
                    pass
        candidates = set(self.symbols.get(name, ())) - excluded
        parents = set()
        for module in candidates & self.compiled_modules:
            parts = module.split(".")
            parents.update(".".join(parts[:length]) for length in range(1, len(parts)))
        # Bound first-use latency. Successful/failed checks are reused until an
        # indexed file changes; timed-out or unvisited checks can be retried.
        deadline = time.monotonic() + 2
        complete = set().union(*self.completed_packages.values()) & self.compiled_modules
        for parent in sorted(parents - candidates - excluded - complete, key=import_rank):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self.verified_reexport(parent, name, remaining):
                candidates.add(parent)
        return sorted(candidates, key=import_rank)


def import_actions(params, text, index):
    only = params.get("context", {}).get("only")
    if only and not any(
        kind == "" or "quickfix" == kind or "quickfix".startswith(kind + ".") for kind in only
    ):
        return []
    actions, seen = [], set()
    requested = params["range"]
    start, end = offset(text, requested["start"]), offset(text, requested["end"])
    for diagnostic in params.get("context", {}).get("diagnostics", []):
        match = UNKNOWN.search(diagnostic.get("message", ""))
        if not match or diagnostic.get("source", "mojo") != "mojo":
            continue
        symbol = match[1]
        span = diagnostic["range"]
        first, last = offset(text, span["start"]), offset(text, span["end"])
        if first > end or last < start or text[first:last] != symbol:
            continue
        if text[:first].rstrip().endswith("."):
            continue
        hint = IMPORT_HINT.search(diagnostic["message"])
        candidates = ([hint[1]] if hint and hint[2] == symbol else []) + index.candidates(
            symbol, params["textDocument"]["uri"]
        )
        for module in sorted(set(candidates), key=import_rank)[:12]:
            if (module, symbol) in seen:
                continue
            seen.add((module, symbol))
            edit = import_edit(text, module, symbol)
            if edit:
                actions.append(
                    {
                        "title": f"Import {symbol} from {module}",
                        "kind": "quickfix",
                        "diagnostics": [diagnostic],
                        "edit": {"changes": {params["textDocument"]["uri"]: [edit]}},
                    }
                )
    return actions


def prepare(server, args, workspace, cache, initialize):
    options = initialize.get("params", {}).get("initializationOptions") or {}
    if not isinstance(options, dict):
        raise ValueError("initialization_options must be an object")
    config = options.get("zed_mojo", {})
    if not isinstance(config, dict):
        raise ValueError("initialization_options.zed_mojo must be an object")
    configured = config.get("import_paths", [])
    if not isinstance(configured, list) or not all(isinstance(path, str) for path in configured):
        raise ValueError("zed_mojo.import_paths must be an array of directory paths")
    config_paths = configured_import_paths()
    paths = unique_paths(
        config_paths
        + configured
        + argument_paths(args)
        + [value for value in os.environ.get("MOJO_IMPORT_PATH", "").split(os.pathsep) if value]
        + [workspace, Path(server).parent.parent / "lib/mojo"],
        workspace,
    )
    try:
        stdlib = ensure_stdlib(server, config, cache, paths, workspace)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        log(
            f"Stdlib source navigation unavailable: {error}. Set initialization_options.zed_mojo.stdlib_path to a matching checkout."
        )
        stdlib = None
    extra = unique_paths(([stdlib] if stdlib else []) + configured, workspace)
    command = [server]
    for path in extra:
        command.extend(["-I", str(path)])
    command.extend(args)
    command.extend(["-I", str(workspace)])
    # Explicit binary paths should work even without activating a Pixi environment.
    library = Path(server).parent.parent / "lib/mojo"
    if library.is_dir():
        command.extend(["-I", str(library)])
    environment = os.environ.copy()
    if stdlib:
        # Replacing only the server's configuration search path is necessary:
        # Pixi's MODULAR_HOME otherwise makes std.mojoc win over stdlib sources.
        environment["MODULAR_MOJO_MAX_IMPORT_PATH"] = ",".join(
            str(path) for path in unique_paths([stdlib] + config_paths, workspace)
        )
    immutable_roots = {}
    if stdlib and not config.get("stdlib_path"):
        for parent in stdlib.parents:
            if parent.parent == cache.resolve() and parent.name.startswith("stdlib-"):
                head = parent / ".git/HEAD"
                if head.is_file():
                    immutable_roots[stdlib] = head.read_text().strip()
                break
    index = ImportIndex(
        unique_paths(([stdlib] if stdlib else []) + paths, workspace),
        Path(server).with_name("mojo" + (".exe" if os.name == "nt" else "")),
        cache / "import-index",
        # Metadata/import checks need no source locations. Keep the compiler's
        # original stdlib configuration: reparsing stdlib sources in each short-
        # lived doc process is much slower than the server's incremental parse.
        os.environ.copy(),
        immutable_roots,
    )
    return command, index, environment


def backend_initialize(initialize):
    # Mojo's progress callback defers parsing until the client acknowledges
    # window/workDoneProgress/create, but releases the document's task chain
    # immediately. Hover/definition can then dereference a null parser context.
    # Disable that optional handshake so parsing stays inside its queued task.
    # Keep the client's message intact and use this on initial launch AND restart.
    params = initialize.get("params") or {}
    capabilities = params.get("capabilities") or {}
    window = capabilities.get("window") or {}
    return {
        **initialize,
        "params": {
            **params,
            "capabilities": {
                **capabilities,
                "window": {**window, "workDoneProgress": False},
            },
        },
    }


def run_proxy(command, index, initialize, workspace, environment, client_input):
    initialize = backend_initialize(initialize)
    config = (initialize.get("params", {}).get("initializationOptions") or {}).get("zed_mojo", {})
    restart_limit = config.get("restart_limit", 2)
    if type(restart_limit) is not int or not 0 <= restart_limit <= 5:
        raise ValueError("zed_mojo.restart_limit must be an integer between 0 and 5")
    events = queue.Queue()
    documents, pending = {}, {}
    language_ids, state_notifications = {}, []
    client_requests = {initialize["id"]}
    server_requests, progress_tokens = set(), set()
    cancelled_actions = set()
    jobs, restarts = {}, []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    stopped = threading.Event()
    ended_by = None
    shutdown_requested = False
    initialized = False
    generation = 0
    recovery_id = None
    recovery_deadline = None

    def read_loop(label, epoch, stream):
        try:
            while not stopped.is_set() and (message := read_message(stream)) is not None:
                events.put((label, epoch, message))
        except (OSError, ValueError, EOFError) as error:
            if not stopped.is_set():
                log(f"{label} transport: {error}")
        finally:
            events.put((label, epoch, None))

    def start_backend():
        child = subprocess.Popen(
            command,
            cwd=workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
        reader = threading.Thread(
            target=read_loop,
            args=("server", generation, PipeReader(child.stdout.fileno())),
            daemon=True,
        )
        reader.start()
        return child, reader

    def stop_backend(child, reader):
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        reader.join(timeout=1)
        child.stdout.close()

    def send_backend(message):
        try:
            write_message(process.stdin, message)
        except OSError:
            # Let the EOF handler perform recovery; preserve document updates
            # and fail outstanding requests rather than silently losing them.
            events.put(("server", generation, None))

    def fail_request(ident, code, reason):
        client_requests.discard(ident)
        write_message(
            sys.stdout.buffer,
            {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": reason}},
        )

    def finish_actions(epoch, token, message, request, snapshot):
        try:
            additions = import_actions(request, snapshot[1], index)
            result = message.get("result") or []
            titles = {action.get("title") for action in result}
            message = {
                **message,
                "result": result
                + [action for action in additions if action["title"] not in titles],
            }
        except Exception as error:
            log(f"Import fixes unavailable: {error}")
        events.put(
            ("actions", epoch, (token, message, request["textDocument"]["uri"], snapshot[0]))
        )

    process, server_reader = start_backend()
    threading.Thread(target=read_loop, args=("client", None, client_input), daemon=True).start()
    try:
        send_backend(initialize)
        while True:
            timeout = None
            if recovery_deadline is not None:
                timeout = recovery_deadline - time.monotonic()
                if timeout <= 0:
                    log("Native Mojo server timed out while reinitializing")
                    ended_by = "server"
                    break
            try:
                source, epoch, message = events.get(timeout=timeout)
            except queue.Empty:
                continue
            if source != "client" and epoch != generation:
                continue
            if message is None:
                if source == "client" or shutdown_requested:
                    ended_by = source
                    break
                stop_backend(process, server_reader)
                log(
                    f"Native Mojo language server exited unexpectedly (status {process.returncode})"
                )
                for ident in list(client_requests):
                    fail_request(ident, -32802, "Native Mojo server exited; request cancelled")
                for token in progress_tokens:
                    write_message(
                        sys.stdout.buffer,
                        {
                            "jsonrpc": "2.0",
                            "method": "$/progress",
                            "params": {
                                "token": token,
                                "value": {"kind": "end", "message": "Mojo server exited"},
                            },
                        },
                    )
                progress_tokens.clear()
                server_requests.clear()
                pending.clear()
                cancelled_actions.clear()
                for job, _ in jobs.values():
                    job.cancel()
                jobs.clear()
                now = time.monotonic()
                restarts = [stamp for stamp in restarts if now - stamp < 60]
                if not initialized or len(restarts) >= restart_limit:
                    log(
                        "Automatic restart unavailable or limit reached; restart the Mojo language server in Zed"
                    )
                    ended_by = "server"
                    break
                restarts.append(now)
                generation += 1
                recovery_id = "zed-mojo-restart-" + uuid.uuid4().hex
                recovery_deadline = now + RECOVERY_TIMEOUT
                log("Restarting native Mojo server and restoring unsaved documents")
                process, server_reader = start_backend()
                send_backend({**initialize, "id": recovery_id})
                continue
            if source == "actions":
                token, response, uri, version = message
                if jobs.get(response["id"], (None, None))[1] is not token:
                    continue
                jobs.pop(response["id"])
                if response["id"] not in client_requests:
                    continue
                if documents.get(uri, (None,))[0] != version:
                    response = {
                        "jsonrpc": "2.0",
                        "id": response["id"],
                        "error": {
                            "code": -32801,
                            "message": "Document changed while computing import fixes",
                        },
                    }
                write_message(sys.stdout.buffer, response)
                client_requests.discard(response["id"])
                continue
            method, params = message.get("method"), message.get("params") or {}
            if source == "client":
                if method is None:
                    if message.get("id") in server_requests:
                        server_requests.discard(message["id"])
                        send_backend(message)
                    continue
                if "id" in message:
                    client_requests.add(message["id"])
                if method == "shutdown":
                    shutdown_requested = True
                document = params.get("textDocument", {})
                uri = document.get("uri")
                if method == "textDocument/didOpen":
                    documents[uri] = (document["version"], document["text"])
                    language_ids[uri] = document.get("languageId", "mojo")
                elif method == "textDocument/didChange" and uri in documents:
                    try:
                        documents[uri] = (
                            document["version"],
                            apply_changes(documents[uri][1], params["contentChanges"]),
                        )
                    except ValueError:
                        documents.pop(uri, None)
                elif method == "textDocument/didClose":
                    documents.pop(uri, None)
                    language_ids.pop(uri, None)
                elif method in (
                    "workspace/didChangeConfiguration",
                    "workspace/didChangeWorkspaceFolders",
                ):
                    if method == "workspace/didChangeConfiguration":
                        state_notifications = [
                            item for item in state_notifications if item["method"] != method
                        ]
                    state_notifications.append(message)
                elif method == "textDocument/codeAction" and uri in documents:
                    pending[message["id"]] = (params, documents[uri])
                elif method == "$/cancelRequest" and (
                    params.get("id") in jobs or params.get("id") in pending
                ):
                    ident = params["id"]
                    job = jobs.pop(ident, None)
                    if job is not None:
                        job[0].cancel()
                        if ident in client_requests:
                            fail_request(ident, -32800, "Import fix request cancelled")
                    else:
                        # Keep the native request outstanding until its response;
                        # otherwise a reused client ID could match that late reply.
                        cancelled_actions.add(ident)
                if recovery_id is not None:
                    if method == "shutdown":
                        write_message(
                            sys.stdout.buffer,
                            {"jsonrpc": "2.0", "id": message["id"], "result": None},
                        )
                        client_requests.discard(message["id"])
                    elif "id" in message:
                        pending.pop(message["id"], None)
                        fail_request(
                            message["id"],
                            -32802,
                            "Native Mojo server is restarting; retry the request",
                        )
                else:
                    send_backend(message)
                if method == "exit":
                    break
            else:
                if method is None and recovery_id is not None and message.get("id") == recovery_id:
                    if "error" in message:
                        log(f"Native Mojo server could not reinitialize: {message['error']}")
                        ended_by = "server"
                        break
                    recovery_id = None
                    recovery_deadline = None
                    send_backend({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                    for notification in state_notifications:
                        send_backend(notification)
                    for uri, (version, contents) in documents.items():
                        send_backend(
                            {
                                "jsonrpc": "2.0",
                                "method": "textDocument/didOpen",
                                "params": {
                                    "textDocument": {
                                        "uri": uri,
                                        "version": version,
                                        "languageId": language_ids[uri],
                                        "text": contents,
                                    }
                                },
                            }
                        )
                    log("Native Mojo language server restarted; unsaved documents restored")
                    write_message(
                        sys.stdout.buffer,
                        {
                            "jsonrpc": "2.0",
                            "method": "window/logMessage",
                            "params": {
                                "type": 3,
                                "message": "Mojo language server restarted; unsaved documents restored",
                            },
                        },
                    )
                    continue
                if method is None:
                    if message.get("id") not in client_requests:
                        continue
                    if message.get("id") == initialize["id"] and "error" not in message:
                        initialized = True
                elif "id" in message:
                    server_requests.add(message["id"])
                if method == "$/progress":
                    if params.get("value", {}).get("kind") == "begin":
                        progress_tokens.add(params["token"])
                    elif params.get("value", {}).get("kind") == "end":
                        progress_tokens.discard(params["token"])
                request = pending.pop(message.get("id"), None) if method is None else None
                if method is None and message.get("id") in cancelled_actions:
                    cancelled_actions.discard(message["id"])
                    fail_request(message["id"], -32800, "Import fix request cancelled")
                elif request and "error" not in message:
                    if (
                        documents.get(request[0]["textDocument"]["uri"], (None,))[0]
                        != request[1][0]
                    ):
                        fail_request(
                            message["id"], -32801, "Document changed while computing import fixes"
                        )
                    else:
                        token = object()
                        jobs[message["id"]] = (
                            pool.submit(finish_actions, generation, token, message, *request),
                            token,
                        )
                else:
                    write_message(sys.stdout.buffer, message)
                    if method is None:
                        client_requests.discard(message["id"])
    finally:
        stopped.set()
        pool.shutdown(wait=False, cancel_futures=True)
        stop_backend(process, server_reader)
    if ended_by == "server" and not shutdown_requested:
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("server_args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    client_input = PipeReader(sys.stdin.fileno())
    initialize = read_message(client_input)
    if not initialize:
        return
    args = options.server_args
    if args[:1] == ["--"]:
        args = args[1:]
    workspace = Path(options.workspace).resolve()
    command, index, environment = prepare(
        options.server, args, workspace, Path(options.cache), initialize
    )
    return run_proxy(command, index, initialize, workspace, environment, client_input)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        log(str(error))
        sys.exit(1)
