"""Gradescope homework agent.

Reads the user's Gradescope account (read-only, same surface as
bruglet/gradescope-mcp) and syncs homework deadlines to Google Calendar
through the google_calendar connector. Deployed as a Mistral-managed
deployment; gradescope_email / gradescope_password come from workspace
secrets bound at deploy time.
"""

from __future__ import annotations

import os
from typing import Annotated

from mistralai import agents
from pydantic import Field

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

Calendar tools come from the google_calendar connector (connector_google_calendar.*): list_calendars, list_events, get_event, create_event, update_event, delete_event.

MAIN TASK — "sync homework to calendar" (run when the user asks to sync, or on a scheduled run):
1. Call list_calendars and use the primary calendar unless the user names another; note its timeZone.
2. Call list_assignments with no course_id to get every assignment in every student course (due dates, late due dates, deadline status, submission status, scores, URLs).
3. For each released assignment with a due date, ensure a calendar event exists:
   - Title: "[GS] {course short name} - {assignment name} (due)"
   - Start: the regular due date/time expressed in the calendar's timeZone (use due_date_iso when it carries an offset; if it is naive, treat it as calendar-local). Duration 1 hour. If the due date has no time component, create an all-day event on that date instead.
   - Description: the assignment URL, course name, deadline_status, submission_status, score if any, and the late due date if it differs from the due date.
   - If a late due date exists and differs from the due date, also ensure a second event titled "[GS] {course short name} - {assignment name} (LATE deadline)" at the late due time.
4. Idempotency: before creating anything, call list_events on the target calendar with fullText "[GS]" and a startTime/endTime window wide enough to cover all assignment due dates (paginate with pageToken if needed). Match existing events by exact title. If an event with the same title exists, call update_event only when the start time or description actually changed; otherwise skip. Never delete events unless the user explicitly asks.
5. Assignments without a due date are skipped. End with a concise summary: courses scanned, assignments found, events created / updated / skipped, and any errors encountered.

When the user asks about courses, deadlines, submissions, scores, or regrade requests, answer from the Gradescope tools and cite assignment URLs. Keep answers short. Never invent data: if a tool returns null or "unknown", say so.
"""


async def _run(operation):
    try:
        return await operation
    except GradescopeError as error:
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


google_calendar = agents.connector("google_calendar", run_as="deployment", credentials_name="homework-sync")

agent = agents.Agent(
    model="mistral-large-4",
    name="gradescope-homework-sync",
    description="Reads Gradescope homework assignments (read-only) and syncs deadlines to Google Calendar.",
    instructions=INSTRUCTIONS,
    tools=[list_courses, list_assignments, list_submissions, get_submission, list_regrade_requests],
    connectors=[google_calendar],
    secrets={
        "gradescope_email": agents.MistralSecret.workspace("gradescope_email", worker_env=True),
        "gradescope_password": agents.MistralSecret.workspace("gradescope_password", worker_env=True),
    },
)


if __name__ == "__main__":
    print(f"agent: {agent.name} (model={agent.model})")
    print(f"GRADESCOPE_EMAIL set: {bool(os.environ.get('GRADESCOPE_EMAIL'))}")
