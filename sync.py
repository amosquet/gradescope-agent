"""One-shot homework sync: Gradescope -> Google Calendar, idempotent.

Runs entirely inside one tool call so a sync costs one model round-trip plus
plain HTTP calls, instead of one model call per calendar event.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from gcal import call_google_calendar
from gradescope_client import (
    GradescopeError,
    fetch_course_assignments,
    list_current_student_courses,
)

EVENT_PREFIX = "[GS]"
UPCOMING_STATUSES = ("before_due", "accepting_late")


GROUP_CALENDAR_SUFFIXES = (
    "@group.calendar.google.com",
    "@import.calendar.google.com",
    "@v.calendar.google.com",
)


def pick_primary_calendar(calendar_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The account's own primary calendar: flagged primary, else the first
    non-group calendar whose id is an email address."""
    for calendar in calendar_list:
        if calendar.get("primary"):
            return calendar
    for calendar in calendar_list:
        calendar_id = calendar.get("id") or ""
        if "@" in calendar_id and not calendar_id.endswith(GROUP_CALENDAR_SUFFIXES):
            return calendar
    return calendar_list[0] if calendar_list else None


def event_title(short_name: str | None, course_name: str | None, assignment_name: str, kind: str) -> str:
    course = short_name or course_name or "Gradescope"
    return f"{EVENT_PREFIX} {course} - {assignment_name} ({kind})"


def event_description(course_name: str | None, assignment: dict[str, Any]) -> str:
    url = assignment["url"]
    if url.startswith("/"):
        url = f"https://www.gradescope.com{url}"
    lines = [
        f"Course: {course_name or assignment.get('course_name') or ''}".rstrip(),
        f"Assignment: {assignment['name']}",
        f"Link: {url}",
        f"Deadline status: {assignment.get('deadline_status')}",
        f"Submission: {assignment.get('submission_status')}",
    ]
    if assignment.get("points_awarded") is not None and assignment.get("points_possible") is not None:
        lines.append(f"Score: {assignment['points_awarded']} / {assignment['points_possible']}")
    if assignment.get("late_due_date"):
        lines.append(f"Late deadline: {assignment['late_due_date']}")
    return "\n".join(lines)


def event_time(iso: str | None, time_zone: str | None) -> dict[str, Any] | None:
    """Connector-shaped time arguments for a due timestamp: flat startTime/
    endTime ISO strings with a 1h duration; all-day (midnight UTC) if date-only.

    The google_calendar connector's create_event/update_event take top-level
    startTime/endTime strings (plus optional timeZone / allDay), not Google's
    nested {dateTime|date, timeZone} start/end objects.
    """
    if not iso:
        return None
    if "T" in iso:
        begin = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        times: dict[str, Any] = {"startTime": iso, "endTime": (begin + timedelta(hours=1)).isoformat()}
        if time_zone:
            times["timeZone"] = time_zone
        return times
    day = datetime.fromisoformat(iso).date()
    return {
        "allDay": True,
        "startTime": f"{day.isoformat()}T00:00:00Z",
        "endTime": f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z",
    }


def _parse_instant(value: str, time_zone: str | None) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(time_zone) if time_zone else timezone.utc)
    return parsed


def desired_start_key(times: dict[str, Any], time_zone: str | None) -> str:
    """Comparable key for a planned event's start: UTC instant, or the date for all-day."""
    if times.get("allDay"):
        return times["startTime"][:10]
    return _parse_instant(times["startTime"], time_zone or times.get("timeZone")).astimezone(timezone.utc).isoformat()


def existing_start_key(event: dict[str, Any]) -> str | None:
    """Comparable key for an existing event's start, from the Google event resource shape."""
    start = event.get("start") or {}
    date_time = start.get("dateTime")
    if date_time:
        return _parse_instant(date_time, start.get("timeZone") or start.get("eventTimeZone")).astimezone(timezone.utc).isoformat()
    return start.get("date")


def plan_events(assignments_by_course: list[dict[str, Any]], time_zone: str | None) -> list[dict[str, Any]]:
    """Build the desired event list from the fetched courses/assignments."""
    desired: list[dict[str, Any]] = []
    for course in assignments_by_course:
        for assignment in course["assignments"]:
            if assignment.get("deadline_status") not in UPCOMING_STATUSES or not assignment.get("released", True):
                continue
            short = course.get("short_name")
            name = course.get("name")
            for kind, iso_key in (("due", "due_date_iso"), ("LATE deadline", "late_due_date_iso")):
                if kind == "LATE deadline" and not assignment.get("late_due_date_iso"):
                    continue
                if kind == "LATE deadline" and assignment.get("late_due_date_iso") == assignment.get("due_date_iso"):
                    continue
                times = event_time(assignment.get(iso_key), time_zone)
                if times is None:
                    continue
                desired.append(
                    {
                        "summary": event_title(short, name, assignment["name"], kind),
                        "times": times,
                        "start_key": desired_start_key(times, time_zone),
                        "description": event_description(name, assignment),
                        "assignment_id": assignment["id"],
                    }
                )
    return desired


