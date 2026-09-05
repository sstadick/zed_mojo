use zed_extension_api::{self as zed, Result};

const LANGUAGE_SERVER_ID: &str = "mojo-lsp-server";
const SERVER_NAME: &str = "mojo-lsp-server";
const LSP_BRIDGE: &str = include_str!("../server/mojo_lsp.py");

struct MojoExtension;

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

        let binary = lsp_settings.binary;
        let mut args = Vec::new();
        let mut env = worktree.shell_env();
        let binary_path = binary
            .as_ref()
            .and_then(|binary| binary.path.as_deref())
            .unwrap_or(SERVER_NAME)
            .to_string();

        if let Some(binary) = binary {
            if let Some(arguments) = binary.arguments {
                args.extend(arguments);
            }

            if let Some(binary_env) = binary.env {
                env.extend(binary_env);
            }
        }

        let command = if binary_path.contains('/') {
            binary_path
        } else {
            worktree
                .which(&binary_path)
                .ok_or_else(|| format!("{binary_path} must be available in PATH"))?
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
            worktree.root_path(),
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
