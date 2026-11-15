# One message-reader skill that routes by channel

Other projects need a single entry point for querying WeChat and Teams. The decision is to make `skills/message-reader/` the only skill, calling the existing `wechat-reader` or `teams-reader` CLI by channel; when no channel is specified, query both. The two per-channel skills originally in this repo are removed.

## Considered Options

- **Keep one skill per channel** — rejected. Agents in other projects would have to choose an entry point, and the shared query and reporting rules would easily diverge.
- **Unify the JSON formats of the two readers** — rejected. The two channels differ in data source, freshness and limitations; this change does not modify the readers, and the skill simply describes each one's output separately.

## Consequences

- The common flow is maintained once, channel differences are explained in the same skill, and the CLI and JSON interfaces are unchanged.
- ADR-0005's approach (source in `skills/`, a `.claude/skills/` junction pointing to it) is unchanged; the skill name and the SSOT copy now follow this decision: the owner's personal settings-backup repository holds the SSOT copy at `skills/shared/message-reader/`, registered in `manifest.json` (`shared/message-reader`), deployed by junction to `~/.claude/skills/` and `~/.agents/skills/`, with `skillOverrides` set to `name-only`; the old `shared/wechat-reader` has been removed. This copy does not update automatically; after `skills/message-reader/` changes in this repo, it must be copied to the SSOT by hand.
- Always edit `skills/message-reader/`, never the content under the junction. To recreate the junction in a fresh clone: `New-Item -ItemType Junction -Path .claude\skills\message-reader -Target skills\message-reader`. These current rules replace the corresponding instructions in ADR-0005 that use the old name.
- Commands and output paths stay as absolute paths so the same skill can be used from other projects. This absolute-path rule is superseded by ADR-0009: the skill now uses repo-relative paths or the `<repo>` placeholder (the folder where the user cloned this repository) instead of absolute paths.
