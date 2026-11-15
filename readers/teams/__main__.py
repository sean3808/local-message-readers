"""Entrypoint for python -m readers.teams."""

import sys

from readers.teams.cli import main

if __name__ == "__main__":
    sys.exit(main())
