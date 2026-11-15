# Do not block WeChat auto-update; run the smoke test after upgrades

The WeChat reader's key extraction relies on the upstream project's memory-scanning approach, and a WeChat release may break it. Even so, we choose to let WeChat auto-update as usual: after each upgrade, run `tests/test_wechat_smoke.py` first, and if it breaks, switch to the fallback (the key-scan fix in upstream PR #29).

WeChat parses images, files and links sent by strangers, and exploitable vulnerabilities have appeared in it year after year. Staying on an old version leaves those vulnerabilities on a computer that holds work data. Moreover, Tencent periodically disables versions that are too old, so blocking updates only delays the day of forced upgrade rather than avoiding it. On that day the smoke test is needed all the same to tell whether the reader still works.

## Considered Options

- **Turn off auto-update or block the updater with a firewall** — rejected. This trades security for reader stability, putting the cost in the wrong place; it also amounts to changing system security settings and may stop working after a WeChat release.

## Consequences

- When the smoke test goes red, fix the reader; do not go back and disable updates.
- When WeChat auto-upgraded from 4.1.13 to 4.1.15 in 2026-10, the reader did not break; so far there is no evidence that auto-update is a common source of breakage.
