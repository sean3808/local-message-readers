"""Guard: running the suite never touches the real archive (issue #42).

File name sorts last so every other test has already run. The check only stats the file.
"""

from conftest import _stat_mtime_ns


def test_real_archive_mtime_is_unchanged(real_archive_mtime_at_start):
    path, before = real_archive_mtime_at_start
    assert _stat_mtime_ns(path) == before, "a test modified, created or removed the real archive.db"
