"""Weekly Teams and WeChat 7-Zip AES-256 encrypted backup (issues #17, #30).

Periodically refreshes the Teams archive, collects raw WeChat data and keys, and backs them up with 7-Zip
using AES-256 encryption (including header encryption, -mhe=on) into the specified backup folder.
The password is read from Windows Credential Manager; it is never written to disk, never logged and never printed to stdout.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

from readers.archive import get_default_archive_path
from readers.settings import SETTINGS_HELP, active_settings_path, get_path_setting, load_settings
from readers.settings import configure as configure_settings

__all__ = [
    "CREDENTIAL_TARGET",
    "DEFAULT_KEEP_COUNT",
    "CredentialNotFoundError",
    "collect_wechat",
    "encrypt_and_verify",
    "find_7z_binary",
    "get_default_log_path",
    "lookup_windows_credential",
    "main",
    "make_consistent_copy",
    "prune_old_backups",
    "read_windows_credential",
    "run_backup",
    "run_refresh",
]

CREDENTIAL_TARGET = "local-message-readers/archive-backup"
DEFAULT_KEEP_COUNT = 8
REFRESH_TIMEOUT_SECONDS = 15 * 60

CRED_TYPE_GENERIC = 1


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class CredentialNotFoundError(RuntimeError):
    """Raised when Windows Credential Manager does not contain the target credential."""


def get_default_log_path() -> Path:
    """Get the default path of the backup-log.jsonl file."""
    return get_default_archive_path().parent / "backup-log.jsonl"


def lookup_windows_credential(target_name: str = CREDENTIAL_TARGET) -> str | None:
    """Lookup generic credential from Windows Credential Manager.

    Returns password string, or None if missing.
    """
    advapi32 = getattr(getattr(ctypes, "windll", None), "advapi32", None)
    if advapi32 is None:
        return None

    CredReadW = advapi32.CredReadW
    CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
    ]
    CredReadW.restype = wintypes.BOOL

    CredFree = advapi32.CredFree
    CredFree.argtypes = [ctypes.c_void_p]
    CredFree.restype = None

    cred_ptr = ctypes.POINTER(CREDENTIALW)()
    ok = CredReadW(target_name, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr))
    if not ok:
        return None

    try:
        cred = cred_ptr.contents
        blob_size = cred.CredentialBlobSize
        if not cred.CredentialBlob or blob_size == 0:
            return ""
        blob_bytes = ctypes.string_at(cred.CredentialBlob, blob_size)
        return blob_bytes.decode("utf-16-le").rstrip("\0")
    finally:
        CredFree(cred_ptr)


def read_windows_credential(target_name: str = CREDENTIAL_TARGET) -> str:
    """Read password from Windows Credential Manager.

    Raises:
        CredentialNotFoundError: if target is not found in Credential Manager.
    """
    cred = lookup_windows_credential(target_name)
    if cred is None:
        raise CredentialNotFoundError(
            f"Windows Credential Manager target '{target_name}' is missing. "
            f"Please set it up using cmdkey per docs/teams-archive-backup.md"
        )
    return cred


def find_7z_binary() -> Path:
    """Locate 7z executable via PATH or Program Files."""
    found = shutil.which("7z")
    if found:
        return Path(found)

    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    default_7z = Path(prog_files) / "7-Zip" / "7z.exe"
    if default_7z.is_file():
        return default_7z

    raise FileNotFoundError(
        "7-Zip (7z.exe) not found. Please install 7-Zip or ensure 7z is in PATH or '%ProgramFiles%\\7-Zip\\7z.exe'."
    )


def default_refresh_command(archive_path: Path) -> list[str]:
    """teams-reader refresh command for this archive.

    `--archive-path` must come after the subcommand: teams-reader silently drops it when given
    before (the subcommand's default overwrites it), and the refresh would update a different file
    than the one being backed up.
    """
    command = [
        sys.executable,
        "-m",
        "readers.teams",
        "conversations",
        "--archive-path",
        str(archive_path),
        "--limit",
        "1",
        # The backup must see a real refresh, never a throttled skip
        "--refresh",
    ]
    settings_file = active_settings_path()
    if settings_file is not None:
        # The refresh must read the same settings (profile_dir, self id) as this backup run
        command += ["--settings", str(settings_file)]
    return command


def run_refresh(
    archive_path: Path,
    refresh_command: list[str] | None = None,
    timeout: float = REFRESH_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Run teams-reader conversations refresh as a subprocess.

    A hung refresh must not block the backup, so the subprocess has a time limit.
    """
    if refresh_command is None:
        cmd = default_refresh_command(archive_path)
    else:
        cmd = list(refresh_command)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"Refresh timed out after {timeout:g} seconds"
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed to run refresh command: {exc}"

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        err = stderr or stdout or f"exit code {proc.returncode}"
        return False, f"Refresh process failed (exit code {proc.returncode}): {err}"

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return False, f"Refresh stdout is not valid JSON: {exc}"

    if not isinstance(data, dict):
        return False, f"Refresh JSON output is not an object (got {type(data).__name__})"

    if data.get("error"):
        return False, f"Refresh returned error: {data['error']}"

    failures = data.get("failures")
    if failures:
        return False, f"Refresh returned failures: {failures}"

    return True, "ok"


def make_consistent_copy(archive_path: Path, temp_copy_path: Path) -> None:
    """Make a consistent SQLite copy of the archive file.

    Uses URI file:...?mode=ro with Connection.backup() to flush committed WAL
    data, sets journal_mode=DELETE on the standalone copy, and verifies integrity.
    """
    if not archive_path.is_file():
        raise FileNotFoundError(f"Archive file not found: {archive_path}")

    temp_copy_path.parent.mkdir(parents=True, exist_ok=True)
    uri = f"{archive_path.resolve().as_uri()}?mode=ro"
    src_conn = sqlite3.connect(uri, uri=True)
    try:
        dst_conn = sqlite3.connect(temp_copy_path)
        try:
            src_conn.backup(dst_conn)
            dst_conn.execute("PRAGMA journal_mode=DELETE")
            row = dst_conn.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0].lower() != "ok":
                raise RuntimeError(f"Integrity check failed on archive copy: {row}")
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _run_7z_step(
    seven_zip: Path,
    args: list[str],
    password: str,
    step_name: str,
    cwd: Path | None = None,
) -> None:
    """Execute a 7-Zip subcommand with error masking."""
    cmd = [str(seven_zip), *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=cwd,
    )
    if proc.returncode != 0:
        stdout_str = (proc.stdout or "").strip()
        stderr_str = (proc.stderr or "").strip()
        err_msg = stderr_str or stdout_str or f"exit code {proc.returncode}"
        if password:
            err_msg = err_msg.replace(password, "***")
        raise RuntimeError(f"7-Zip {step_name} failed: {err_msg}")


