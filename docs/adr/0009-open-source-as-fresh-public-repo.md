# Open-source as a fresh public repo, usable by colleagues

The owner and colleagues need these readers, and no suitable existing tool was found on GitHub, so the project is given back as open source (#36). Unlike mailattic (showcase only, "sample runs"), the bar here is a usable tool: a colleague on their own Windows PC can install it from the README alone and read their own WeChat and Teams.

We follow the procedure the owner used when open-sourcing mailattic:

- **Carrier**: a fresh public repo whose only starting commit is built with `git commit-tree <cleaned tree>`; both `^{tree}` must match. No history, no issues carried over. The public repo becomes the home; this repo is frozen afterwards. The name stays `local-message-readers`; because one account cannot hold two repos with the same name, this private repo is renamed (e.g. `local-message-readers-private`) right before the public one is created. That rename is part of the authorized publication step.
- **License**: MIT, the same as mailattic. It is permissive, compatible with every dependency (wechat-cli Apache-2.0, ccl_* MIT, ijson BSD), and imposes nothing on colleagues who adapt it internally.
- **Language**: everything in English (code messages, docs, ADRs, glossary, agent docs, skill). README in three languages: `README.md` (English, primary), `README.zh-TW.md`, `README.zh-CN.md`, section for section.
- **No hardcoded locations or identities**: no developer-machine absolute paths, user names, OneDrive/employer paths or the owner's account id in code, skill, docs or scripts. The user's own identity is derived from data or configured.
- **Internal notes stay local**: `docs/current-status.md`, `.claude/promote-candidates*.md`, `session-continuity.md` and similar are git-ignored and keep their place, so the private and public trees are the same tree.
- **Leak prevention**: an out-of-repo blocklist plus a pre-commit hook that fails closed and reports locations only. De-identification is judged by "a person in the trade cannot tell who it is", not by zero machine hits; test data is invented from scratch.
- **Dependency safety**: wechat-cli is pinned to the owner's fork `<owner>/wechat-cli` branch `pinned/pr22-weixin-4.1` so installs survive the upstream disappearing.
- **Platform**: Windows only (WeChat and Teams desktop data locations, DPAPI, Task Scheduler); no cross-platform work.

## Considered Options

- **Flip this repo to public**: rejected. History and 35 issues contain real names, the employer name, conversation ids and share links.
- **git filter-repo**: rejected. String replacement cannot remove the shape of real chats from tests and issues, and every hash changes anyway.
- **Showcase only (as mailattic did)**: rejected by the owner (2026-11-14); colleagues must be able to use it.

## Consequences

- Creating the public repo and making it public are the only irreversible steps; each needs the owner's authorization at that moment.
- Pre-publication acceptance adds two machine scans on the public tree with zero hits: CJK characters outside the README translations and a listed fixture allowlist; and developer-machine absolute paths (drive-letter project and user-profile paths), the owner's account id (except the LICENSE copyright) and the employer name.
