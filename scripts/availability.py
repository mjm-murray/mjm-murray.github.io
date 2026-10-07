"""Compute public availability windows from private Google Calendar iCal feeds.

Reads one or more secret iCal URLs from CALENDAR_ICS_URLS (whitespace-separated),
pads every busy event, and writes only the resulting free windows -- no event
titles or busy times -- to the JSON file given as the first argument.
"""

import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests

TZ = ZoneInfo(os.environ.get("AVAIL_TZ", "America/Los_Angeles"))
PADDING = dt.timedelta(minutes=int(os.environ.get("AVAIL_PADDING_MIN", "15")))
MIN_SLOT = dt.timedelta(minutes=int(os.environ.get("AVAIL_MIN_SLOT_MIN", "30")))
DAYS_AHEAD = int(os.environ.get("AVAIL_DAYS_AHEAD", "6"))
DAY_START = dt.time(9, 0)
DAY_END = dt.time(17, 0)
ROUND_TO = dt.timedelta(minutes=15)


def to_local(value):
    """Normalize an iCal DTSTART/DTEND value to an aware datetime in TZ."""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
    # All-day events are plain dates.
    return dt.datetime.combine(value, dt.time(0), tzinfo=TZ)


def declined(event, owner):
    attendees = event.get("ATTENDEE", [])
    if not isinstance(attendees, list):
        attendees = [attendees]
    for attendee in attendees:
        if str(attendee).lower().removeprefix("mailto:") == owner:
            return str(attendee.params.get("PARTSTAT", "")).upper() == "DECLINED"
    return False


def busy_intervals(url, start, end):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    cal = icalendar.Calendar.from_ical(resp.content)
    owner = str(cal.get("X-WR-CALNAME", "")).lower()

    for event in recurring_ical_events.of(cal).between(start, end):
        if str(event.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
            continue
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if declined(event, owner):
            continue
        ev_start = to_local(event["DTSTART"].dt)
        if "DTEND" in event:
            ev_end = to_local(event["DTEND"].dt)
        elif "DURATION" in event:
            ev_end = ev_start + event["DURATION"].dt
        else:
            ev_end = ev_start
        yield ev_start - PADDING, ev_end + PADDING


def ceil_time(t):
    floor = t.replace(second=0, microsecond=0, minute=0)
    while floor < t:
        floor += ROUND_TO
    return floor


def free_slots(day, busy, now):
    window_start = dt.datetime.combine(day, DAY_START, tzinfo=TZ)
    window_end = dt.datetime.combine(day, DAY_END, tzinfo=TZ)
    cursor = max(window_start, ceil_time(now))
    slots = []
    for b_start, b_end in busy:
        if b_end <= cursor or b_start >= window_end:
            continue
        if b_start - cursor >= MIN_SLOT:
            slots.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if window_end - cursor >= MIN_SLOT:
        slots.append((cursor, window_end))
    return slots


def main(out_path):
    urls = os.environ.get("CALENDAR_ICS_URLS", "").split()
    if not urls:
        sys.exit("CALENDAR_ICS_URLS is not set")

    now = dt.datetime.now(TZ)
    range_start = dt.datetime.combine(now.date(), dt.time(0), tzinfo=TZ)
    range_end = range_start + dt.timedelta(days=DAYS_AHEAD + 1)

    busy = sorted(b for url in urls for b in busy_intervals(url, range_start, range_end))

    days = []
    for offset in range(DAYS_AHEAD + 1):
        day = now.date() + dt.timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        slots = free_slots(day, busy, now)
        days.append({
            "date": day.isoformat(),
            "slots": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in slots],
        })

    data = {
        "generated": now.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "timezone": str(TZ),
        "paddingMinutes": int(PADDING.total_seconds() // 60),
        "days": days,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {sum(len(d['slots']) for d in days)} windows across {len(days)} weekdays")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "availability.json")