def encrypt_and_verify(
    seven_zip: Path,
    source_file: Path,
    dest_dir: Path,
    password: str,
    _before_verify_hook: Callable[[Path], None] | None = None,
    extra_source: Path | None = None,
) -> Path:
    """Encrypt into a uniquely named .partial in dest, verify it with 7-Zip, then rename to the final name.

    Verification happens before the rename, so the failure path only ever deletes the .partial this
    run created (its name is unique per run) and never a file carrying a final backup name. On
    Windows the rename refuses to replace an existing file, so a same-second rerun cannot overwrite
    a backup either.
    """
    if not dest_dir.is_dir():
        raise FileNotFoundError(f"Backup destination directory does not exist: {dest_dir}")

    timestamp_str = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    final_path = dest_dir / f"teams-archive-{timestamp_str}.7z"
    partial_path = dest_dir / f"teams-archive-{timestamp_str}.{secrets.token_hex(4)}.partial"

    try:
        _run_7z_step(
            seven_zip=seven_zip,
            args=["a", "-t7z", "-mhe=on", f"-p{password}", "-y", str(partial_path.resolve()),
                  *([source_file.name, extra_source.name] if extra_source else [str(source_file)])],
            password=password,
            step_name="encryption",
            cwd=source_file.parent if extra_source else None,
        )
        if not partial_path.is_file():
            raise RuntimeError("7-Zip encryption failed: partial archive was not created")

        if _before_verify_hook is not None:
            _before_verify_hook(partial_path)

        _run_7z_step(
            seven_zip=seven_zip,
            args=["t", "-t7z", f"-p{password}", "-y", str(partial_path)],
            password=password,
            step_name="verification",
        )

        try:
            partial_path.rename(final_path)
        except FileExistsError as exc:
            raise FileExistsError(f"Backup file already exists, refusing to overwrite: {final_path}") from exc
    except Exception:
        # Only the uniquely named .partial belongs to this run; never touch final-named files here.
        if partial_path.exists():
            try:
                partial_path.unlink()
            except OSError:
                pass
        raise

    if not final_path.is_file():
        raise RuntimeError(f"Verified backup is missing after rename: {final_path}")
    return final_path


