# local-message-readers

[English](README.md) | [Traditional Chinese (zh-TW)](README.zh-TW.md) | [Simplified Chinese (zh-CN)](README.zh-CN.md)

local-message-readers lets an AI agent on your own Windows PC read your WeChat and Microsoft Teams chats directly, instead of you copying and pasting messages into the conversation. It has two command-line tools, `wechat-reader` and `teams-reader`, that print conversations as JSON, plus a ready-made agent skill (`message-reader`) that tells an agent how to use them.

## What it is and why

Work chatter often lives in WeChat and Teams, and the only way to hand it to an agent used to be to copy it out by hand. These readers open the data that the desktop apps already keep on your computer and answer four questions: which conversations exist, what is in one conversation, which messages contain a keyword, and what is new since a given time.

Teams keeps only a rolling window of recent messages in its cache. So `teams-reader` also builds a local archive: every query first merges the Teams cache into a SQLite file, and messages stay there after Teams purges them. Older history can be added by importing the official Teams export.

## Limits

- **Windows only.** The data locations, the credential encryption (DPAPI) and the weekly backup (Task Scheduler) are all Windows features.
- **Read only.** There is no function that sends, edits or deletes a message.
- **Teams personal accounts only.** Messages of a company or school work account cannot be read.
- **Only what is already on this computer.** If the desktop app was closed or logged out for a while, the messages from that time are not fetched for you. Sync the chat history between your phone and the desktop app first. The readers cannot tell whether the data is complete, so compare the newest message with your phone when it matters.
- **Not supported:** some WeChat call states and the content of images (only an identifier is returned).

## Install (for non-developers)

You need a Windows 11 PC, WeChat and/or the Teams desktop app signed in at least once, and an internet connection for the install (the dependencies are downloaded from GitHub). You do not need to install Python: uv installs the right version by itself.

All commands below are typed into PowerShell (open the Start menu, type `PowerShell`, press Enter).

### 1. Install uv and get the code

