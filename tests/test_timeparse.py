"""Time inputs shared by every reader (ADR-0010, issue #1)."""

from datetime import UTC, datetime

import pytest

from readers.timeparse import parse_time_bound, parse_time_range
from readers.wechat import _iso

INSTANT = datetime(2026, 1, 1, 1, 0, tzinfo=UTC).timestamp()


@pytest.mark.parametrize("value", ["2026-01-01T09:00:00+08:00", "2026-01-01T01:00:00Z", "2026-01-01T01:00:00+00:00",
                                   "2026-01-01T08:00:00+07:00"])
def test_offset_and_z_inputs_name_the_same_instant(value):
    assert parse_time_bound(value) == INSTANT


def test_naive_input_is_machine_local_time():
    expected = datetime(2026, 1, 1, 9, 0).astimezone().timestamp()
    assert parse_time_bound("2026-01-01 09:00") == expected
    assert parse_time_bound("2026-01-01 09:00:00") == expected


def test_date_only_end_bound_covers_the_whole_day():
    start = parse_time_bound("2026-01-01")
    end = parse_time_bound("2026-01-01", is_end=True)
    assert start == datetime(2026, 1, 1).astimezone().timestamp()
    assert 86399 < end - start < 86400


def test_output_time_round_trips_as_input():
    ts = 1767229200
    assert parse_time_bound(_iso(ts)) == ts


@pytest.mark.parametrize("value", ["", None])
def test_empty_bound_means_unbounded(value):
    assert parse_time_bound(value) is None


@pytest.mark.parametrize("value", ["yesterday", "2026/01/01", "2026-13-01"])
def test_invalid_input_is_an_error(value):
    with pytest.raises(ValueError):
        parse_time_bound(value)


def test_start_later_than_end_is_an_error():
    with pytest.raises(ValueError):
        parse_time_range("2026-01-02", "2026-01-01")