def validate_password(password: str) -> None:
    """Reject passwords that would fail or not round-trip through 7-Zip's command line.

    Measured with 7-Zip 26.05 on Windows: a `"` makes `7z a` fail with "Cannot find archive name";
    a non-ASCII password, or one containing a space and ending in `\\` (argv quoting doubles the
    trailing backslashes and 7-Zip does not halve them), encrypts and self-verifies via argv but
    cannot be typed back at 7-Zip's interactive password prompt. Only printable ASCII without `"`
    and not ending in `\\` is accepted, so the password the user types on a new computer opens the backup.
    """
    if not password:
        raise RuntimeError(
            f"Password for '{CREDENTIAL_TARGET}' is empty. Please set it up per docs/teams-archive-backup.md"
        )
    if any(not (" " <= ch <= "~") or ch == '"' for ch in password) or password.endswith("\\"):
        raise RuntimeError(
            f"Password for '{CREDENTIAL_TARGET}' must be printable ASCII, must not contain a double quote "
            f"and must not end with a backslash (see docs/teams-archive-backup.md). Re-create it with cmdkey."
        )


def _validate_keep(keep: int) -> None:
    if keep < 1:
        raise ValueError(f"keep count must be at least 1 (the new backup is always kept), got {keep}")


def collect_wechat(root: Path, key: Path, destination: Path) -> tuple[int, list[str]]:
    """Copy a stable read-only WeChat selection, retrying once on changes."""
    accounts = [p for p in root.iterdir() if p.is_dir() and (p / "db_storage").is_dir()]
    if len(accounts) != 1:
        raise RuntimeError(f"Expected one WeChat account under {root}; found {len(accounts)}")
    account = accounts[0]
    if not key.is_file():
        raise FileNotFoundError(f"WeChat key not found: {key}")
    resolved_destination = destination.resolve()
    for protected in (account.resolve(), key.parent.resolve()):
        if resolved_destination.is_relative_to(protected) or protected.is_relative_to(resolved_destination):
            raise ValueError(f"WeChat destination overlaps source: {destination}")
    names = ("db_storage", "msg", "config", "business", "resource")

    def inventory() -> tuple[dict[Path, tuple[int, int]], list[str]]:
        files = [key]
        folders = []
        for name in names:
            folder = account / name
            if not folder.is_dir():
                if name == "db_storage":
                    raise FileNotFoundError(f"WeChat folder not found: {folder}")
                continue
            folders.append(name)

            def raise_walk_error(exc: OSError) -> None:
                raise exc

            for directory, _, filenames in os.walk(folder, onerror=raise_walk_error):
                files.extend(Path(directory) / filename for filename in filenames)
        stats = {p: p.stat() for p in files}
        return {p: (st.st_size, st.st_mtime_ns) for p, st in stats.items()}, folders

    for attempt in range(2):
        before, folders = inventory()
        if destination.exists():
            shutil.rmtree(destination)
        for name in folders:
            (destination / account.name / name).mkdir(parents=True, exist_ok=True)
        for source in before:
            target = destination / (Path("all_keys.json") if source == key else Path(account.name) / source.relative_to(account))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if (before, folders) == inventory():
            return len(before), folders
    raise RuntimeError("WeChat files changed during both copy attempts")


