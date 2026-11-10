"""One-shot homework sync: Gradescope -> Google Calendar, idempotent.

Runs entirely inside one tool call so a sync costs one model round-trip plus
plain HTTP calls, instead of one model call per calendar event.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from gcal import call_google_calendar
from gradescope_client import (
    GradescopeError,
    list_course_assignments,
    list_current_student_courses,
)

EVENT_PREFIX = "[GS]"
UPCOMING_STATUSES = ("before_due", "accepting_late")


def event_title(short_name: str | None, course_name: str | None, assignment_name: str, kind: str) -> str:
    course = short_name or course_name or "Gradescope"
    return f"{EVENT_PREFIX} {course} - {assignment_name} ({kind})"


def event_description(course_name: str | None, assignment: dict[str, Any]) -> str:
    lines = [
        f"Course: {course_name or assignment.get('course_name') or ''}".rstrip(),
        f"Assignment: {assignment['name']}",
        f"Link: https://www.gradescope.com{assignment['url']}",
        f"Deadline status: {assignment.get('deadline_status')}",
        f"Submission: {assignment.get('submission_status')}",
    ]
    if assignment.get("points_awarded") is not None and assignment.get("points_possible") is not None:
        lines.append(f"Score: {assignment['points_awarded']} / {assignment['points_possible']}")
    if assignment.get("late_due_date"):
        lines.append(f"Late deadline: {assignment['late_due_date']}")
    return "\n".join(lines)


def event_time(iso: str | None, time_zone: str | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build (start, end) for a due timestamp; 1h duration, all-day if date-only."""
    if not iso:
        return None
    start: dict[str, Any] = {}
    if "T" in iso:
        if any(iso.endswith(suffix) for suffix in ("Z",)) or "+" in iso[10:] or "-" in iso[10:]:
            start["dateTime"] = iso
        else:
            start["dateTime"] = iso  # naive: interpreted in timeZone below
        if time_zone:
            start["timeZone"] = time_zone
        begin = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        end_dt = begin + timedelta(hours=1)
        end = dict(start)
        end["dateTime"] = end_dt.isoformat()
    else:
        start["date"] = iso
        end = {"date": (datetime.fromisoformat(iso) + timedelta(days=1)).date().isoformat()}
    return start, end


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
                start, end = times
                desired.append(
                    {
                        "summary": event_title(short, name, assignment["name"], kind),
                        "start": start,
                        "end": end,
                        "description": event_description(name, assignment),
                        "assignment_id": assignment["id"],
                    }
                )
    return desired


async def list_existing_gs_events(calendar_id: str | None, window_start: str, window_end: str, time_zone: str | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        page = await call_google_calendar(
            "list_events",
            {
                **({"calendarId": calendar_id} if calendar_id else {}),
                "startTime": window_start,
                "endTime": window_end,
                "fullText": EVENT_PREFIX,
                "pageSize": 250,
                **({"pageToken": page_token} if page_token else {}),
                **({"timeZone": time_zone} if time_zone else {}),
            },
        )
        events.extend(page.get("events") or [])
        page_token = page.get("nextPageToken")
        if not page_token:
            return events


async def sync_homework_to_calendar() -> dict[str, Any]:
    """Fetch upcoming Gradescope assignments (current-term courses) and sync [GS] events to the primary Google Calendar. Idempotent: matches by exact event title, updates only on change, never deletes."""
    calendars = await call_google_calendar("list_calendars", {})
    calendar_list = calendars.get("calendars") or []
    primary = next((c for c in calendar_list if c.get("primary")), calendar_list[0] if calendar_list else None)
    if primary is None:
        raise GradescopeError("No Google Calendar found for the connected account")
    calendar_id = primary.get("id")
    time_zone = primary.get("timeZone")

    courses = await list_current_student_courses()
    assignments_by_course = []
    for course in courses:
        assignments = await list_course_assignments(course["id"])
        assignments_by_course.append({**course, "assignments": assignments})

    desired = plan_events(assignments_by_course, time_zone)
    now = datetime.now().astimezone()
    window_start = (now - timedelta(days=30)).isoformat()
    window_end = (now + timedelta(days=400)).isoformat()
    existing = await list_existing_gs_events(calendar_id, window_start, window_end, time_zone)
    existing_by_title = {e.get("summary"): e for e in existing if e.get("summary")}

    created: list[str] = []
    updated: list[str] = []
    skipped = 0
    errors: list[str] = []
    for event in desired:
        title = event["summary"]
        match = existing_by_title.get(title)
        try:
            if match is None:
                await call_google_calendar(
                    "create_event",
                    {
                        "calendarId": calendar_id,
                        "summary": title,
                        "description": event["description"],
                        "start": event["start"],
                        "end": event["end"],
                    },
                )
                created.append(title)
            else:
                changed = (
                    (match.get("start") or {}).get("dateTime") != (event["start"].get("dateTime"))
                    or (match.get("start") or {}).get("date") != event["start"].get("date")
                    or (match.get("description") or "") != event["description"]
                )
                if changed:
                    await call_google_calendar(
                        "update_event",
                        {
                            "calendarId": calendar_id,
                            "eventId": match["id"],
                            "summary": title,
                            "description": event["description"],
                            "start": event["start"],
                            "end": event["end"],
                        },
                    )
                    updated.append(title)
                else:
                    skipped += 1
        except Exception as error:  # noqa: BLE001 - report per-event, keep syncing
            errors.append(f"{title}: {error}")

    return {
        "calendar": primary.get("summary") or calendar_id,
        "courses_scanned": [c.get("short_name") or c.get("name") for c in assignments_by_course],
        "assignments_upcoming": sum(
            1 for c in assignments_by_course for a in c["assignments"] if a.get("deadline_status") in UPCOMING_STATUSES
        ),
        "events_created": created,
        "events_updated": updated,
        "events_skipped_unchanged": skipped,
        "errors": errors,
    }
