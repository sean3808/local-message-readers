# local-message-readers

Lets a Local Agent read content from chat applications that would otherwise have to be copied and pasted by hand. Covers channels other than email (WeChat, LINE, Teams); email has its own project, see ADR-0003.

Read-only, local-first, existing tools first. Vocabulary is defined in `GLOSSARY.md`, settled decisions are in `docs/adr/`, and open work is in GitHub Issues.

The three READMEs (`README.md`, `README.zh-TW.md`, `README.zh-CN.md`) are updated together: same sections in the same order, so a change to one is made in all three.

## Public home

This repository is the public home of the project (ADR-0009). A bare `#NNN` in the docs that does not link to an issue refers to a ticket from the private era and cannot be resolved here. Issues, PRs and commits carry only fictional data: never put real conversations, names, account IDs or machine paths in them.

## Readers

In the commands below, `<repo>` is the folder where you cloned this repository. Run them from `<repo>`, or from any other project with `uv run --project <repo> <reader> ...` (for example `uv run --project <repo> teams-reader --no-refresh conversations`). `uv run --directory <repo>` also works; the settings file is found through the package location either way.

- **WeChat**: `uv run wechat-reader conversations | messages <conversation> | search <keyword> | recent --since <time>` (`--help` lists the parameters). The reader does not remember a read position; the caller keeps the `--since` value for `recent`; pass back the last message's `time` unchanged (time inputs follow ADR-0010). Official accounts (ids starting with `gh_`, and the folded subscription/service placeholders) are excluded from `conversations`, `search` and `recent`. After a WeChat upgrade, set `wechat = true` under `[smoke]` in `settings.toml`, then run `uv run pytest tests/test_wechat_smoke.py`.
  - Data freshness: the reader only reads data WeChat has already written on this computer. Messages from periods when the desktop app was off or logged out are not backfilled automatically; first sync chat history between the phone and the desktop app. The reader cannot tell whether the data is complete; before reading `recent`, compare against the time of the latest message on the phone.
  - Install: `uv sync`. The upstream is pinned to the commit of PR #22, see `[tool.uv.sources]` in `pyproject.toml`.
  - Key: with WeChat open, run once (PowerShell) `uv run wechat-cli init --db-dir "$env:USERPROFILE\Documents\xwechat_files\<wxid folder>\db_storage"`.

- **Teams**: `uv run teams-reader conversations | messages <conversation> | search <keyword> | recent --since <time>` (`--help` lists the parameters; `--no-refresh` skips the cache update and queries the archive directly). The reader maintains a local cumulative archive (ADR-0006) and merges the Teams cache into it before every query; Teams does not need to be running, and the unread state is not changed. When several conversations share a name, the reader returns an error listing each `id`; select by `id` instead. The caller keeps the `--since` value for `recent`; pass back the last message's `time` unchanged (time inputs follow ADR-0010).
  - Each message's `attachments` lists the attachment `name` and `url` (may be null). Download a message's attachments with `uv run teams-reader download <message-id> --to <dir>`; add `--name <file name>` to filter and `--conversation <name or id>` to disambiguate. An attachment without a link is reported as skipped; a failure returns a non-zero exit code and a JSON error. If a file name already exists, a suffix such as ` (1)` or ` (2)` is added and nothing is overwritten.
  - On the first download, a Microsoft device-code sign-in URL and code are shown on stderr. The credential is stored in `%LOCALAPPDATA%\local-message-readers\graph-token-cache.bin`, encrypted with DPAPI and bound to the Windows user. The `Files.Read.All` permission can read the entire personal OneDrive; delete the cache file to remove the local credential, or remove Microsoft Graph Command Line Tools under the Microsoft account privacy page (Apps and services) to revoke the grant.
  - One-time repair of old U+FFFD decoding damage: `uv run teams-reader repair-fffd` previews; after confirming, add `--write` to apply. It is limited to the default archive and requires a non-stale backup.
  - One-time rewrite of archived Chinese system-event texts (such as the old "member added" text) to the English text introduced in #39: `uv run teams-reader migrate-system-text` previews; after confirming, add `--write` to apply. It touches only rows of type `system` whose text is exactly an old description, keeps versions unchanged, is idempotent, and has the same default-archive and non-stale-backup guards as `repair-fffd`.
  - Data freshness: check `refresh.status` and `refresh.current_up_to` in the output (`null` when the cache was never synced successfully; the reason is in `refresh.current_up_to_reason`). `backup.stale` being true means no successful backup for more than 8 days, and the user must be told. History outside the cache window comes from importing the official export (`uv run teams-reader import-export <export file>`; an encrypted backup must exist before importing into the real archive).
  - Install: `uv sync`; no key is needed.
  - Own identity: the cache prefers `isSentByCurrentUser` and the export prefers `userId`. When the data lacks both, set `teams_self_id` in `settings.toml` to your Teams user ID (for example `8:example`); if the sender still cannot be determined, `is_self` is false.
  - The backup destination is required: set `backup_dest` in `settings.toml`, or pass `--dest` (which wins over the settings file). Run manually with `uv run python -m readers.backup --dest <existing folder>`; when registering the schedule, pass `-Destination <existing folder>`.
  - Backup: the weekly encrypted backup contains the raw Teams and WeChat data; always check `wechat.ok` in the result. For backup and restore see `docs/teams-archive-backup.md`.

