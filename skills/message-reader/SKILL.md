---
name: message-reader
description: Query local WeChat or Teams chat history on this computer: list conversations, read one conversation, search by keyword, and fetch new messages since a given time.
---

Read-only; there is no send function. Query the channel the user names; if none is named, query both and label the reply by channel. The two readers do not share one output schema; their differences are described in the channel sections below.

`<repo>` is the folder where the user cloned the local-message-readers repository. From any directory, call the readers by their installed names. The query subcommands are `conversations | messages | search | recent`; parameters are defined by `<subcommand> --help`:

```text
wechat-reader <subcommand> ...
teams-reader <subcommand> ...
```

When the shell reports the name as not found, run `uv run --project <repo> <reader> <subcommand> ...` instead and tell the user the installed readers are missing (`<repo>/AGENTS.md > Readers`).

If the location of `<repo>` is not known, ask the user once. Do not guess a path.

Date parameter format: `--start 2026-01-01`, `--since "2026-01-01 09:00:00"` (machine-local time), or an ISO 8601 time with an offset; for `recent`, pass back the last message's `time` unchanged. For installation and the WeChat key see `<repo>/AGENTS.md > Readers`.

## Steps

1. **Freshness**: confirm how far the data reaches using the "Freshness" part of the channel section below. For multi-step Teams investigations, refresh once with `--refresh` on the first query, then pass `--no-refresh` for subsequent `search` / `messages` / `download` calls in the same investigation while citing that single `refresh.current_up_to`. Done when: every conclusion can state up to when the data reaches.
2. **Locate the conversation**: if the name is uncertain, run `conversations` first (Teams returns only 20 by default), then pass the name to `messages <conversation>` or `search <keyword> [keyword ...] --conversation <conversation>`. Pass multiple keywords (OR semantics) in a single `search` call instead of running one `search` per synonym; each hit already carries `conversation_name`, `is_group`, and `matched_keywords`. WeChat accepts a display name or a remark name; Teams accepts a display name or an `id`. `recent` has no conversation parameter and returns the new messages of all conversations at once.
   - Teams may resolve a shared name by itself or return an ambiguity `error`: follow "Same-name conversations" in the Teams section. A name that is not found is also an `error`, not "no messages".
