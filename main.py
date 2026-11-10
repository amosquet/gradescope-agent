"""Gradescope homework agent.

Reads the user's Gradescope account (read-only, same surface as
bruglet/gradescope-mcp) and syncs homework deadlines to Google Calendar
through the google_calendar connector. Deployed as a Mistral-managed
deployment; gradescope_email / gradescope_password / mistral_calendar_api_key
come from workspace secrets bound at deploy time. Calendar writes go through
the connectors API with the user's API key (secret mistral_calendar_api_key),
pinned to the user's homework-sync google_calendar credential, because the
deployment's service account cannot use user-scope OAuth credentials.
"""

from __future__ import annotations

import os
from typing import Annotated

from mistralai import agents
from pydantic import Field

from gcal import (
    CalendarError,
)
from gcal import (
    calendar_create_event as _calendar_create_event,
    calendar_get_event as _calendar_get_event,
    calendar_list_calendars as _calendar_list_calendars,
    calendar_list_events as _calendar_list_events,
    calendar_update_event as _calendar_update_event,
)
from gradescope_client import (
    GradescopeError,
    get_submission_detail,
    list_assignment_regrade_requests,
    list_assignment_submissions,
    list_course_assignments,
    list_student_courses,
)

INSTRUCTIONS = """You are a homework assistant. You read the user's Gradescope account (read-only) and sync homework assignments to their Google Calendar.

Gradescope tools (read-only, called as client.*): list_courses, list_assignments, list_submissions, get_submission, list_regrade_requests. Credentials come from the gradescope_email / gradescope_password environment variables (managed secrets); never ask the user for them and never print them. Never modify anything in Gradescope.

Calendar tools (called as client.calendar_*): calendar_list_calendars, calendar_list_events, calendar_get_event, calendar_create_event, calendar_update_event. They write to the primary Google Calendar unless a calendar_id is given. Time fields use {\"dateTime\": ISO 8601 with offset, \"timeZone\": IANA name} or {\"date\": \"YYYY-MM-DD\"} for all-day events.

MAIN TASK — "sync homework to calendar" (run when the user asks to sync, or on a scheduled run):
1. Call calendar_list_calendars and use the primary calendar unless the user names another; note its timeZone.
2. Call list_assignments with no course_id to get every assignment in every student course (due dates, late due dates, deadline status, submission status, scores, URLs).
3. For each released assignment with a due date, ensure a calendar event exists:
   - Title: "[GS] {course short name} - {assignment name} (due)"
   - Start: the regular due date/time expressed in the calendar's timeZone (use due_date_iso when it carries an offset; if it is naive, treat it as calendar-local). Duration 1 hour. If the due date has no time component, create an all-day event on that date instead.
   - Description: the assignment URL, course name, deadline_status, submission_status, score if any, and the late due date if it differs from the due date.
   - If a late due date exists and differs from the due date, also ensure a second event titled "[GS] {course short name} - {assignment name} (LATE deadline)" at the late due time.
4. Idempotency: before creating anything, call calendar_list_events on the target calendar with full_text "[GS]" and a start_time/end_time window wide enough to cover all assignment due dates (paginate with page_token if needed). Match existing events by exact summary (title). If an event with the same title exists, call calendar_update_event only when the start time or description actually changed; otherwise skip. Never delete events unless the user explicitly asks.
5. Assignments without a due date are skipped. End with a concise summary: courses scanned, assignments found, events created / updated / skipped, and any errors encountered.

When the user asks about courses, deadlines, submissions, scores, or regrade requests, answer from the Gradescope tools and cite assignment URLs. Keep answers short. Never invent data: if a tool returns null or "unknown", say so.
"""


async def _run(operation):
    try:
        return await operation
    except GradescopeError as error:
        raise agents.ToolError(str(error)) from error
    except CalendarError as error:
        raise agents.ToolError(str(error)) from error


@agents.tool
async def list_courses() -> dict:
    """List the user's Gradescope courses (student role only): course IDs, names, short names, terms, and URLs. Call this first to get course_id values."""
    courses = await _run(list_student_courses())
    return {
        "courses": [
            {
                "course_id": c["id"],
                "course_name": c["name"],
                "short_name": c["short_name"],
                "term": c["term"],
                "url": c["url"],
            }
            for c in courses
        ]
    }


@agents.tool
async def list_assignments(
    course_id: Annotated[
        str | None,
        Field(description="Numeric Gradescope course ID from list_courses. Omit to list assignments for ALL courses at once."),
    ] = None,
) -> dict:
    """List Gradescope assignments with regular and late deadlines (raw and ISO), deadline status, submission status, timestamps, lateness, scores, release state, and URLs. Omit course_id to scan every course; pass one to scan a single course."""
    if course_id:
        courses = [{"id": course_id, "short_name": None, "name": None}]
    else:
        courses = await _run(list_student_courses())
    result = []
    for course in courses:
        assignments = await _run(list_course_assignments(course["id"]))
        result.append({"course_id": course["id"], "short_name": course.get("short_name"), "assignments": assignments})
    return {"courses": result}


@agents.tool
async def list_submissions(
    course_id: Annotated[str, Field(description="Numeric course ID from list_courses.")],
    assignment_id: Annotated[str, Field(description="Numeric assignment ID from list_assignments.")],
) -> dict:
    """List the user's submission attempts for one assignment: submission IDs, timestamps, status, lateness, scores, and URLs."""
    submissions = await _run(list_assignment_submissions(course_id, assignment_id))
    return {"course_id": course_id, "assignment_id": assignment_id, "submissions": submissions}


