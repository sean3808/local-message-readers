# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the "Label in our tracker" column to match whatever vocabulary you actually use.

## Category labels

`bug` and `enhancement` are the two category roles used by triage. Every triaged issue carries exactly one category label and one state label.

## Release labels (not triage)

These labels belong to the release process, not to triage. Keep them: removing one breaks the release notes or Dependabot.

| Label             | Used by                                                      |
| ----------------- | ------------------------------------------------------------ |
| `documentation`   | `.github/release.yml`: "Documentation" section               |
| `breaking-change` | `.github/release.yml`: "Breaking changes" section            |
| `dependencies`    | `.github/release.yml` and `.github/dependabot.yml`           |
| `skip-changelog`  | `.github/release.yml`: leaves the PR out of the release notes |
