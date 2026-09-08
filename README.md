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

Have a working Mojo installation, Python 3.10 or newer (`python3`), and Git available on the development machine (the server for SSH projects). The extension automatically discovers the nearest project's `.pixi/envs/default` Mojo installation, falling back to `PATH`. No `.zed/settings.json` is required for this setup. Python runs the bundled language-server bridge; it has no third-party dependencies. Git is used for the first stdlib source download.

1. Open Zed.
2. Run `zed: extensions` from the command palette.
3. Click `Install Dev Extension`.
4. Select this repository directory: `zed_mojo`.
5. Open a `.mojo` file.

Installing this checkout as a dev extension replaces the published Mojo extension. Zed shows the published version as "Overridden by dev extension" and uses this local checkout for future development. After changing the extension, use its `Rebuild` button on the extensions page.

If the extension does not appear immediately, reload Zed with `zed: reload window`.

## Mojo language server

This extension registers `mojo-lsp-server` as the default language server for Mojo files. Completion, diagnostics, go-to-definition, hover, and other semantic features come from that LSP server.

By default the extension searches the worktree and its parent directories for `.pixi/envs/default/bin/mojo-lsp-server`, then falls back to the worktree's `PATH`. This also works when opening a subdirectory or single file. The project environment takes precedence over a global installation. The bridge automatically supplies project and compiler import paths and downloads matching stdlib sources; activating Pixi or configuring project settings is unnecessary.

