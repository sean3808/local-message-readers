# Weekly Teams Refresh and Encrypted Backup of Teams and WeChat

This document describes the weekly refresh of the Teams archive database, and the 7-Zip AES-256 encrypted backup of the raw Teams and WeChat data: setup, restore procedure and status codes (corresponds to issues #17 and #30 from the private era, and ADR-0006).

In this document `<repo>` is the folder where you cloned this repository.

---

## 1. Architecture and how it works

- **Cumulative Archive**: the local Teams cache is a rolling cache, and historical messages are purged over time. This project accumulates messages in a local SQLite file (by default `%LOCALAPPDATA%\local-message-readers\archive.db`).
- **Weekly Refresh**: the CLI of `readers.teams` runs `conversations --limit 1`, which triggers an incremental sync from the cache into the archive database.
- **Consistent Copy**: a copy is made with a read-only SQLite URI (`file:...?mode=ro`) and `Connection.backup()`. This writes the WAL content completely into one standalone `archive.db`, then switches to `DELETE` journal mode and runs `PRAGMA integrity_check`.
- **WeChat raw data**: finds the single account under `%USERPROFILE%\Documents\xwechat_files` that contains `db_storage`, and collects `db_storage`, plus `msg`, `config`, `business` and `resource` when they exist, plus `%USERPROFILE%\.wechat-cli\all_keys.json`; `cache`, `temp` and `apm_record` are not included. `wechat.folders` in the result lists the folders collected this time. Every `*.db` file under `db_storage` is read with Win32 shared reads (`FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`) alongside its paired `-wal` file and merged over opaque 4096-byte encrypted pages (without decrypting) into a WAL-consistent standalone `.db` snapshot, reading the `-wal` file both before and after the main file and retrying that database pair up to 3 times if either file's `(size, mtime_ns, ino)` or the WAL contents change mid-read; `-wal` and `-shm` sidecar files are omitted from the backup. Non-database files in `db_storage` and the other folders remain best-effort copies whose sizes and modification times are compared before and after copying, retried once if they changed. If the account or `db_storage` is missing, there are several accounts, the key is missing, any database fails WAL snapshot validation or keeps changing past retries, or copying fails, only Teams is backed up.
- **Encrypted Backup**: 7-Zip packs the data with AES-256 encryption (including file-name hiding, `-mhe=on`) into the backup folder (the destination must be specified).
- **Retention (Prune)**: the newest 8 backups are kept (including the new one, which is always kept). Only files whose names exactly match `teams-archive-YYYYMMDD-HHMMSS.7z` count as backups; other files in the folder are neither counted nor deleted. Older backups are deleted only after the new backup has been written completely and passed `7z t`. An old file that cannot be deleted (for example locked by an ongoing OneDrive sync) is listed in `prune_errors` and does not affect whether this backup counts as successful.
- **Password protection**: the backup password is read from the Windows Credential Manager and is never written to disk, log files, standard output or exception messages. Limitation: 7-Zip only accepts a password from a command-line argument, so during the few seconds of encryption and verification other programs on this computer can see that argument; this is within the "keep honest people honest" protection level (ADR-0006).

---

## 2. One-time setup

The two steps below are performed by the user personally; an agent never creates the password or registers the schedule on your behalf.

### Step A: create the backup password in Windows Credential Manager

Run this in a Command Prompt (CMD):

```cmd
cmdkey /generic:local-message-readers/archive-backup /user:backup /pass
```

> **Password rules** (a backup fails immediately with an explanation if they are not met): use only English letters, digits and symbols found on the keyboard (spaces allowed); **no double quote `"`, no Chinese or other non-English characters, and it must not end with a backslash `\`**. The reason, measured with 7-Zip 26.05: a `"` makes encryption fail; with a password that contains non-English characters, or that contains a space and ends with `\`, the program's own encryption and verification both pass, but typing the same password into 7-Zip by hand on another computer cannot open the archive.
>
> **Important security notes**:
> - Do not put a plain-text password after the `/pass` parameter. cmdkey prompts you to type the password, so it does not stay in your shell command history.
> - Record this password in your personal password manager or another secure place. Restoring on a new computer depends on it.

### Step B: register the weekly Windows scheduled task

From the main project folder (the project checkout, not a worktree under `.claude\worktrees\`; the script refuses to register when it detects a worktree, because a worktree is deleted after use and the schedule would point to a path that no longer exists), run the registration script with PowerShell 7 (`pwsh`). The script is UTF-8 without BOM, so keep using PowerShell 7 rather than Windows PowerShell 5.1:

```powershell
pwsh -File scripts/register-backup-task.ps1 -Destination "C:\Backups\local-message-readers"
```

- At registration time the script resolves `uv` to a full path and writes that into the schedule (the PATH of a scheduled run does not necessarily contain uv); after reinstalling or moving uv, run the script again.
- The cache refresh has a 15-minute limit; when it hangs it is recorded as a failed refresh and the backup is still made.

- The script registers the task `\LocalMessageReaders\local-message-readers-weekly-backup` (inside the "LocalMessageReaders" folder on the left of Task Scheduler, not in the root).
- It triggers every Sunday at 12:00 noon with `StartWhenAvailable` set (if the computer was off at the scheduled time, the task runs automatically after the next boot and logon).
- It runs only while the user is logged on (Interactive Logon), so the password in Credential Manager can be read.
- The `-WhatIf` preview parameter is supported; `-TaskPath` selects another Task Scheduler folder (default `\LocalMessageReaders\`).

---

## 3. Manual run and log

### Trigger once manually

To verify immediately or create a backup by hand, run this in `<repo>`:

```cmd
uv run python -m readers.backup --dest "C:\Backups\local-message-readers"
```

From another project, use `uv run --project <repo> python -m readers.backup --dest "C:\Backups\local-message-readers"`.

Parameters:
- `--archive-path <path>`: the archive SQLite file to back up (default: `archive_path` in `%LOCALAPPDATA%\local-message-readers\settings.toml`; only when that is unset, the default location of `readers.archive.get_default_archive_path()`).
- `--dest <path>`: the folder that stores the backups (can also be set as `backup_dest` in `%LOCALAPPDATA%\local-message-readers\settings.toml`; when both are set the parameter wins, and the directory must already exist). When neither is set, the backup fails and tries to write a log entry.
- `--settings <file>`: read the given settings file instead (default: `%LOCALAPPDATA%\local-message-readers\settings.toml`; usage in `AGENTS.md > Settings file`). The scheduled weekly backup reads the default `settings.toml`, and the cache refresh before the backup uses the same settings.
- `--keep <count>`: how many weekly backups to keep (default 8).
- `--log-path <path>`: where to write the log file.

### Run log

The result of each run is printed to standard output as a single-line JSON and appended to the log file:
`%LOCALAPPDATA%\local-message-readers\backup-log.jsonl`

Example JSON output:
```json
{
  "refresh": {
    "ok": true,
    "detail": "ok"
  },
  "backup": {
    "ok": true,
    "path": "C:\\Backups\\local-message-readers\\teams-archive-20261111-120000.7z",
    "pruned": ["teams-archive-20260901-120000.7z"],
    "prune_errors": [],
    "error": null
  },
  "wechat": {"ok": true, "error": null, "file_count": 123, "folders": ["db_storage", "msg", "config", "business", "resource"]},
  "started": "2026-11-11T12:00:00+08:00",
  "finished": "2026-11-11T12:00:05+08:00"
}
```

---

## 4. Restore on a New Computer

After replacing the computer or a hardware failure, you only need **7-Zip** and the **backup password** to unpack a backup and restore the historical messages:

1. **Install 7-Zip**:
   Install 7-Zip on the new computer (for example `scoop install 7zip`, or the official installer).
2. **Decrypt the backup**:
   Get the latest `teams-archive-*.7z` from the backup folder (for example in OneDrive) and decrypt and extract it:
   ```cmd
   7z x teams-archive-YYYYMMDD-HHMMSS.7z -o"C:\RestoreTarget"
   ```
   You are asked for the password. A complete backup contains `teams\archive.db`, `wechat\<account folder>` and `wechat\all_keys.json`; a backup where WeChat collection failed, and backups from older versions, contain only `archive.db` in the root. Check `wechat.ok` in the log; do not assume every backup contains WeChat.
3. **Point teams-reader at the restored database** (pick one of the three ways):
   The paths below use a complete backup as the example; for a Teams-only or older backup the file is at `C:\RestoreTarget\archive.db`.
   - **Option 1 (default path)**: copy the decrypted `archive.db` into the default folder:
     `%LOCALAPPDATA%\local-message-readers\archive.db`
   - **Option 2 (settings file)**: set this in `%LOCALAPPDATA%\local-message-readers\settings.toml`:
     `archive_path = "C:/RestoreTarget/teams/archive.db"`
   - **Option 3 (CLI parameter)**: pass the path when running the reader:
     `uv run teams-reader conversations --archive-path "C:\RestoreTarget\teams\archive.db"`
     (`--archive-path` works before or after the subcommand.)
4. **Restore WeChat**: close WeChat first, put `wechat\<account folder>` back into `%USERPROFILE%\Documents\xwechat_files\<account folder>`, and put `wechat\all_keys.json` back at `%USERPROFILE%\.wechat-cli\all_keys.json`. Because `db_storage` in the backup holds standalone WAL-consistent `.db` snapshots without `-wal` or `-shm` sidecars, move any existing target `<account folder>` aside before restoring rather than overwriting in place so stale `-wal`/`-shm` files are not left alongside the restored `.db` files; after restarting WeChat, confirm the data is usable. The `db_storage` databases are transaction-consistent snapshots of committed WAL frames, while the non-database folders (`msg`, `config`, `business`, `resource`) are best-effort copies.

---

### Recommended: do a restore drill after the first backup

The program's `7z t` verification uses the password it read from Credential Manager, which cannot prove that the one you wrote down also opens the archive. After the first backup exists, in any temporary folder run `7z x <latest teams-archive-*.7z> -o<temp folder>` by hand, **type the password you recorded separately when prompted**, confirm you get `teams\archive.db` and the WeChat data and key, and then delete that temporary folder.

---

## 5. Known limitations

- **"In the OneDrive folder" does not mean "uploaded to the cloud"**: the program only confirms that the new backup was written to the local OneDrive folder and passed verification; if OneDrive sync is paused, signed out or out of space, the backup is still reported as successful. Occasionally check in the OneDrive web app that the folder holds recent files.
- **Staleness warnings depend on queries**: when the schedule fails, the result is in `backup-log.jsonl` and in the "Last Run Result" of Task Scheduler. In addition, every `teams-reader` query carries a `backup` object with the time of the last successful backup; when it is older than 8 days or there was never a success, `stale` is true and the agent reports it to the user (issue #21 from the private era). If Teams is not queried for several weeks, this warning is not seen.
- **Old backups are not re-verified**: only the new backup goes through `7z t`; pruning deletes the oldest by the time in the file name. If the archive content itself is wrong (for example a large amount of history was deleted by mistake), 8 consecutive weeks of backups carry the wrong content, and the earliest good one is pruned.
- **Leftover `.partial` files**: if the machine shuts down or logs off during encryption, the folder may keep `teams-archive-*.<random>.partial`. Such a file is not a backup and is not pruned; it can be deleted by hand.

---

## 6. Exit codes

The process exit codes of `readers.backup` are defined as follows:

| Exit code | Meaning | Description |
| :---: | :--- | :--- |
| **`0`** | **Full success** | Teams cache refresh, WeChat collection, 7-Zip encryption and verification all succeeded. |
| **`2`** | **Partial success** | The encrypted backup and verification succeeded, but the Teams cache refresh or the WeChat collection failed; when WeChat fails, the backup contains Teams only. |
| **`1`** | **Backup failed** | The backup was not completed whether or not the refresh succeeded (for example 7-Zip not found, no password in Credential Manager, the destination directory does not exist, or the 7z integrity check failed). |
