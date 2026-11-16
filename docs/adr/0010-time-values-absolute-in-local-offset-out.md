# Store absolute instants, output local time with its offset, accept any unambiguous time as input

Every reader stores message times as absolute instants (epoch values) and outputs each message with two fields: `time`, an ISO 8601 local time carrying its offset (for example `2026-01-01T09:00:00+08:00`), and `timestamp`, epoch seconds. Time inputs (`--since`, `--start`, `--end`) are parsed by one shared function, `readers/timeparse.py`, and accept ISO 8601 with an offset or `Z`, epoch seconds, and the naive forms `YYYY-MM-DD [HH:MM[:SS]]`, which mean machine-local time. A date-only end bound covers the whole day.

The usual rule "the backend speaks UTC and the frontend renders local time" is about keeping instants unambiguous; an offset-carrying local time is exactly as unambiguous as UTC. The consumer here is an agent that reasons in the user's own day ("who wrote this morning"), so handing it local time with the offset removes a conversion it could get wrong, while `timestamp` stays available for arithmetic.

Naive input depends on the machine's current time zone: after the Windows time zone changes, the same `--since` value names a different instant. A caller that passes back the previous message's `time` unchanged is immune to that, so the docs and `--help` recommend that round trip.

## Considered Options

- **Output UTC only (`Z`)** — rejected. Unambiguous, but every caller then converts to the user's day, and an agent that skips or botches the conversion mislabels messages by hours.
- **Reject naive input** — rejected. Typing a date by hand is the common case for a person, and the machine-local meaning is predictable.

## Consequences

- A new reader uses `readers/timeparse.py` for its time inputs and emits both `time` and `timestamp`.
- Before issue #1 the WeChat reader rejected offset-carrying input, so the round trip worked only on Teams.