@agents.tool
async def get_submission(
    course_id: Annotated[str, Field(description="Numeric course ID from list_courses.")],
    assignment_id: Annotated[str, Field(description="Numeric assignment ID from list_assignments.")],
    submission_id: Annotated[str, Field(description="Numeric submission ID from list_submissions.")],
) -> dict:
    """Get one submission with summary fields, per-question scores, rubric items, score adjustments, and grader comments."""
    submission = await _run(get_submission_detail(course_id, assignment_id, submission_id))
    return {"course_id": course_id, "assignment_id": assignment_id, "submission": submission}


@agents.tool
async def list_regrade_requests(
    course_id: Annotated[str, Field(description="Numeric course ID from list_courses.")],
    assignment_id: Annotated[str, Field(description="Numeric assignment ID from list_assignments.")],
) -> dict:
    """List the user's regrade requests for one assignment: question, status, explanation, response, request time, and URL."""
    requests = await _run(list_assignment_regrade_requests(course_id, assignment_id))
    return {"course_id": course_id, "assignment_id": assignment_id, "regrade_requests": requests}


@agents.tool
async def calendar_list_calendars() -> dict:
    """List the Google calendars available to the connected account, with each calendar's id, summary, and timeZone. Call this first to resolve the target calendar and its time zone."""
    return await _run(_calendar_list_calendars())


@agents.tool
async def calendar_list_events(
    calendar_id: Annotated[str | None, Field(description="Exact calendar ID from calendar_list_calendars. Omit for the primary calendar.")] = None,
    start_time: Annotated[str | None, Field(description="ISO 8601 lower bound (exclusive) for an event's end time, e.g. 2026-11-01T00:00:00-05:00.")] = None,
    end_time: Annotated[str | None, Field(description="ISO 8601 upper bound (exclusive) for an event's start time.")] = None,
    full_text: Annotated[str | None, Field(description="Free-form search across event title, description, location, and attendees, e.g. \"[GS]\".")] = None,
    time_zone: Annotated[str | None, Field(description="IANA time zone used in the response, e.g. America/Indiana/Indianapolis.")] = None,
    page_token: Annotated[str | None, Field(description="nextPageToken from a previous calendar_list_events call.")] = None,
) -> dict:
    """List Google Calendar events in an optional time window, optionally filtered by full-text query. Returns events and nextPageToken when more pages exist."""
    return await _run(_calendar_list_events(calendar_id=calendar_id, start_time=start_time, end_time=end_time, full_text=full_text, time_zone=time_zone, page_token=page_token))


@agents.tool
async def calendar_get_event(
    event_id: Annotated[str, Field(description="Exact event id returned by calendar_list_events or calendar_create_event.")],
    calendar_id: Annotated[str | None, Field(description="Calendar ID that located the event. Omit for the primary calendar.")] = None,
) -> dict:
    """Get one Google Calendar event by id."""
    return await _run(_calendar_get_event(event_id=event_id, calendar_id=calendar_id))


@agents.tool
async def calendar_create_event(
    summary: Annotated[str, Field(description="Event title, e.g. \"[GS] CS 251 - HW5 (due)\".")],
    start: Annotated[dict, Field(description="Start: {\"dateTime\": ISO 8601 with offset, \"timeZone\": IANA name} or {\"date\": \"YYYY-MM-DD\"} for all-day.")],
    end: Annotated[dict, Field(description="End, same shape as start (exclusive).")],
    description: Annotated[str | None, Field(description="Event description; include the Gradescope assignment URL.")] = None,
    calendar_id: Annotated[str | None, Field(description="Exact calendar ID. Omit for the primary calendar.")] = None,
) -> dict:
    """Create a Google Calendar event on the primary calendar (or the given calendar_id)."""
    return await _run(_calendar_create_event(summary=summary, start=start, end=end, description=description, calendar_id=calendar_id))


@agents.tool
async def calendar_update_event(
    event_id: Annotated[str, Field(description="Exact event id to update.")],
    summary: Annotated[str | None, Field(description="New title. Only provided fields are changed.")] = None,
    description: Annotated[str | None, Field(description="New description.")] = None,
    start: Annotated[dict | None, Field(description="New start: {\"dateTime\": ..., \"timeZone\": ...} or {\"date\": ...}.")] = None,
    end: Annotated[dict | None, Field(description="New end, same shape as start.")] = None,
    calendar_id: Annotated[str | None, Field(description="Calendar ID that located the event. Omit for the primary calendar.")] = None,
) -> dict:
    """Update an existing Google Calendar event. Only provided fields are changed."""
    return await _run(_calendar_update_event(event_id=event_id, summary=summary, description=description, start=start, end=end, calendar_id=calendar_id))


agent = agents.Agent(
    model="mistral-large-4",
    name="gradescope-homework-sync",
    description="Reads Gradescope homework assignments (read-only) and syncs deadlines to Google Calendar.",
    instructions=INSTRUCTIONS,
    tools=[
        list_courses,
        list_assignments,
        list_submissions,
        get_submission,
        list_regrade_requests,
        calendar_list_calendars,
        calendar_list_events,
        calendar_get_event,
        calendar_create_event,
        calendar_update_event,
    ],
    secrets={
        "gradescope_email": agents.MistralSecret.workspace("gradescope_email", worker_env=True),
        "gradescope_password": agents.MistralSecret.workspace("gradescope_password", worker_env=True),
        "mistral_calendar_api_key": agents.MistralSecret.workspace("mistral_calendar_api_key", worker_env=True),
    },
)


if __name__ == "__main__":
    print(f"agent: {agent.name} (model={agent.model})")
    print(f"GRADESCOPE_EMAIL set: {bool(os.environ.get('GRADESCOPE_EMAIL'))}")
