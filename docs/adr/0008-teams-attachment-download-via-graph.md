# Teams attachments are downloaded through Microsoft Graph with the user's own account

Attachments on Teams messages are OneDrive share links. Originally the agent had to open a signed-in Chrome to download them, taking a screenshot and clicking a menu for every file, which consumed many rounds of tool calls (#32). The decision: let `teams-reader download` sign in with the user's own personal Microsoft account using device code, obtain a pre-authorized download URL through Graph `/shares/{shareId}/driveItem`, and then download. This is the first feature in the project that connects outward; the other readers still read only local data.

## Considered Options

- **Anonymous direct download (`?download=1`, `api.onedrive.com/shares`)**: rejected. Non-browser clients always receive 503 `RequestQueueFull`, and the `1drv.ws` domain has also been retired (#32 spike).
- **agent-browser headless download**: rejected. `--profile` actually opens a copy of the profile, and the copy does not carry Microsoft's sign-in state; attaching to the running Chrome would require opening a brand-new profile and signing in again.
- **Register our own Entra app**: rejected. Instead use Microsoft's official public client Microsoft Graph Command Line Tools (`14d82eec-204b-4c2f-b7e8-296a70dab67e`), which supports personal accounts and device code and needs no registration.

## Consequences

- Credentials are kept as an MSAL token cache at `%LOCALAPPDATA%\local-message-readers\graph-token-cache.bin`, encrypted with DPAPI and bound to the Windows user (approved by the owner on 2026-11-14). The file contains a refresh token, whose validity extends automatically as long as it is used within 90 days.
- The permission is `Files.Read.All`, which can read the entire personal OneDrive. There is no smaller permission available for reading items shared by others through `/shares`. To revoke: delete the cache file, or remove the application in the Microsoft account's privacy settings.
- The read-only, never-send principle is unchanged (ADR-0002): only GET requests are sent to Graph, and Teams' unread state is not changed.
- What is downloaded is the original file as stored by the sharer. A version downloaded from Excel on the web is re-saved at that moment, so the two differ in metadata and bytes, while the worksheet contents are identical.
- Only personal Microsoft accounts are supported (the `consumers` endpoint), consistent with the Teams reader reading only personal Teams.
