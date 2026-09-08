use zed_extension_api::{self as zed, Result};

const LANGUAGE_SERVER_ID: &str = "mojo-lsp-server";
const SERVER_NAME: &str = "mojo-lsp-server";
const LSP_BRIDGE: &str = include_str!("../server/mojo_lsp.py");

struct MojoExtension;

fn discover_server(
    root: &str,
    mut which: impl FnMut(&str) -> Option<String>,
) -> Result<(String, String)> {
    // A worktree can be a project subdirectory or a single file. Prefer the
    // nearest Pixi environment so a global installation cannot select a
    // different compiler (and therefore different stdlib sources).
    for directory in std::path::Path::new(root).ancestors() {
        let candidate = directory.join(".pixi/envs/default/bin/mojo-lsp-server");
        if let Some(server) = which(&candidate.to_string_lossy()) {
            return Ok((server, directory.to_string_lossy().into_owned()));
        }
    }
    which(SERVER_NAME)
        .map(|server| (server, root.to_string()))
        .ok_or_else(|| {
            "mojo-lsp-server was not found in a project .pixi/envs/default environment or PATH; \
             install the project's Mojo environment first"
                .to_string()
        })
}

impl zed::Extension for MojoExtension {
    fn new() -> Self {
        Self
    }

    fn language_server_command(
        &mut self,
        language_server_id: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<zed::Command> {
        let lsp_settings =
            zed::settings::LspSettings::for_worktree(language_server_id.as_ref(), worktree)?;

        // Zed consumes binary.path before calling an extension and replaces the
        // extension's argv with binary.arguments. Keep native-server options
        // separate so the Python bridge remains the process Zed launches.
        let binary = lsp_settings
            .initialization_options
            .as_ref()
            .and_then(|options| options.get("zed_mojo"))
            .and_then(|options| options.get("server"))
            .map(|server| {
                zed::serde_json::from_value::<zed::settings::CommandSettings>(server.clone())
            })
            .transpose()
            .map_err(|error| format!("invalid zed_mojo.server settings: {error}"))?;
        let mut args = Vec::new();
        let mut env = worktree.shell_env();
        let binary_path = binary.as_ref().and_then(|binary| binary.path.clone());

        if let Some(binary) = binary {
            if let Some(arguments) = binary.arguments {
                args.extend(arguments);
            }

            if let Some(binary_env) = binary.env {
                env.extend(binary_env);
            }
        }

        let (command, workspace) = match binary_path {
            Some(path) if path.contains('/') => (path, worktree.root_path()),
            Some(path) => (
                worktree
                    .which(&path)
                    .ok_or_else(|| format!("{path} must be available in PATH"))?,
                worktree.root_path(),
            ),
            None => discover_server(&worktree.root_path(), |path| worktree.which(path))?,
        };

        if lsp_settings
            .initialization_options
            .as_ref()
            .and_then(|options| options.get("zed_mojo"))
            .and_then(|options| options.get("enabled"))
            .and_then(|enabled| enabled.as_bool())
            == Some(false)
        {
            return Ok(zed::Command { command, args, env });
        }

        let python = worktree.which("python3").ok_or_else(|| {
            "python3 is required for Mojo stdlib navigation and import fixes; set \
             initialization_options.zed_mojo.enabled to false to use the server directly"
                .to_string()
        })?;
        let directory = std::env::current_dir().map_err(|error| error.to_string())?;
        let bridge = directory.join("mojo_lsp.py");
        if std::fs::read_to_string(&bridge).ok().as_deref() != Some(LSP_BRIDGE) {
            let staging = directory.join("mojo_lsp.py.tmp");
            std::fs::write(&staging, LSP_BRIDGE).map_err(|error| error.to_string())?;
            std::fs::rename(staging, &bridge).map_err(|error| error.to_string())?;
        }
        let mut bridge_args = vec![
            "-u".to_string(),
            bridge.to_string_lossy().into_owned(),
            "--server".to_string(),
            command,
            "--workspace".to_string(),
            workspace,
            "--cache".to_string(),
            directory.join("sources").to_string_lossy().into_owned(),
            "--".to_string(),
        ];
        bridge_args.extend(args);

        Ok(zed::Command {
            command: python,
            args: bridge_args,
            env,
        })
    }

    fn language_server_initialization_options(
        &mut self,
        language_server_id: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<Option<zed::serde_json::Value>> {
        if language_server_id.as_ref() != LANGUAGE_SERVER_ID {
            return Ok(None);
        }

        zed::settings::LspSettings::for_worktree(language_server_id.as_ref(), worktree)
            .map(|settings| settings.initialization_options)
    }

    fn language_server_workspace_configuration(
        &mut self,
        language_server_id: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> Result<Option<zed::serde_json::Value>> {
        if language_server_id.as_ref() != LANGUAGE_SERVER_ID {
            return Ok(None);
        }

        zed::settings::LspSettings::for_worktree(language_server_id.as_ref(), worktree)
            .map(|settings| settings.settings)
    }
}

zed::register_extension!(MojoExtension);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nested_project_environment_wins_over_parent_and_path() {
        let nested = "/work/project/.pixi/envs/default/bin/mojo-lsp-server";
        let parent = "/work/.pixi/envs/default/bin/mojo-lsp-server";
        let (server, workspace) =
            discover_server(
                "/work/project/src/example.mojo",
                |candidate| match candidate {
                    path if path == nested || path == parent => Some(path.to_string()),
                    SERVER_NAME => Some("/usr/local/bin/mojo-lsp-server".to_string()),
                    _ => None,
                },
            )
            .unwrap();
        assert_eq!(server, nested);
        assert_eq!(workspace, "/work/project");
    }

    #[test]
    fn non_pixi_projects_use_the_shell_installation() {
        let (server, workspace) = discover_server("/work/project", |candidate| {
            (candidate == SERVER_NAME).then(|| "/usr/local/bin/mojo-lsp-server".to_string())
        })
        .unwrap();
        assert_eq!(server, "/usr/local/bin/mojo-lsp-server");
        assert_eq!(workspace, "/work/project");
    }

    #[test]
    fn project_root_subdirectory_and_single_file_find_the_same_environment() {
        let expected = "/work/project/.pixi/envs/default/bin/mojo-lsp-server";
        for root in [
            "/work/project",
            "/work/project/src/nested",
            "/work/project/src/main.mojo",
        ] {
            let (server, workspace) = discover_server(root, |candidate| {
                (candidate == expected).then(|| expected.to_string())
            })
            .unwrap();
            assert_eq!(server, expected);
            assert_eq!(workspace, "/work/project");
        }
    }

    #[test]
    fn different_projects_do_not_reuse_another_projects_compiler() {
        for root in ["/work/first", "/work/second", "/work/project with spaces"] {
            let expected = format!("{root}/.pixi/envs/default/bin/mojo-lsp-server");
            let (server, workspace) = discover_server(root, |candidate| match candidate {
                path if path == expected => Some(expected.clone()),
                SERVER_NAME => Some("/usr/local/bin/mojo-lsp-server".to_string()),
                _ => None,
            })
            .unwrap();
            assert_eq!(server, expected);
            assert_eq!(workspace, root);
        }
    }

    #[test]
    fn absent_project_environment_falls_back_to_path_without_changing_workspace() {
        let (server, workspace) = discover_server("/work/new-project", |candidate| {
            (candidate == SERVER_NAME).then(|| "/opt/mojo/bin/mojo-lsp-server".to_string())
        })
        .unwrap();
        assert_eq!(server, "/opt/mojo/bin/mojo-lsp-server");
        assert_eq!(workspace, "/work/new-project");
    }

    #[test]
    fn missing_installation_is_reported() {
        assert!(discover_server("/work/project", |_| None).is_err());
    }
}
