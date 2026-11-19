---
name: message-reader
description: Query local WeChat or Teams chat history on this computer: list conversations, read one conversation, search by keyword, and fetch new messages since a given time.
---

Read-only; there is no send function. Query the channel the user names; if none is named, query both and label the reply by channel. The two readers do not share one output schema; their differences are described in the channel sections below.

`<repo>` is the folder where the user cloned the local-message-readers repository. From any directory, call the readers with `uv run --project <repo>`. The query subcommands are `conversations | messages | search | recent`; parameters are defined by `<subcommand> --help`:

```text
uv run --project <repo> wechat-reader <subcommand> ...
uv run --project <repo> teams-reader <subcommand> ...
```

If the location of `<repo>` is not known, ask the user once. Do not guess a path.

Date parameter format: `--start 2026-01-01`, `--since "2026-01-01 09:00:00"` (machine-local time), or an ISO 8601 time with an offset; for `recent`, pass back the last message's `time` unchanged. For installation and the WeChat key see `<repo>/AGENTS.md > Readers`.

## Steps

1. **Freshness**: confirm how far the data reaches using the "Freshness" part of the channel section below. Done when: every conclusion can state up to when the data reaches.
2. **Locate the conversation**: if the name is uncertain, run `conversations` first (Teams returns only 20 by default), then pass the name to `messages <conversation>` or `search <keyword> --conversation <conversation>`. WeChat accepts a display name or a remark name; Teams accepts a display name or an `id`. `recent` has no conversation parameter and returns the new messages of all conversations at once.
   - When several non-group Teams conversations share a name with distinct `last_time` values, the reader automatically picks the newest one and lists the others in `alternatives` (`[{id, last_time}]`, newest first; also present on a `download` error). Whenever `alternatives` is present, say in the reply that several conversations share this name, that the reader used the newest one, and list the older ones from `alternatives` (`id`, `last_time`). If the user wanted an older conversation, or a `download` error says the message was not found (it may be in an older one), rerun with that `id`. When any candidate is a group or two candidates tie on `last_time`, the reader returns an `error` listing the candidates' `id`, `last_time` and `is_group`: hand the candidates to the user to choose, or decide from `last_time` and the time the user described and state the reason, then rerun with the `id`. A name that is not found is also an `error`, not "no messages".
3. **Write output to a file**: redirect results into `<repo>/fixtures/local/` (excluded by that repo's `.gitignore`), then read back only the fields the answer needs. When called from another project, still write into `<repo>/fixtures/local/`, never into the calling project. Done when: conversation content appears only in this folder and in the reply to the user.
4. **Report and completeness**: attach the time and the speaker to every conclusion (`sender`; `is_self` marks the user's own messages; in groups identify members by `sender`).
   - A non-empty `failures` means some data could not be read and the result is incomplete: tell the user which items failed.
   - `alternatives` and `refresh.reason`, when present, are always part of the report (see step 2 and the channel's Freshness).
   - The count is bounded by `--limit`: for `recent`, `truncated` being true means there is more; `messages` and `search` have no such field, so treat a count equal to `--limit` as possibly having more. Raise `--limit` and read again, or for `messages` / `search` split by `--start` / `--end` (`recent` has neither; Teams `messages` also has `--offset` for paging).
   - Done when: `failures` is empty and the count is below `--limit` (for `recent`, `truncated` is false).
5. **Watermark**: only after `recent` meets the completion condition of the previous step, end the reply with the time of the last message read, in the format `YYYY-MM-DD HH:MM:SS` (convert the ISO `time` that carries a time zone). The reader does not remember a read position; next time the user passes it into `recent --since`. `--since` is inclusive, so next time de-duplicate by `(conversation_id, id)` and do not report already-reported boundary messages again. The time range of `messages` / `search` covers only part of the messages and is not used as a watermark.

## WeChat

**Freshness**: the reader only reads data WeChat has already written on this computer. When the user wants "latest / today / after some time", first ask whether the desktop app was logged in during that period and synced with the phone; for old history, just proceed.

**Content the reader does not provide** (report "not provided by the reader"): some call states (an unanswered call appears only as a bare call tag), and image content (only a `local_id`). A quoted reply has its `type` labelled as link/file, but the body text and the quote line that starts with `↳` themselves are correct.

## Teams

**How it works**: the reader maintains a cumulative archive (`%LOCALAPPDATA%\local-message-readers\archive.db`). Every query first merges the local Teams cache into the archive and then answers from the archive; once Teams purges its cache, the messages in the archive remain. Teams does not need to be running, and querying does not change the unread state. Opening a conversation in Teams marks it as read, so always use the reader to look at messages. Output is JSON; an error is `{"error": "..."}` with a non-zero exit code.

**Freshness**, check on every query:

- `refresh.status`: `ok` = just synced; `skipped` = `--no-refresh` was used or the last successful refresh was within `refresh_min_interval_seconds` (default 120s), with the reason in `refresh.reason` (both `--no-refresh` and `--refresh` are allowed before or after the subcommand; `--refresh` forces a refresh, and passing both together is a usage error); `failed` = the update failed, the reason is in `refresh.error`. Always state `refresh.current_up_to`. Read `skipped` by its reason: a throttle skip means the last sync was less than `refresh_min_interval_seconds` ago, so the data is not stale, though messages sent after `current_up_to` are not in it yet; when the user asks about a time later than `current_up_to`, rerun with `--refresh`. For a `--no-refresh` skip and for `failed`, report "data only reaches `refresh.current_up_to`".
- `refresh.current_up_to` is `null` = the cache was never synced successfully, the reason is in `refresh.current_up_to_reason`; report "the data cut-off time is unknown" and do not substitute another time.
- `backup.stale` is true (no successful backup for more than 8 days, or never): tell the user "the backup is stale" and attach `backup.last_success`.
- If a conversation's `last_seen_in_cache` in `conversations` is far earlier than `current_up_to`, this conversation was not covered by recent cache updates and its latest messages may be incomplete; say so in the report.

**Reading messages**:

- For forwards and quotes look at `quoted`, which holds the original author and original time; do not attribute the original author's words to the user. A `hit_in_quote` of true in `search` means the keyword appears only inside the quoted content.
- `edited` true = the text is the current edited version (the pre-edit content is not kept); a message with `deleted` true is not cited as a normal message.
- Attachments: `files` holds only file names; `attachments` holds `name` and `url` (may be null). Download with `uv run --project <repo> teams-reader download <message-id> --to <folder>`; add `--name <file name>` to filter. The first-sign-in instructions are shown on stderr. Check each attachment's status in the JSON; only when the download returns an error, open `attachments[].url` in the Chrome the user is signed in to. An image appears in `text` only as a short placeholder.

**Coverage**: the Teams cache keeps only a recent window per conversation, and older messages enter the archive only after the reader has read them; messages purged during a long period when the reader did not run are missed, and the remedy is importing the official export (see the maintenance commands below). Only the personal account is read; messages of a company work account cannot be read.

## Maintenance and upgrades

Importing the official export, reconciliation, and checks after a WeChat upgrade: read `references/maintenance.md`. Ordinary queries do not need it.
