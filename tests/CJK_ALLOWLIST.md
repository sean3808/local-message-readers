# CJK allowlist for tests

Code and messages are English. CJK characters may appear under `tests/` only at the locations below, where they are deliberate user data that the code must carry through unchanged.

Check: `git grep -n -I -P "[\p{Han}]" -- tests` should list only these lines (plus this file).

| File | Location | Reason |
|---|---|---|
| `tests/fixtures/teams/raw_messages.json` | line 35 (`alt` text of an image in a message) | A real-shaped Teams cache sample: the CJK alt text must be replaced by the `[image]` placeholder during HTML normalization |
| `tests/test_teams_export.py` | lines 157 and 164 (group-rename system event and its expected text) | A CJK group name is user data and must pass through the system-event formatter unchanged |
| `tests/test_teams_export.py` | lines 210, 215 and 219 (conversation display name and topic, and the expected name) | A CJK conversation name is user data and must be normalized and kept as-is |
| `tests/test_teams_system_text_migration.py` | lines 11-17 (old-to-new mapping table), 27 and 29 (rows that must not be rewritten), 81, 83, 97, 108 and 118 (expected unchanged texts) | The old Chinese system-event texts are archive data the migration must recognize exactly, and look-alike rows must survive unchanged; the production mapping in `readers/teams/system_text.py` writes the same texts as `\u` escapes |

Non-ASCII test inputs that are not about CJK pass-through (for example a non-ASCII password, or a common search keyword in the WeChat smoke test) are written as `\u` escapes instead.
