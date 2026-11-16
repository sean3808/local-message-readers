"""Time inputs shared by every reader (ADR-0010).

Accepted: ISO 8601 with an offset or `Z`, epoch seconds, and the naive forms YYYY-MM-DD / YYYY-MM-DD HH:MM[:SS],
which are read in the machine's current time zone. A message's `time` output is valid input unchanged, so callers
can pass the last value they read back as `--since` without depending on the machine's time zone.
"""

from __future__ import annotations

import math
from datetime import datetime

FORMATS_HELP = "YYYY-MM-DD [HH:MM[:SS]] (machine-local time), or ISO 8601 with an offset or Z such as a message's `time`"


def parse_time_bound(val: str | float | None, is_end: bool = False) -> float | None:
    """Return epoch seconds, or None for an empty value. A date-only end bound means the end of that day."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        f = float(s)
        if not (math.isnan(f) or math.isinf(f)):
            return f
    except ValueError:
        pass

    # YYYY-MM-DD
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        s = f"{s} 23:59:59.999999" if is_end else f"{s} 00:00:00"

    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt).astimezone()
            if is_end and fmt == "%Y-%m-%d %H:%M":
                dt = dt.replace(second=59, microsecond=999999)
            return dt.timestamp()
        except ValueError:
            pass

    raise ValueError(f"Invalid time format: '{val}'. Supported: YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS or an ISO timestamp")


def parse_time_range(start: str | None, end: str | None) -> tuple[float | None, float | None]:
    start_ts = parse_time_bound(start, is_end=False)
    end_ts = parse_time_bound(end, is_end=True)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("The start time is later than the end time")
    return start_ts, end_ts