1. Install [uv](https://docs.astral.sh/uv/), the tool that sets up Python and the dependencies:

   ```powershell
   winget install --id=astral-sh.uv -e
   ```

   Close and reopen PowerShell afterwards so that `uv` is found.
2. Get the code. Either install [Git](https://git-scm.com/) and run `git clone <repository URL>` with the address shown on this page's green **Code** button, or choose **Code > Download ZIP** on the repository page and unzip it. The folder you end up with is called `<repo>` below.
3. Install the dependencies:

   ```powershell
   cd <repo>
   uv sync
   ```

4. Check that it works: `uv run wechat-reader --help` and `uv run teams-reader --help` should each print a usage text.

Optional: copy `settings.example.toml` to `settings.toml` in `<repo>` and fill in only the values you need. It is ignored by git, every key is optional, and the file explains each key. A misspelled key stops the command with an error that names the key. Every command also accepts `--settings <file>` to read another file.

### 2. WeChat: get the key

WeChat stores its chat database encrypted. Once, with WeChat desktop open and logged in, extract the key:

```powershell
cd <repo>
uv run wechat-cli init --db-dir "$env:USERPROFILE\Documents\xwechat_files\<your account folder>\db_storage"
```

`<your account folder>` is the folder under `xwechat_files` that contains `db_storage`. The key is saved outside the repository, at `%USERPROFILE%\.wechat-cli\all_keys.json`. After a WeChat upgrade that changes its database, run this step again. The pinned version of the underlying tool is tested with WeChat 4.1.x.

Then try it:

```powershell
uv run wechat-reader conversations
uv run wechat-reader recent --since "2026-01-01 09:00:00"
```

The reader does not remember what it has already shown you; whoever calls `recent` keeps the `--since` time.

### 3. Teams: first sync

No key and no sign-in are needed, and Teams does not have to be running. The first query copies what is in the Teams cache into the local archive (default `%LOCALAPPDATA%\local-message-readers\archive.db`):

```powershell
cd <repo>
uv run teams-reader conversations
```

Look at `refresh.status` and `refresh.current_up_to` in the JSON: they say whether the sync worked and how far the data reaches. If you have the official Teams export for older history, add it with `uv run teams-reader import-export <export file>` (take a backup first, see step 5). Querying never changes the unread state in Teams. If you also open a conversation in Teams itself, Teams marks it as read.

If the profile folder is not in the standard place, set `profile_dir` in `settings.toml` or pass `--profile-dir`.

### 4. Teams: download attachments (first sign-in)

Message JSON lists each attachment's `name` and `url`. To download files, run:

```powershell
uv run teams-reader download <message-id> --to <target folder>
```

Add `--name <file name>` to pick one file. Existing files are never overwritten; a suffix such as ` (1)` is added instead.

The first download asks you to sign in with a **device code**: PowerShell shows a Microsoft web address and a short code. Open the address in a browser, enter the code and sign in with your personal Microsoft account. Details:

- The sign-in uses Microsoft Graph Command Line Tools and the `Files.Read.All` permission. That permission can read your entire personal OneDrive, which is why attachments can be fetched.
- The credential is stored in `%LOCALAPPDATA%\local-message-readers\graph-token-cache.bin`, encrypted with DPAPI and tied to your Windows user.
- **To remove it from this computer**, delete that file. **To revoke the grant itself**, open your Microsoft account's privacy page, go to Apps and services, and remove Microsoft Graph Command Line Tools.

### 5. Weekly backup

Teams purges old cache, so the archive is worth backing up. The weekly job refreshes the archive and writes an encrypted 7-Zip backup of the Teams archive and the raw WeChat data (including the key). You need to provide:

1. **7-Zip**, installed (for example `winget install 7zip.7zip`).
2. **PowerShell 7** (`pwsh`), installed (`winget install Microsoft.PowerShell`).
3. **A backup password** kept in Windows Credential Manager. Run this in Command Prompt and type the password when asked:

   ```cmd
   cmdkey /generic:local-message-readers/archive-backup /user:backup /pass
   ```

   Use only English letters, digits and keyboard symbols; no double quote `"`, no non-English characters, and do not end it with a backslash. Write the password down in your password manager: restoring on a new computer needs it.
4. **A destination folder** that already exists (for example a folder inside OneDrive). Set `backup_dest` in `settings.toml`, or pass `--dest`.
5. **The schedule**, registered once from the main project folder:

   ```powershell
   pwsh -File scripts/register-backup-task.ps1 -Destination "<existing folder>"
   ```

   It runs every Sunday at 12:00 while you are logged on, and runs after the next logon if the computer was off. The newest 8 backups are kept.

To run a backup right now: `uv run python -m readers.backup --dest "<existing folder>"`. Check `wechat.ok` in the result; when it is false, that backup holds Teams only. Restore steps, the exit codes and the known limits are in [`docs/teams-archive-backup.md`](docs/teams-archive-backup.md). Every Teams query also reports `backup.stale`; it is true when there has been no successful backup for more than 8 days.

## Using it with an agent

The skill in `skills/message-reader/` teaches an agent to query WeChat and Teams, check how fresh the data is, handle duplicate conversation names, and report where the data may be incomplete. Make the folder visible to your agent:

- **Claude Code:** create a link to it in the skills folder of the project or user profile where you want to use it:

  ```powershell
  New-Item -ItemType Junction -Path "<project or %USERPROFILE%>\.claude\skills\message-reader" -Target "<repo>\skills\message-reader"
  ```

- **Other agents:** point the agent at `skills/message-reader/SKILL.md`, or paste it into the agent's instructions.

From any other folder, the agent runs the readers as `uv run --project <repo> wechat-reader ...` and `uv run --project <repo> teams-reader ...`, so tell it where `<repo>` is. Query output is written to `<repo>/fixtures/local/`, which git ignores. `AGENTS.md` describes every command and option in full.

## Privacy

- All data stays on your computer: the readers, the archive, the key and the backups are local files. If you choose a backup destination inside a cloud-synced folder (such as OneDrive), the encrypted backup file is synced by that service.
- The only network connection the readers make is to Microsoft Graph, and only when you download a Teams attachment. (`uv sync` downloads the dependencies once at install time; that is not message data.)
- Message content is never sent anywhere by this project. What an agent does with it afterwards depends on the agent you connect.
- Do not commit chat content, keys or your `settings.toml`; the repository ignores them by default.

## Credits

- [wechat-cli](https://github.com/huohuoer/wechat-cli) (Apache-2.0) reads the WeChat database and extracts its key. This project installs it from a pinned fork so that installs keep working if the upstream disappears.
- [ccl_chrome_indexeddb](https://github.com/cclgroupltd/ccl_chrome_indexeddb) and [ccl_simplesnappy](https://github.com/cclgroupltd/ccl_simplesnappy) (MIT) by CCL Forensics read the browser-style database where Teams keeps its cache.
- [uv](https://docs.astral.sh/uv/) and [MSAL for Python](https://github.com/AzureAD/microsoft-authentication-library-for-python) handle environments and the Microsoft sign-in.

## License

[MIT](LICENSE).
