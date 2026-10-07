"""Committed prefix for the four-column private engine event log."""

import json
from numbers import Integral

EVENT_COLUMNS = ("timestamp", "severity", "name", "message")


def committed_event_count(events) -> int:
    lengths = [len(events[name]) for name in EVENT_COLUMNS]
    if "committed_count" in events.attrs:
        count = events.attrs["committed_count"]
        if isinstance(count, bool) or not isinstance(count, Integral) or not 0 <= count <= min(lengths):
            raise ValueError("Invalid committed event count or missing committed event rows.")
        return int(count)
    # Older archives had no marker. Discard only an incomplete trailing row;
    # never manufacture a resume boundary from a resized but unwritten cell.
    count = min(lengths)
    while count:
        row = [events[name].asstr()[count - 1] for name in EVENT_COLUMNS]
        try:
            complete = all(row) and isinstance(json.loads(row[-1]), dict)
        except (ValueError, TypeError):
            complete = False
        if complete:
            break
        count -= 1
    return count
