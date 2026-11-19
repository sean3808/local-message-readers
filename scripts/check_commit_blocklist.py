"""Check staged blobs against a private, literal, case-insensitive blocklist."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from readers.settings import SETTINGS_HELP, SettingsError, configure, get_path_setting, get_setting


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def load_entries():
    path = get_path_setting("commit_blocklist")
    if path is None:
        raise ValueError("missing blocklist")
    entries = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("# "):
            continue
        line = line.removeprefix("\\")
        if line:
            entries.append(line.casefold())
    if not entries:
        raise ValueError("empty blocklist")
    return entries


def load_public():
    """Public strings (longest first, casefolded) masked out before matching."""
    values = get_setting("commit_blocklist_public") or []
    return sorted({value.casefold() for value in values}, key=len, reverse=True)


def mask(text, public):
    """Casefold text and replace every occurrence of each public string with NULs, which no blocklist
    entry contains. Occurrences are found on the unmasked text, so overlapping ones are all covered."""
    text = text.casefold()
    covered = bytearray(len(text))
    for value in public:
        start = text.find(value)
        while start != -1:
            covered[start:start + len(value)] = b"\1" * len(value)
            start = text.find(value, start + 1)
    if not any(covered):
        return text
    return "".join("\0" if flag else char for char, flag in zip(text, covered))


def files_to_check(head):
    """Return (path, blob reference); HEAD uses object IDs, index uses :path."""
    if not head:
        paths = git("diff", "--cached", "--name-only", "--diff-filter=d", "-z").split(b"\0")
        return [(path, b":" + path) for path in paths if path]
    files = []
    for record in git("ls-tree", "-r", "-z", "--full-tree", "HEAD").split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            _, kind, object_id = metadata.split()
            if kind == b"blob":
                files.append((path, object_id))
    return files


def scan_files(entries, files, public=()):
    """Return hit locations. A path that itself matches is never printed: it is reported by its
    1-based position in the scanned file list (the order of `git diff --cached --name-only`, or of
    `git ls-tree -r --full-tree HEAD` with --head), so the output never contains a blocked string."""
    hits = []
    for position, (raw_path, blob_ref) in enumerate(files, 1):
        name = raw_path.decode("utf-8", "surrogateescape")
        if any(entry in mask(name, public) for entry in entries):
            name = f"<file #{position}, name withheld>"
            hits.append(f"{name}:path")
        data = git("cat-file", "blob", os.fsdecode(blob_ref)).decode("utf-8", "replace")
        for number, line in enumerate(data.split("\n"), 1):
            if any(entry in mask(line, public) for entry in entries):
                hits.append(f"{name}:{number}")
    return hits


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--head", action="store_true", help="scan all tracked files at HEAD")
    parser.add_argument("--settings", default=None, help=SETTINGS_HELP)
    args = parser.parse_args(argv)
    try:
        configure(args.settings)
        hits = scan_files(load_entries(), files_to_check(args.head), load_public())
        for hit in hits:
            print(hit)
        return int(bool(hits))
    except SettingsError as exc:
        # Settings errors name the settings file and key; they never include blocklist entries
        print(f"commit blocklist check failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.CalledProcessError):
        # ValueError covers UnicodeDecodeError and missing/empty blocklist; never echo its text,
        # because a decode error can quote bytes from the private blocklist file
        print("commit blocklist check failed; verify commit_blocklist in the settings file (--settings, or %LOCALAPPDATA%\\local-message-readers\\settings.toml), its UTF-8 file, and Git state", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