For a new stable Mojo project, follow the [Mojo installation guide](https://mojolang.org/install/):

```sh
pixi init my-project -c https://conda.modular.com/max/ -c conda-forge
cd my-project
pixi add mojo
```

Open that project in Zed and open a `.mojo` file. For SSH development, create/install the project on the server; keep the dev extension checkout and Rust build tools on the local computer. Python 3.10+ and Git must be available on the server. No machine-specific paths, Pixi activation, or `.zed` directory are needed. Different projects select their own installed compiler; matching stdlib sources are cached once per compiler release.

For existing projects, run `pixi install` first. Discovery does not install or change your project's dependencies. Non-default Pixi environments and installations outside `.pixi/envs/default` use `PATH` or the optional override below. Nightly/custom compilers without a matching public release tag need matching sources supplied explicitly; offline first use also needs a pre-populated source cache or source override.

After rebuilding a dev extension, wait for Zed to upload it to the SSH server, then run `editor: restart language server` with a Mojo file focused if the server has stopped. This is a dev-extension reload step, not per-project configuration.

No default diagnostic flags are passed to the server. The bridge adds source and package search paths with `-I`, and preserves the native server's arguments and environment configured under `initialization_options.zed_mojo.server`. If you want `mojo-lsp-server` to additionally parse and type-check code blocks inside docstrings, pass `--check-docstrings` under that object's `arguments`. That check is off by default.

For installations outside Pixi, make sure the executable is visible from the environment that launches Zed:

```sh
which mojo-lsp-server
```

On Nix systems, launch Zed from a shell where `mojo-lsp-server` resolves successfully, or otherwise expose it through the environment used by Zed.

### Optional language-server overrides

Normal Pixi projects need no settings. For a custom installation or additional arguments, configure the native Mojo server under `initialization_options.zed_mojo.server`. This selects an explicit executable without bypassing the extension's Python bridge:

```json
{
  "lsp": {
    "mojo-lsp-server": {
      "initialization_options": {
        "zed_mojo": {
          "server": {
            "path": "/path/to/project/.pixi/envs/default/bin/mojo-lsp-server",
            "arguments": ["-I", "/path/to/project"],
            "env": {}
          }
        }
      },
      "settings": {}
    }
  }
}
```

Omit `server.path` to use automatic project/PATH discovery. The bridge adds the selected installation's `lib/mojo` directory and downloads matching standard-library sources automatically.

Do not use Zed's top-level `binary.path` or `binary.arguments` for these native-server options: `binary.path` bypasses the extension launcher entirely, and `binary.arguments` replaces the Python bridge's command-line arguments. Remove those overrides when migrating to the configuration above, then restart the language server. Setting `binary.path` to the native Mojo server intentionally disables the bridge's source download, import fixes, and crash protection.

If autocomplete does not appear, first verify that Zed can start the server, then check `zed: open log` for LSP startup errors. Repeated `mojo-lsp-server failed: server shut down` messages mean the server process exited and Zed is still draining stale requests; reload the window to restart it.

### Native-server crashes

The bridge disables the native server's optional work-progress handshake to prevent a reproduced Mojo 1.0.0 crash. The server can release its parse task while waiting for Zed to acknowledge `window/workDoneProgress/create`; hover or go-to-definition can then access an uninitialized parser context and segfault. This is a timing race, which can look like a random crash after editing. Disabling that capability keeps parsing in the document's task queue. Native parsing progress indicators are suppressed; semantic features remain available. See [the investigation and reproducer](docs/lsp-progress-race.md).

The bridge automatically restarts a native Mojo server that exits unexpectedly after initialization, up to twice in a rolling minute. It restores open documents from Zed's latest buffers (including unsaved edits), replays workspace configuration, and ends interrupted progress indicators. In-flight requests are cancelled rather than replayed against potentially changed text; invoke the action again after recovery. Old-server responses cannot complete new-server requests.

Recovery covers other native compiler/server crashes; it does not repair those bugs. If the same document repeatedly crashes the server, recovery stops and logs the exit status; save a reproduction and restart the language server manually. Set `initialization_options.zed_mojo.restart_limit` to `0` to disable automatic recovery (allowed values: `0`–`5`, default `2`). Startup failures are not retried, and a restarted server must finish initializing within 15 seconds.

The bridge's pipe reader avoids Python's buffered-stdin shutdown lock, so a native crash or ordinary shutdown should no longer cause the secondary `_enter_buffered_busy` Python abort.

### Standard-library navigation

On first use, the extension detects the server's Mojo version and caches the matching `mojo/v<version>` tag from [`modular/modular`](https://github.com/modular/modular), using a sparse checkout of the standard library. It adds those sources to the language server's search paths so native go-to-definition works for stdlib functions, types, built-ins, and methods. Project definitions still come from the same Mojo language server. First startup includes the source download; later starts reuse the cache.

An existing stdlib source directory on your import path is used before downloading. For an offline setup, a nightly/custom compiler, or a local stdlib checkout, set `stdlib_path` to the directory containing `std/`. Use sources matching your compiler: the extension deliberately does not substitute `main` for an unknown release. If downloading fails, the original server still starts and logs how to configure local sources.

### Import quick fixes

When Mojo reports an unknown declaration, open Zed's code-actions menu on the underlined symbol and choose **Import `<symbol>` from `<module>`**. For example, `sqrt(1.0)` offers `from std.math import sqrt`. The edit inserts an import while preserving the file's header, module docstring, existing imports, and line endings. Existing Mojo quick fixes remain available.

The import index searches the worktree, stdlib sources, `-I` directories, Mojo's configured import paths, `MOJO_IMPORT_PATH`, and the selected compiler's `lib/mojo` directory. Source packages include public declarations and re-exports; compiled packages are indexed with the matching `mojo doc` command and cached. Packages must already be installed or available on an import path.

Compiler documentation metadata omits compiled-package re-exports. The extension checks parent-package imports in batches and ranks confirmed shorter paths first (for example, `mojo_raylib` before `mojo_raylib.raw.types`). These checks resolve imports without running or linking package code. The results persist across language-server restarts and are invalidated when installed package artifacts, compiler, or import-path configuration change. Editing an unrelated project file does not rerun successful package-wide checks.

The automatically downloaded stdlib checkout is treated as immutable: its symbol index is built once per pinned checkout and saved to disk. Subsequent lookups do not walk or parse that tree. Explicit `stdlib_path` checkouts remain editable and are checked for changes, like project sources. Mutable import paths are checked at most once every two seconds; unchanged files do not rebuild the index.

The first lookup can include indexing and compiler work. Package-wide re-export checking has a ten-second budget per package; unsupported/incomplete compiler diagnostics fall back to bounded per-symbol checks (two seconds per lookup). Renamed compiled-only exports absent from metadata and dynamically generated or conditional source exports may not be discovered. Warm-cache performance therefore depends on whether package-wide indexing completed successfully.

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

Omit `stdlib_path` to detect/download sources automatically; `download_stdlib` defaults to `true`. Relative paths resolve against the discovered Pixi project root (otherwise the worktree). `import_paths` are added to both the server and import index. Restart the language server after changing these settings. To run the original server directly, set `initialization_options.zed_mojo.enabled` to `false`; this disables the added features and removes the Python requirement.

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
cargo check --locked
cargo test --locked --lib
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

The cold-cache zero-configuration integration test needs only a stable server executable, Git, and network access, with no activated environment or `MOJO_STDLIB_PATH`:

```sh
MOJO_LSP_SERVER=/path/to/project/.pixi/envs/default/bin/mojo-lsp-server \
PYTHONPATH=tests python3 -m unittest test_bridge.ZeroConfigurationIntegrationTests -v
```

It downloads matching sources into an empty temporary cache, verifies stdlib and project definitions, completion, semantic tokens, error diagnostics, and compilation, then repeats after restarting to verify cache reuse. CI runs this test against a freshly installed Pixi Mojo 1.0.0 environment. Rust tests cover per-project discovery, nested folders/single files, spaces in paths, and `PATH` fallback.

`extension.wasm` is a generated build artifact and is intentionally ignored by git.
