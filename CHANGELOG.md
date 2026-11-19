# Changelog

Each release's notes on GitHub are this file's section for that version. `scripts/release.ps1` refuses to release a version that has no section here, so write the section before releasing.

## [0.2.0] - 2026-11-19

### Breaking changes

- **The settings file moved** from `settings.toml` in the repository folder to `%LOCALAPPDATA%\local-message-readers\settings.toml`, next to the archive. The copy in the repository folder is no longer read: move yours to the new location, or every setting falls back to unset. `--settings <file>` still overrides the location (ADR-0011, #7).

### New

- **Install once, call from anywhere**: `uv tool install <repo>` installs `teams-reader` and `wechat-reader` as commands that work from any folder. The installed copy is a snapshot; it changes only when a new version is released, so work in progress never runs against your archive from another project (ADR-0011, #4).
- **Teams: same-name 1:1 chats resolve by themselves.** When a name matches several 1:1 chats with that same name, the reader uses the most recent one and lists the others in `alternatives` (also on a `download` error), so the caller can rerun with an older chat's `id`. Group chats, chats matched only through an old name, and ties still return an error listing each `id` (#2).
- **Teams: back-to-back queries skip the cache refresh.** A query within `refresh_min_interval_seconds` (default 120, `0` turns it off) of the last clean refresh of the same Teams profile answers from the archive and says so in `refresh.status` / `refresh.reason`. A refresh that failed or reported `failures` never counts. `--refresh` forces one; the weekly backup always does (#3).

### Project

- CI also installs the commit as a tool and starts both readers. Releases are made with `scripts/release.ps1`, which waits for CI, reinstalls the global tool from the release commit, and pushes the tag; the Release workflow then publishes the release with these notes.

## [0.1.0] - 2026-11-16

First public release. Read-only, local-first readers that let a local agent read WeChat and Teams chats on Windows, with JSON output.

### Readers

- **WeChat** (`wechat-reader`): `conversations`, `messages`, `search`, `recent`. Official accounts (subscription and service accounts) are left out of listings, search and `recent`.
- **Teams** (`teams-reader`): the same four commands over a local cumulative archive that survives Teams cache purges, plus attachment download through Microsoft Graph, import of the official Teams export, and an encrypted weekly backup.

### Time values

Every message carries `time` (local ISO 8601 with its offset) and `timestamp` (epoch seconds). Time inputs in both readers accept ISO 8601 with an offset or `Z` as well as `YYYY-MM-DD [HH:MM[:SS]]` in machine-local time, so the `time` of the last message read can be passed back to `recent --since` unchanged (ADR-0010, #1).

### Project

Issue and pull request templates, a security policy with private vulnerability reporting, Dependabot updates, and a tag-driven release workflow.