async def list_existing_gs_events(calendar_id: str | None, window_start: str, window_end: str, time_zone: str | None) -> list[dict[str, Any]]:
    # NB: the connector's fullText filter is unreliable, so list the window and
    # filter by the "[GS]" title prefix client-side.
    events: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        page = await call_google_calendar(
            "list_events",
            {
                **({"calendarId": calendar_id} if calendar_id else {}),
                "startTime": window_start,
                "endTime": window_end,
                "pageSize": 250,
                **({"pageToken": page_token} if page_token else {}),
                **({"timeZone": time_zone} if time_zone else {}),
            },
        )
        events.extend(e for e in (page.get("events") or []) if (e.get("summary") or "").startswith(EVENT_PREFIX))
        page_token = page.get("nextPageToken")
        if not page_token:
            return events


async def sync_homework_to_calendar() -> dict[str, Any]:
    """Fetch upcoming Gradescope assignments (current-term courses) and sync [GS] events to the primary Google Calendar. Idempotent: matches by exact event title, updates only on change, never deletes."""
    # The calendar list and the Gradescope scan are independent; overlap them.
    calendars_task = asyncio.create_task(call_google_calendar("list_calendars", {}))

    courses = await list_current_student_courses()

    async def with_assignments(course: dict[str, Any]) -> dict[str, Any]:
        # Courses already came from the student course list, so skip the
        # per-course /account re-validation (one throttled request each).
        return {**course, "assignments": await fetch_course_assignments(course["id"])}

    assignments_by_course = list(await asyncio.gather(*(with_assignments(course) for course in courses)))

    calendars = await calendars_task
    calendar_list = calendars.get("calendars") or []
    primary = pick_primary_calendar(calendar_list)
    if primary is None:
        raise GradescopeError("No Google Calendar found for the connected account")
    calendar_id = primary.get("id")
    time_zone = primary.get("timeZone")

    desired = plan_events(assignments_by_course, time_zone)
    now = datetime.now().astimezone()
    window_start = (now - timedelta(days=30)).isoformat()
    window_end = (now + timedelta(days=400)).isoformat()
    existing = await list_existing_gs_events(calendar_id, window_start, window_end, time_zone)
    existing_by_title = {e.get("summary"): e for e in existing if e.get("summary")}

    async def apply_event(event: dict[str, Any], match: dict[str, Any] | None) -> tuple[str, Any]:
        title = event["summary"]
        if match is None:
            result = await call_google_calendar(
                "create_event",
                {
                    "calendarId": calendar_id,
                    "summary": title,
                    "description": event["description"],
                    **event["times"],
                },
            )
            return (
                "created",
                {
                    "title": title,
                    "id": result.get("id") if isinstance(result, dict) else None,
                    "htmlLink": result.get("htmlLink") if isinstance(result, dict) else None,
                    "start": result.get("start") if isinstance(result, dict) else None,
                },
            )
        changed = existing_start_key(match) != event["start_key"] or (match.get("description") or "") != event["description"]
        if changed:
            await call_google_calendar(
                "update_event",
                {
                    "calendarId": calendar_id,
                    "eventId": match["id"],
                    "summary": title,
                    "description": event["description"],
                    **event["times"],
                },
            )
            return ("updated", title)
        return ("skipped", None)

    # Writes are independent per event; issue them concurrently and report
    # per-event failures without aborting the rest of the sync.
    outcomes = await asyncio.gather(
        *(apply_event(event, existing_by_title.get(event["summary"])) for event in desired),
        return_exceptions=True,
    )
    created: list[dict[str, Any]] = []
    updated: list[str] = []
    skipped = 0
    errors: list[str] = []
    for event, outcome in zip(desired, outcomes):
        if isinstance(outcome, BaseException):
            errors.append(f"{event['summary']}: {outcome}")
        elif outcome[0] == "created":
            created.append(outcome[1])
        elif outcome[0] == "updated":
            updated.append(outcome[1])
        else:
            skipped += 1

    return {
        "calendar": primary.get("summary") or calendar_id,
        "calendar_id": calendar_id,
        "calendars_seen": [
            {"id": c.get("id"), "summary": c.get("summary"), "primary": c.get("primary"), "accessRole": c.get("accessRole")}
            for c in calendar_list
        ],
        "existing_gs_events": sorted(existing_by_title.keys()),
        "courses_scanned": [c.get("short_name") or c.get("name") for c in assignments_by_course],
        "assignments_upcoming": sum(
            1 for c in assignments_by_course for a in c["assignments"] if a.get("deadline_status") in UPCOMING_STATUSES
        ),
        "events_created": created,
        "events_updated": updated,
        "events_skipped_unchanged": skipped,
        "errors": errors,
    }
