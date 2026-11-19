# message-reader maintenance commands

Ordinary queries do not need this. Call the readers as in SKILL.md (`<reader> <subcommand> ...`); `<repo>` is the folder where the repository was cloned, parameters are defined by `--help`, and all output is JSON.

## Teams: import the official export

`teams-reader import-export <official export .tar or messages.json>`: merges the official export into the archive to fill in history outside the cache window; importing twice or overlapping with the cache produces no duplicates. A directory is not accepted as the argument.

This command **writes to the archive**. Before importing into the real archive an encrypted backup must already exist (for weekly backup and restore see `<repo>/docs/teams-archive-backup.md`); with no backup, stop and ask the user.

## Teams: reconcile

`teams-reader reconcile <official export .tar or messages.json>`: read-only; compares the official export with the cache-derived version in the archive, counting only the overlapping period. `--limit` caps how many locators are printed per kind of difference (default 0 = all).

## WeChat: after a WeChat upgrade

Set `wechat = true` under `[smoke]` in `%LOCALAPPDATA%\local-message-readers\settings.toml` (if you still have a `settings.toml` in `<repo>`, move it there first), then run `uv run pytest tests/test_wechat_smoke.py` in `<repo>`. Without that setting, or when the key is absent, the test skips: a skip means not yet verified, so first re-initialize the key following `<repo>/AGENTS.md > Readers` and run again. If it goes red, fix the reader (`<repo>/docs/adr/0004-keep-wechat-auto-update.md`).
