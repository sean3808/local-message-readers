# Readers run as an installed snapshot; settings live in the user data folder

Other projects call the readers by their bare command (`teams-reader`, `wechat-reader`). These commands are installed as a non-editable uv tool, so each install is a snapshot of the repo at that moment. The settings file moves from the repo root to `%LOCALAPPDATA%\local-message-readers\settings.toml`, next to the archive. We chose this because a Teams refresh writes into the archive, which cannot be rebuilt. An editable install would let any half-finished change in the working tree run against the archive the next time any project queries Teams. With a snapshot, a change reaches other projects only after it is reinstalled on purpose.

The settings file has to move because it used to be found relative to the package location. Inside an installed snapshot that location is the tool environment, so the file would be missing, and since a missing default file means "nothing configured", the readers would run without the user's settings and report no error. Environment variables stay off the table (they are reserved for system locations), so a fixed per-user path is the only default that works from both the snapshot and the checkout. `--settings <file>` still overrides it.

## Considered Options

- **Editable install** — rejected. It is one command and needs no design change, but the working tree becomes the live code for every caller, including the code that writes the archive.
- **Point at the settings file with an environment variable**, the way the mail CLI uses `MAILATTIC_DB` — rejected. The settings design reads no environment variables except system locations.

## Consequences

- After changing reader code, run the install again with `--reinstall` before other projects see the change. Inside the repo, `uv run` still runs the working tree for development and tests.
- The repo-root `settings.toml` is no longer read. Move an existing copy to the new path.
