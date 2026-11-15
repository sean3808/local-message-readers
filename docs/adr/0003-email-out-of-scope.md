# Email is out of scope for this repo

The project's original handoff document listed Email as the first priority. In reality, Email already has its own repo, its own skill and its own scheduled sync mechanism, and it is working. Bringing it in again would only create a second source of truth. This repo therefore covers only the messaging platforms other than Email.

If a future reader, comparing against the old handoff document, wonders "where did Email go" — it was not missed; it was deliberately excluded.

## Consequences

- A need such as "search what the same person said across channels", if it is ever pursued, would require a higher-level concept spanning this repo and the Email project. That is outside the current scope.
- The Email project's own technical debt (for example, an environment variable that cross-project deployment depends on is unset, so it currently works only by default-path coincidence) belongs to that project and does not enter this repo's backlog.
