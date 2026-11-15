# The skill source lives in `skills/`, with `.claude/skills/` as a junction pointing to it

> Later decision: the skill name and SSOT approach are in ADR-0007; keeping the source in `skills/` with a junction pointing to it is unchanged. The record below is kept as it was at the time.

The source of the `wechat-reader` skill lives in `skills/wechat-reader/` at the project root and is under version control. Claude Code loads skills only from `.claude/skills/`, so a junction pointing to the source is placed there; the junction is not under version control (`.gitignore` excludes `.claude/skills/`).

With the source under version control, PRs and other agents can get it; the junction lets Claude Code read the same content without maintaining two copies.

## Considered Options

- **Keep the source in `.claude/skills/`** — rejected. That is a Claude Code-specific path, PRs cannot carry it, and it is unfriendly to other agents that use the generic AGENTS.md.
- **Keep a copy in each of two places** — rejected. The content would diverge: this skill went through eight review rounds, each editing the same text, so two copies would almost certainly fall out of sync.

## Consequences

- A fresh clone has no junction, so Claude Code cannot see this skill; the command to recreate it: `New-Item -ItemType Junction -Path .claude\skills\wechat-reader -Target skills\wechat-reader`.
- Always edit `skills/wechat-reader/`, never the content under the junction.
- The owner's personal settings-backup repository kept a copy of the skill (`skills/shared/wechat-reader/` there). It does not update automatically, so after a change in this repo it had to be copied again by hand. That copy was registered in the SSOT `manifest.json` (`shared/wechat-reader`) and deployed by junction to `~/.claude/skills/` and `~/.agents/skills/` so that other projects could use it too; `skillOverrides` in `~/.claude/settings.json` was set to `name-only`. The commands and output paths inside the skill were all written as absolute paths, so the same text worked in this repo and in other projects.