## Settings file

Personal settings (backup destination, own Teams identity, location of the commit blocklist, archive and profile paths, targets of the real-data smoke tests) all live in `settings.toml` in the repo root; environment variables are not used. Copy `settings.example.toml` to `settings.toml` and fill in your own values. `settings.toml` is ignored by git; the example is tracked and explains every key. The program finds the repo root through the package location, so calling it from another project with `uv run --directory <repo>` also reads it.

- Every entry point (`teams-reader`, `wechat-reader`, `python -m readers.backup`, `scripts/check_commit_blocklist.py`) accepts `--settings <file>` to read another settings file; no `settings.toml` means no value is set.
- Command-line parameters (`--archive-path`, `--profile-dir`, `--dest`) take precedence over the settings file.
- A misspelled key, a wrong type or a malformed file makes the command return an error naming the file and key; nothing is silently ignored.
- Tests always pass `--settings <tmp file>` (see `settings_args` / `use_settings` in `tests/conftest.py`), so they never read the real `settings.toml` or archive. The real-data smoke tests read only the `[smoke]` table and skip when it is absent.
- The only environment variables still read are system locations: `LOCALAPPDATA`, `USERPROFILE`, `ProgramFiles`.

## Agent skills

### Issue tracker

Issues and specs live in this repo's GitHub Issues, operated with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five standard labels from mattpocock/skills are used, with their names unchanged. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the root plus `docs/adr/`. See `docs/agents/domain.md`.

## Commit blocklist

Install the hook with `uv tool run pre-commit install`. Set `commit_blocklist` in `settings.toml` to the private UTF-8 blocklist path before committing; missing or unreadable configuration blocks the commit. To scan the entire committed tree, run `uv run python scripts/check_commit_blocklist.py --head`. Hits are reported as `<path>:<line>`; a path that itself matches is shown as `<file #N, name withheld>`, where N is its position in `git diff --cached --name-only` (or `git ls-tree -r --full-tree HEAD` with `--head`), so the output never contains a blocked string.

If a blocklist entry is a substring of a string that is public (for example the owner's GitHub handle), list that string under `commit_blocklist_public` in `settings.toml`: each case-insensitive occurrence is masked in file content and paths before matching, so the entry still matches everywhere else.

Verify the executable with `uv tool run pre-commit --version` before installation. Blocklist lines beginning with `# ` are comments; a leading backslash escapes an entry (including one beginning with `# ` or a backslash). Other non-empty lines are literal entries.
