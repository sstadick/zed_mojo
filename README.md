# Mojo language support for Zed

Zed extension that adds Mojo language support for files with the `.mojo` suffix.

This fork is maintained at [`sstadick/zed_mojo`](https://github.com/sstadick/zed_mojo), based on [`vadim-su/zed_mojo`](https://github.com/vadim-su/zed_mojo).

## What is included

- Zed extension metadata in `extension.toml`
- Mojo language config in `languages/mojo/config.toml`
- Syntax highlighting queries in `languages/mojo/highlights.scm`
- Editor queries for bracket matching, indentation, outline, and Vim text objects
- LSP integration for `mojo-lsp-server`
- Go-to-definition into matching Mojo standard-library sources
- Quick fixes to import missing symbols from the standard library, project, and installed packages
- Mojo snippets in `snippets/mojo.json`
- Runnable detection for `def main` / `fn main` in `languages/mojo/runnables.scm`
- Default runnable task binding in `languages/mojo/tasks.json`
- Tree-sitter grammar pinned to [`vadim-su/tree-sitter-mojo`](https://github.com/vadim-su/tree-sitter-mojo)

## Install locally in Zed

Have Mojo, Python 3.10 or newer (`python3`), and Git available in the environment Zed uses. Python runs the bundled language-server bridge; it has no third-party dependencies. Git is used for the first stdlib source download.

1. Open Zed.
2. Run `zed: extensions` from the command palette.
3. Click `Install Dev Extension`.
4. Select this repository directory: `zed_mojo`.
5. Open a `.mojo` file.

Installing this checkout as a dev extension replaces the published Mojo extension. Zed shows the published version as "Overridden by dev extension" and uses this local checkout for future development. After changing the extension, use its `Rebuild` button on the extensions page.

If the extension does not appear immediately, reload Zed with `zed: reload window`.

## Mojo language server

This extension registers `mojo-lsp-server` as the default language server for Mojo files. Completion, diagnostics, go-to-definition, hover, and other semantic features come from that LSP server.

The server is launched through the `PATH` inherited by Zed; the extension does not pin a machine-specific executable path.

No default diagnostic flags are passed to the server. The bridge adds source and package search paths with `-I`, and preserves your `binary.arguments` and `binary.env`. If you want `mojo-lsp-server` to additionally parse and type-check code blocks inside docstrings, pass `--check-docstrings` under `binary.arguments`. That check is off by default.

Before opening a `.mojo` file, make sure the executable is visible from the environment that launches Zed:

```sh
which mojo-lsp-server
```

On Nix systems, launch Zed from a shell where `mojo-lsp-server` resolves successfully, or otherwise expose it through the environment used by Zed.

### Configuring the language server

You can override the language server command, arguments, and environment in Zed settings. This is useful when Mojo imports require extra search paths via `-I`:

```json
{
  "lsp": {
    "mojo-lsp-server": {
      "binary": {
        "path": "mojo-lsp-server",
        "arguments": ["-I", "/path/to/mojo/packages"],
        "env": {}
      },
      "initialization_options": {},
      "settings": {}
    }
  }
}
```

If autocomplete does not appear, first verify that Zed can start the server, then check `zed: open log` for LSP startup errors. Repeated `mojo-lsp-server failed: server shut down` messages mean the server process exited and Zed is still draining stale requests; reload the window to restart it.

### Standard-library navigation

On first use, the extension detects the server's Mojo version and caches the matching `mojo/v<version>` tag from [`modular/modular`](https://github.com/modular/modular), using a sparse checkout of the standard library. It adds those sources to the language server's search paths so native go-to-definition works for stdlib functions, types, built-ins, and methods. Project definitions still come from the same Mojo language server. First startup includes the source download; later starts reuse the cache.

An existing stdlib source directory on your import path is used before downloading. For an offline setup, a nightly/custom compiler, or a local stdlib checkout, set `stdlib_path` to the directory containing `std/`. Use sources matching your compiler: the extension deliberately does not substitute `main` for an unknown release. If downloading fails, the original server still starts and logs how to configure local sources.

### Import quick fixes

When Mojo reports an unknown declaration, open Zed's code-actions menu on the underlined symbol and choose **Import `<symbol>` from `<module>`**. For example, `sqrt(1.0)` offers `from std.math import sqrt`. The edit inserts an import while preserving the file's header, module docstring, existing imports, and line endings. Existing Mojo quick fixes remain available.

The import index searches the worktree, stdlib sources, `-I` directories, Mojo's configured import paths, `MOJO_IMPORT_PATH`, and the selected compiler's `lib/mojo` directory. Source packages include public declarations and re-exports; compiled packages are indexed with the matching `mojo doc` command and cached. Packages must already be installed or available on an import path.

Compiler documentation metadata omits compiled-package re-exports. For known symbols, the extension checks whether parent-package imports compile and ranks confirmed shorter paths first (for example, `mojo_raylib` before `mojo_raylib.raw.types`). Checks resolve imports without running or linking package code, are cached until indexed files change, and have a two-second budget per symbol; unchecked paths can be retried on the next request. Renamed compiled-only exports absent from metadata and dynamically generated or conditional source exports may not be discovered.

### Source and bridge settings

These optional settings live under the existing language server's `initialization_options`:

```json
{
  "lsp": {
    "mojo-lsp-server": {
      "initialization_options": {
        "zed_mojo": {
          "stdlib_path": "/path/to/modular/mojo/stdlib",
          "download_stdlib": false,
          "import_paths": ["/path/to/packages"]
        }
      }
    }
  }
}
```

Omit `stdlib_path` to detect/download sources automatically; `download_stdlib` defaults to `true`. Relative paths resolve against the worktree. `import_paths` are added to both the server and import index. Restart the language server after changing these settings. To run the original server directly, set `initialization_options.zed_mojo.enabled` to `false`; this disables the added features and removes the Python requirement.

## Snippets

The extension includes snippets for common Mojo forms such as `fn main`, `def main`, `struct`, `trait`, `alias`, `var`, loops, conditionals, and tests. They are stored in `snippets/mojo.json` and appear alongside normal completion items.

## Running Mojo files from Zed

Zed runs code through Tasks. This extension marks `def main` / `fn main` as a runnable with the tag `mojo-main` and ships a default language task in `languages/mojo/tasks.json`:

```json
[
  {
    "label": "mojo run current file",
    "command": "mojo",
    "args": ["run", "$ZED_FILE"],
    "cwd": "$ZED_WORKTREE_ROOT",
    "save": "current",
    "use_new_terminal": false,
    "allow_concurrent_runs": false,
    "reveal": "always",
    "hide": "never",
    "tags": ["mojo-main"]
  }
]
```

After installing or updating the dev extension, reload Zed with `zed: reload window` and reopen a `.mojo` file. The inline runnable indicator should appear next to `def main` / `fn main` when `gutter.runnables` is enabled.

You can override the default action in a Mojo project's `.zed/tasks.json`, or globally via `zed: open tasks`, by defining your own task with `"tags": ["mojo-main"]`.

This assumes the `mojo` executable is available in the shell environment that Zed uses. If Zed cannot find it, launch Zed from a terminal where `mojo --version` works, or add Mojo to your shell `PATH`.

## Development

Run the native Rust checks:

```sh
cargo fmt --check
cargo check
```

Zed compiles Rust extensions to WebAssembly. To check that target locally, install it once and run the target check:

```sh
rustup target add wasm32-wasip1
cargo check --target wasm32-wasip1
rustup target add wasm32-wasip2
cargo build --release --target wasm32-wasip2
```

Run the extension query and snippet checks:

```sh
bash scripts/check-snippets.sh
bash scripts/check-indents.sh
bash scripts/check-highlight-order.sh
bash scripts/check-runnables.sh
python3 -m unittest discover -s tests -v
```

The bridge tests include a protocol peer to exercise message forwarding, native quick fixes, and unsaved edits without installing Mojo. To also run the real-server integration test, use an activated Mojo environment and set:

```sh
MOJO_LSP_SERVER="$(command -v mojo-lsp-server)" \
MOJO_STDLIB_PATH=/path/to/modular/mojo/stdlib \
python3 -m unittest discover -s tests -v
```

That test checks stdlib and local definitions, applies stdlib and compiled-package import fixes, and builds the resulting Mojo program. The bridge is embedded from `server/mojo_lsp.py` into the Rust extension; rebuild the extension after changing either file.

`extension.wasm` is a generated build artifact and is intentionally ignored by git.