3. **Write output to a file**: redirect results into `<repo>/fixtures/local/` (excluded by that repo's `.gitignore`), then read back the fields the answer needs plus every top-level status field: `refresh`, `backup`, `failures`, `truncated`, `conversation` and `alternatives` whenever they are present. When called from another project, still write into `<repo>/fixtures/local/`, never into the calling project. Done when: conversation content appears only in this folder and in the reply to the user.
4. **Report and completeness**: attach the time and the speaker to every conclusion (`sender`; `is_self` marks the user's own messages; in groups identify members by `sender`).
   - A non-empty `failures` means some data could not be read and the result is incomplete: tell the user which items failed.
   - Teams `alternatives`, and a refresh that was skipped or failed, are always part of the report (see "Same-name conversations" and "Freshness").
   - The count is bounded by `--limit`: `recent`, `messages`, and `search` all report `truncated` (`true` means older matching rows were cut by `--limit`; `--limit` keeps the newest `N` results, returned oldest to newest). Raise `--limit` and read again, or for `messages` / `search` split by `--start` / `--end` (`recent` has neither; `messages` also has `--offset` for paging). `recent` also always reports `excluded_messages` (count of messages in the `--since`..now range from conversations listed in `wechat_exclude_conversations` / `teams_exclude_conversations` in `settings.toml`, excluded before `--limit`; `0` when unset or empty), so an empty `messages` with `excluded_messages > 0` means activity was filtered rather than absent.
   - Done when: `failures` is empty and `truncated` is false.
5. **Watermark**: for Teams, run `recent` with `--refresh`, so a throttle skip never leaves unmerged cache messages behind the watermark. Only after `recent` meets the completion condition of the previous step, end the reply with the time of the last message read, in the format `YYYY-MM-DD HH:MM:SS` (convert the ISO `time` that carries a time zone). The reader does not remember a read position; next time the user passes it into `recent --since`. `--since` is inclusive, so next time de-duplicate by `(conversation_id, id)` and do not report already-reported boundary messages again. For `search` and `messages` tasks, no watermark applies; omit it and say so when relevant.

## WeChat

**Freshness**: the reader only reads data WeChat has already written on this computer. When the user wants "latest / today / after some time", first ask whether the desktop app was logged in during that period and synced with the phone; for old history, just proceed.

**Media and attachments**: export locally cached files, videos/thumbnails, and plaintext images with `wechat-reader download <message-id> --conversation <conversation> --to <folder>`. Check each item's `status` and `reason` in the JSON: encrypted WeChat 4.x `.dat` images report `encrypted_unsupported` (no bytes are written to `<folder>`), and missing or ambiguous cache files report `not_found` or `ambiguous`.

**Content the reader does not provide** (report "not provided by the reader"): some call states (an unanswered call appears only as a bare call tag), and encrypted WeChat 4.x `.dat` image contents (`download` reports `encrypted_unsupported`). A quoted reply has its `type` labelled as link/file, but the body text and the quote line that starts with `↳` themselves are correct.

## Teams

**How it works**: the reader maintains a cumulative archive (`%LOCALAPPDATA%\local-message-readers\archive.db`). Unless the refresh is skipped (see Freshness), every query first merges the local Teams cache into the archive and then answers from the archive; once Teams purges its cache, the messages in the archive remain. Teams does not need to be running, and querying does not change the unread state. Opening a conversation in Teams marks it as read, so always use the reader to look at messages. Output is JSON; an error is `{"error": "..."}` with a non-zero exit code.

**Freshness**, check on every query:

- `refresh.current_up_to` is the time of the last successful cache sync (not the time of the last message). Always state it in the reply (when it is `null`, follow the `null` rule below).
- `refresh.status`, with `refresh.reason` on every skip:
  - `ok`: synced just now.
  - `skipped` and you passed `--no-refresh`: report "data only reaches `refresh.current_up_to`".
  - `skipped` and you did not pass `--no-refresh`: the throttle skipped it, because the last clean refresh (it succeeded and reported no `failures`) of the same Teams profile is younger than `refresh_min_interval_seconds` (120 when the key is not set). The Teams cache has not been merged since `current_up_to`, so messages sent after it may be missing (an imported official export can still cover part of that time; judge coverage from the messages returned); when the user asks about a time later than `current_up_to`, or wants the very latest messages, rerun with `--refresh`.
  - `failed`: the reason is in `refresh.error`; report "data only reaches `refresh.current_up_to`".
  - `--refresh` forces a refresh; `--refresh` and `--no-refresh` go before or after the subcommand, and passing both is a usage error.
- `refresh.current_up_to` is `null` = the cache was never synced successfully, the reason is in `refresh.current_up_to_reason`; report "the data cut-off time is unknown" and do not substitute another time.
- `backup.stale` is true (no successful backup for more than 8 days, or never): tell the user "the backup is stale" and attach `backup.last_success`.
- If a conversation's `last_seen_in_cache` in `conversations` is far earlier than `current_up_to`, this conversation was not covered by recent cache updates and its latest messages may be incomplete; say so in the report.

**Same-name conversations**:

- When every conversation matching a name is a 1:1 chat with exactly that current name and their `last_time` values all differ, the reader uses the newest one. `messages` output names it in `conversation.id`, and the top-level `alternatives` lists the others (`[{id, last_time}]`, newest first). A `last_time` that is missing, blank, unparseable or has no time-zone offset counts as unknown: it sorts oldest, and two unknowns are a tie.
- Whenever `alternatives` is present (also on a `download` error), say in the reply that several conversations share this name, that the newest one was used, and list the older ones (`id`, `last_time`). If the user wanted an older one, or a `download` error says the message was not found (it may be in an older one), rerun with its `id`: `messages <id>`, `search <keyword> --conversation <id>`, or `download <message-id> --conversation <id>`.
- Any group among the candidates, a candidate matched only through an old name, or a tie returns an `error` listing each candidate's `id`, `last_time` and `is_group`: hand the candidates to the user to choose, or decide from `last_time` and the time the user described and state the reason, then rerun with the `id`.

**Reading messages**:

- For forwards and quotes look at `quoted`, which holds the original author and original time; do not attribute the original author's words to the user. A `hit_in_quote` of true in `search` means the keyword appears only inside the quoted content.
- `edited` true = the text is the current edited version (the pre-edit content is not kept); a message with `deleted` true is not cited as a normal message.
- Attachments: `files` holds only file names; `attachments` holds `name` and `url` (may be null). Download with `teams-reader download <message-id> --to <folder>`; add `--name <file name>` to filter. The first-sign-in instructions are shown on stderr. Check each attachment's status in the JSON; only when the download returns an error, open `attachments[].url` in the Chrome the user is signed in to. An image appears in `text` only as a short placeholder.

**Coverage**: the Teams cache keeps only a recent window per conversation, and older messages enter the archive only after the reader has read them; messages purged during a long period when the reader did not run are missed, and the remedy is importing the official export (see the maintenance commands below). Only the personal account is read; messages of a company work account cannot be read.

## Maintenance and upgrades

Importing the official export, reconciliation, and checks after a WeChat upgrade: read `references/maintenance.md`. Ordinary queries do not need it.

## Feedback

If a query flow feels awkward, slow, ambiguous, or missing a capability, open an issue (or offer to open one) at `https://github.com/sean3808/local-message-readers/issues` describing the friction and suggested improvement.
Follow ADR-0009: use only fictional or anonymized examples — never include real conversations, names, account IDs, or machine paths.
