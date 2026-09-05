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
from urllib.parse import unquote, urlparse

IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]*"
UNKNOWN = re.compile(r"use of unknown declaration '(" + IDENTIFIER + r")'")
IMPORT_HINT = re.compile(r"Add 'from ([A-Za-z_][\w.]*) import (" + IDENTIFIER + r")'")
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


class ImportIndex:
    def __init__(self, roots, compiler, cache, environment=None):
        self.roots, self.compiler, self.cache = roots, compiler, cache
        self.environment = environment
        self.parsed, self.symbols = {}, {}
        self.compiled_modules, self.reexports = set(), {}
        self.updated = 0

    def compiled_exports(self, path, module):
        # Mojo 1.0's doc tool crashes on std.mojoc. Index the matching source tree.
        if module == "std" or not self.compiler.is_file():
            return {}
        stat, compiler_stat = path.stat(), self.compiler.stat()
        key = hashlib.sha256(
            repr(
                (
                    str(path),
                    stat.st_mtime_ns,
                    stat.st_size,
                    str(self.compiler),
                    compiler_stat.st_mtime_ns,
                    [str(p) for p in self.roots],
                )
            ).encode()
        ).hexdigest()
        self.cache.mkdir(parents=True, exist_ok=True)
        cached = self.cache / f"{key}.json"
        if cached.is_file():
            return json.loads(cached.read_text())
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
            staging = Path(temporary) / "index.json"
            staging.write_text(json.dumps(exports))
            os.replace(staging, cached)
            return exports

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
        for root in self.roots:
            for path, module, is_package in module_files(root):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    stat = path.stat()
                    stamp = (stat.st_mtime_ns, stat.st_size)
                    cached = self.parsed.get(path)
                    if cached and cached[0] == stamp:
                        exports = cached[1]
                    elif path.suffix == ".mojo":
                        exports = source_exports(
                            path.read_text(encoding="utf-8"), module, is_package
                        )
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
                    if path.is_file():
                        stat = path.stat()
                        self.parsed[path] = ((stat.st_mtime_ns, stat.st_size), {})
        # Resolve relative imports, aliases and wildcard re-exports, including cycles.
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
        symbols = {}
        for module, names in modules.items():
            if any(part.startswith("_") for part in module.split(".")):
                continue
            for name in names:
                if re.fullmatch(IDENTIFIER, name) and not name.startswith("_"):
                    symbols.setdefault(name, set()).add(module)
        self.symbols = symbols
        self.compiled_modules = compiled_modules
        self.parsed = {path: value for path, value in self.parsed.items() if path in seen}
        if previous != {path: value[0] for path, value in self.parsed.items()}:
            self.reexports.clear()
        self.updated = time.monotonic()

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
        for parent in sorted(parents - candidates - excluded, key=import_rank):
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
    index = ImportIndex(
        unique_paths(([stdlib] if stdlib else []) + paths, workspace),
        Path(server).with_name("mojo" + (".exe" if os.name == "nt" else "")),
        cache / "import-index",
        # Metadata/import checks need no source locations. Keep the compiler's
        # original stdlib configuration: reparsing stdlib sources in each short-
        # lived doc process is much slower than the server's incremental parse.
        os.environ.copy(),
    )
    return command, index, environment


def run_proxy(command, index, initialize, workspace, environment):
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
    )
    events = queue.Queue()
    documents, pending = {}, {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def read_loop(label, stream):
        try:
            while (message := read_message(stream)) is not None:
                events.put((label, message))
        except (OSError, ValueError, EOFError) as error:
            log(f"{label} transport: {error}")
        finally:
            events.put((label, None))

    def finish_actions(message, request, snapshot):
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
        events.put(("actions", (message, request["textDocument"]["uri"], snapshot[0])))

    threading.Thread(target=read_loop, args=("client", sys.stdin.buffer), daemon=True).start()
    threading.Thread(target=read_loop, args=("server", process.stdout), daemon=True).start()
    write_message(process.stdin, initialize)
    try:
        while True:
            source, message = events.get()
            if message is None:
                break
            if source == "actions":
                response, uri, version = message
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
                continue
            method, params = message.get("method"), message.get("params") or {}
            if source == "client":
                document = params.get("textDocument", {})
                uri = document.get("uri")
                if method == "textDocument/didOpen":
                    documents[uri] = (document["version"], document["text"])
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
                elif method == "textDocument/codeAction" and uri in documents:
                    pending[message["id"]] = (params, documents[uri])
                write_message(process.stdin, message)
                if method == "exit":
                    break
            else:
                request = pending.pop(message.get("id"), None) if method is None else None
                if request and "error" not in message:
                    pool.submit(finish_actions, message, *request)
                else:
                    write_message(sys.stdout.buffer, message)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("server_args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    initialize = read_message(sys.stdin.buffer)
    if not initialize:
        return
    args = options.server_args
    if args[:1] == ["--"]:
        args = args[1:]
    workspace = Path(options.workspace).resolve()
    command, index, environment = prepare(
        options.server, args, workspace, Path(options.cache), initialize
    )
    run_proxy(command, index, initialize, workspace, environment)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        log(str(error))
        sys.exit(1)
