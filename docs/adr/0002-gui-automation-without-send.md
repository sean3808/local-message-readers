# GUI automation may ship, but the V1 deliverable contains no send path

After investigating three channels, only WeChat has a clean local database read path. LINE's message store is encrypted with no plaintext alternative, so the only feasible local approach is GUI automation, and the existing GUI tools **also have the ability to send messages**. The decision: GUI automation may become part of the formal deliverable, but any send path must be removed from the V1 deliverable rather than left in place unwired.

## Considered Options

- **Never ship GUI; use it only as a PoC** — rejected, because that would leave LINE with no feasible option.
- **Use the tool as-is, send capability included** — rejected. In an agent-driven project, a "switch that is turned off" will sooner or later be turned on; a wrong read is noise, but a wrong send means someone receives a message the user never wrote.

## Consequences

- If send capability is ever to be reconnected, that is a separate new decision and needs its own ADR; it must not happen by default because it was "left in".
- While the GUI approach runs it actually operates application windows, so the user cannot use that computer at the same time. This makes it inherently unsuitable for scheduled or background execution.
- If the upstream tool adds send functionality on its main branch (a PR to that effect is currently queued), this repo must pin a specific commit rather than track upstream main.
