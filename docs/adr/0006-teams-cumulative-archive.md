# The Teams reader maintains its own cumulative archive, not just the current cache

The local data of personal Teams (the new Teams' WebView2 IndexedDB) is only a rolling cache: each conversation keeps just a recent stretch, older messages are filled back only when you scroll up in the desktop app, and they may be purged again later. The full history can only be obtained from the official export (teams.live.com/dataexport), but each export takes several days to arrive and can only be taken as a whole package. The owner requires all history to be searchable. The decision: the Teams reader maintains a local cumulative archive of its own. Import the official export once, and from then on merge every message read from the cache into the archive, keeping only the latest version of the same message. A message that has entered the archive does not disappear when Teams clears its cache.

## Considered Options

- **The reader keeps no state; re-export periodically** — rejected. Each export takes several days, and messages purged between two exports can only be recovered by opening the conversation and scrolling up; opening a conversation clears its unread state, and scrolling occupies the whole computer.
- **Rely entirely on GUI auto-scrolling to backfill** — rejected. Over 300 conversations would have to be opened, destroying the unread state of every one, and it is uncertain how long the backfilled data would last.
- **Accept gaps in history** — rejected. This violates the requirement that all history be searchable; the owner used to do exports by hand precisely to look up old messages.

## Consequences

- The archive structure is shared across channels from the start (every row carries its channel), but in the first phase only Teams writes to it. When other channels are connected later, it is just more data written, with no redesign.
- The Teams reader is no longer a stateless program, unlike the WeChat reader: the WeChat local database itself holds the full history and needs no separate store.
- The archive has a second purpose: **preservation**, that is, not losing data when a phone is lost or the computer is replaced. So besides "all history is searchable", a copy must also exist off this computer:
  - The main file lives in the local user data folder and is not committed to git.
  - A weekly scheduled task encrypts the main file with 7-Zip AES-256 and backs it up to a user-chosen destination folder (set per ADR-0009's no-hardcoded-locations rule). In the original deployment this was a folder in a OneDrive belonging to an organizational account the owner controls; the encryption is meant only to deter casual access, and after a computer replacement, installing 7-Zip and entering the password is enough to restore.
  - The password is stored in Windows Credential Manager, entered by the owner, and also recorded separately elsewhere.
- Channels differ in how much preservation they need: WeChat needs it most (the cloud is unreliable and local data goes with the computer), LINE is moderate, and Teams needs it least (the cloud is trustworthy). Only Teams is done in the first phase because of business pressure; WeChat is next.
- The official export and the local cache use the same conversation ids and message ids (checked against an export file from 2025-12), so these two ids can be used directly to remove duplicates.
- The cache enters the archive only when it is read. If the reader goes unrun for too long, messages purged in the meantime are still lost and must be recovered by the next official export or by scrolling.
