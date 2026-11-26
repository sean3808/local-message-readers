# Coding Standards

Rules the `/code-review` Standards axis checks diffs against. Every rule comes from a defect or decision in this repo and cites its source.
Mechanical rules live in `ruff.toml` and `.pre-commit-config.yaml`; they are not repeated here.

## A. Failures and recorded status

**A1. A cache record that fails to parse is appended to `failures`; it is never skipped silently (`continue`, `except ...: pass`, returning a default). Code that returns cache content to a caller also excludes every conversation whose key failed, because the loader keeps the newest readable record per key and an older version would otherwise stand in.**
A bare skip makes a partial read look complete, and the older version reads as if it were current.
Source: `readers/teams/snapshot.py` > `_load_latest_store_records` (`failures`, `failed_keys`, `UNKNOWN_KEY`) and `read_cache_messages` (tainted conversations dropped)

**A2. Status that a later run trusts (the refresh throttle token) is written pessimistically: drop it before the work starts and set it only after the work finished cleanly (success and no `failures`). A path that skips the work (throttle, `--no-refresh`) never sets it, and a caller that needs fresh data passes `--refresh`.**
Otherwise an exception mid-refresh, or a throttled skip, leaves a token that makes the next run skip a refresh it needs.
Source: 6b39eb0 and c049878 (fixes for review-found defects), 2839c09 (`readers/teams/cli.py` > `clear_clean_refresh` / `set_clean_refresh` around `ingest_cache`), 1de97e0 (`recent` always passes `--refresh`)

## B. Output contract

**B1. Reader stdout is a JSON contract; a new diagnostic goes to stderr only. Where code consumes a child process's output, a newly added stderr line is filtered out by a constant shared with the code that emits it, and the existing choice of which stream becomes the error detail is left unchanged.**
Reordering that choice to hide one new line changed the error detail of unrelated failures; the reorder was made and reverted on the branch.
Source: 7017c27, PR #17 (`readers/backup.py` > `run_refresh`, `REPO_SETTINGS_WARNING_PREFIX` in `readers/settings.py`)

## C. Tests

**C1. A test that calls an entry point in process (`main([...])`) isolates every default that resolves to real data before the call: the settings file (handled by the autouse fixture in `tests/conftest.py`), and also the archive path, the Teams profile folder, the WeChat data read through `readers.wechat`, and the refresh child process started by `readers.backup`. Each is passed as an argument or patched.**
With no settings file the defaults still point at the real archive and real chat data; settings isolation alone does not cover them.
Source: 7017c27, PR #17 (`tests/test_settings.py` > the repository-settings warning test over all entry points; review round that added the WeChat and refresh-child isolation)

**C2. A test whose name or docstring claims a state (edited, deleted, quoted, skipped) asserts that its fixture actually reached that state, not only the final outcome.**
A fixture set `skypeeditedid` while the normalizer reads `properties.edittime`, so the "edited" case never ran; the test still passed on its outcome assertion.
Source: 7017c27, PR #17 (`tests/test_teams_placeholder_cleanup.py` > `test_ingest_cache_edited_deleted_or_quoted_confirmed_copy_never_ingests_minus_one`, the assertion that the fixture reaches the edited and deleted states)

## Maintenance

- Add a rule only from an observed defect in this repo, and cite its source.
- When a linter, type checker, or test can enforce a rule, move it there and delete it here.
- Delete a rule once the code it governs is gone or the decision behind it is reversed.
