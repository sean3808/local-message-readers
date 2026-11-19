"""Print one version's section of CHANGELOG.md, the body of that version's GitHub Release.

Usage: python scripts/changelog_section.py <version> [--changelog <file>]
Exits 1 with a message on stderr when the section is missing or empty, so a release
without written notes stops instead of publishing blank notes.
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(text: str, version: str) -> str | None:
    """Body under `## [<version>]` up to the next `## [` heading, stripped; None when absent."""
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    match = heading.search(text)
    if match is None:
        return None
    rest = text[match.end():]
    following = re.search(r"^## \[", rest, re.MULTILINE)
    return (rest[: following.start()] if following else rest).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="version without the leading v, for example 0.2.0")
    parser.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG)
    args = parser.parse_args(argv)
    body = section(args.changelog.read_text(encoding="utf-8"), args.version)
    if not body:
        print(f"{args.changelog.name} has no notes under '## [{args.version}]'; write them before releasing.", file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