BACKUP_NAME_RE = re.compile(r"^teams-archive-(\d{8}-\d{6})\.7z$")


def prune_old_backups(
    dest_dir: Path, new_backup: Path, keep: int = DEFAULT_KEEP_COUNT
) -> tuple[list[str], list[str]]:
    """Keep `new_backup` plus the newest `keep - 1` other backups; return (pruned, errors).

    Only files named exactly `teams-archive-YYYYMMDD-HHMMSS.7z` count as backups; anything else in
    the folder is never counted or deleted. The just-verified backup always survives. A file that
    cannot be deleted is reported in errors and does not undo the already-verified backup.
    """
    _validate_keep(keep)

    backups: list[tuple[str, Path]] = []
    for entry in dest_dir.iterdir():
        m = BACKUP_NAME_RE.match(entry.name)
        if m and entry.is_file() and entry.name != new_backup.name:
            backups.append((m.group(1), entry))

    backups.sort(key=lambda item: item[0], reverse=True)

    pruned: list[str] = []
    errors: list[str] = []
    for _, f in backups[keep - 1 :]:
        try:
            f.unlink()
        except OSError as exc:
            errors.append(f"{f.name}: {exc}")
        else:
            pruned.append(f.name)

    return pruned, errors


def run_backup(
    archive_path: Path | None = None,
    dest: Path | None = None,
    keep: int = DEFAULT_KEEP_COUNT,
    log_path: Path | None = None,
    password_provider: Callable[[], str] | None = None,
    refresh_command: list[str] | None = None,
    find_7z: Callable[[], Path] | None = None,
    _before_verify_hook: Callable[[Path], None] | None = None,
    wechat_root: Path | None = None,
    wechat_key: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Execute weekly refresh and encrypted 7z backup."""
    started_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    refresh_ok = False
    refresh_detail = ""
    backup_ok = False
    backup_path: str | None = None
    pruned: list[str] = []
    prune_errors: list[str] = []
    backup_error: str | None = None
    wechat: dict[str, Any] = {"ok": False, "error": "WeChat collection not attempted", "file_count": 0, "folders": []}

    resolved_archive_path = archive_path or get_default_archive_path()
    resolved_dest_dir = dest if dest is not None else get_path_setting("backup_dest")
    resolved_log_path = log_path or get_default_log_path()

    # Step 1: Refresh (errors recorded and continue to backup)
    if resolved_dest_dir is not None:
        refresh_ok, refresh_detail = run_refresh(
            resolved_archive_path,
            refresh_command=refresh_command,
        )
    else:
        refresh_detail = "Backup destination is required: specify --dest or set backup_dest in settings.toml"
        sys.stderr.write(refresh_detail + "\n")

    # Step 2: Backup
    password_val: str | None = None
    try:
        if resolved_dest_dir is None:
            raise ValueError(refresh_detail)
        _validate_keep(keep)

        # Check archive exists before anything else
        if not resolved_archive_path.is_file():
            raise FileNotFoundError(f"Archive file not found: {resolved_archive_path}")

        # Check dest folder exists (must already exist, do not create)
        if not resolved_dest_dir.is_dir():
            raise FileNotFoundError(f"Destination folder does not exist: {resolved_dest_dir}")

        # Get password
        if password_provider is not None:
            password_val = password_provider()
        else:
            password_val = read_windows_credential(CREDENTIAL_TARGET)

        validate_password(password_val)

        # Find 7z
        seven_zip_path = find_7z() if find_7z else find_7z_binary()

        # Step 3: Consistent copy into fresh temp dir
        # A temp folder that cannot be cleaned up (antivirus or index lock) must not invalidate a verified backup
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            temp_copy = Path(temp_dir) / "archive.db"
            make_consistent_copy(resolved_archive_path, temp_copy)

            profile = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
            wechat_copy = Path(temp_dir) / "wechat"
            try:
                count, folders = collect_wechat(
                    wechat_root if wechat_root is not None else profile / "Documents" / "xwechat_files",
                    wechat_key if wechat_key is not None else profile / ".wechat-cli" / "all_keys.json",
                    wechat_copy,
                )
                wechat = {"ok": True, "error": None, "file_count": count, "folders": folders}
            except Exception as exc:  # noqa: BLE001 — Teams backup must continue
                wechat = {"ok": False, "error": str(exc), "file_count": 0, "folders": []}

            source = temp_copy
            if wechat["ok"]:
                teams_dir = Path(temp_dir) / "teams"
                teams_dir.mkdir()
                temp_copy.rename(teams_dir / "archive.db")
                source = teams_dir

            # Step 4: Encrypt and verify
            final_archive = encrypt_and_verify(
                seven_zip=seven_zip_path,
                source_file=source,
                dest_dir=resolved_dest_dir,
                password=password_val,
                _before_verify_hook=_before_verify_hook,
                extra_source=wechat_copy if wechat["ok"] else None,
            )

        # The new backup is verified and renamed to its final name: from here on the backup counts as successful, and pruning afterwards must not overturn it
        backup_ok = True
        backup_path = str(final_archive)
    except Exception as exc:  # noqa: BLE001
        backup_ok = False
        err_str = str(exc)
        if password_val:
            err_str = err_str.replace(password_val, "***")
        backup_error = err_str

    # Step 5: Prune old copies, only after a verified backup; any failure here is a prune error.
    if backup_ok:
        try:
            pruned, prune_errors = prune_old_backups(resolved_dest_dir, Path(backup_path), keep=keep)
        except Exception as exc:  # noqa: BLE001 — a pruning failure is only reported and does not affect the verified backup
            prune_errors = [f"prune failed: {exc}"]

    finished_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    report: dict[str, Any] = {
        "wechat": wechat,
        "refresh": {
            "ok": refresh_ok,
            "detail": refresh_detail,
        },
        "backup": {
            "ok": backup_ok,
            "path": backup_path,
            "pruned": pruned,
            "prune_errors": prune_errors,
            "error": backup_error,
        },
        "started": started_iso,
        "finished": finished_iso,
    }

    json_line = json.dumps(report, ensure_ascii=False)

    # Print to stdout
    print(json_line)

    # Append to log file
    try:
        resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved_log_path, "a", encoding="utf-8") as f:
            f.write(json_line + "\n")
    except OSError as log_exc:
        sys.stderr.write(f"Warning: Failed to write to backup log: {log_exc}\n")

    if backup_ok and refresh_ok and wechat["ok"]:
        exit_code = 0
    elif backup_ok:
        exit_code = 2
    else:
        exit_code = 1

    return report, exit_code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Teams and WeChat weekly backup")
    parser.add_argument(
        "--archive-path",
        type=Path,
        default=None,
        help="Path to archive SQLite database",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Destination directory for encrypted backup",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP_COUNT,
        help=f"Number of weekly backups to keep (default: {DEFAULT_KEEP_COUNT})",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=None,
        help="Path to backup-log.jsonl",
    )

    parser.add_argument(
        "--settings",
        type=Path,
        default=None,
        help=SETTINGS_HELP,
    )

    args = parser.parse_args(argv)
    try:
        configure_settings(args.settings)
        load_settings()
    except ValueError as exc:
        sys.stderr.write(f"Invalid settings: {exc}\n")
        sys.exit(1)

    _, exit_code = run_backup(
        archive_path=args.archive_path,
        dest=args.dest,
        keep=args.keep,
        log_path=args.log_path,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
