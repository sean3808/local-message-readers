# Message content enters the agent context unrestricted

The reason the readers exist is to let a Local Agent handle the work items and to-dos buried in conversation details. If a masking layer were added, or if only summaries or fields were returned, the agent would still have to know in advance which message to look at, and the whole purpose of the project would be lost. The decision is therefore no restriction: the original message text goes directly into the agent's context.

## Considered Options

- **Return only fields or summaries, with a separate mode for sensitive conversations** — rejected, because in most use cases the value lies in the details of the original text.
- **Search and list titles only by default; reading the original text requires naming a conversation** — rejected for the same reason, and because it undermines the original goal of avoiding manual copy and paste.

## Consequences

- Message content enters the model context and lands in the local session transcript.
- This includes what **other people** said in the conversation, and they have not consented to this. It is an informed, accepted trade-off, not an overlooked detail.
- The corresponding safeguards live elsewhere: the repo was private at the time of this decision; reader exports, caches and decrypted artifacts are always gitignored (see the related conventions in ADR-0003 and `.gitignore`).
