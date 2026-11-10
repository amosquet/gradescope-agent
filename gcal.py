"""Google Calendar access for the agent.

The managed worker runs as the deployment's service account, which cannot use
user-scope OAuth connector credentials. Instead, the worker holds a workspace
secret containing a Mistral API key of the connector's owner and calls the
connectors API (`POST /v1/connectors/google_calendar/tools/<tool>/call`) as
that user, pinned to the `homework-sync` credential. The key never enters the
session or the logs; it is read from the environment at call time.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from connectors_api import ConnectorError, call_connector_tool

GOOGLE_CALENDAR = "google_calendar"
CREDENTIALS_NAME = "homework-sync"


class CalendarError(Exception):
    pass


async def call_google_calendar(tool_name: str, arguments: dict[str, Any]) -> Any:
    try:
        return await call_connector_tool(GOOGLE_CALENDAR, tool_name, arguments, CREDENTIALS_NAME)
    except ConnectorError as error:
        raise CalendarError(str(error)) from error


async def calendar_list_calendars() -> dict[str, Any]:
    """List the Google calendars available to the connected account, with each calendar's id, summary, and timeZone."""
    return await call_google_calendar("list_calendars", {})


async def calendar_list_events(
    calendar_id: Annotated[str | None, Field(description="Exact calendar ID from calendar_list_calendars. Omit for the primary calendar.")] = None,
    start_time: Annotated[str | None, Field(description="ISO 8601 lower bound (exclusive) for an event's end time, e.g. 2026-11-01T00:00:00-05:00.")] = None,
    end_time: Annotated[str | None, Field(description="ISO 8601 upper bound (exclusive) for an event's start time.")] = None,
    full_text: Annotated[str | None, Field(description="Free-form search across event title, description, location, and attendees, e.g. \"[GS]\".")] = None,
    time_zone: Annotated[str | None, Field(description="IANA time zone used in the response, e.g. America/Indiana/Indianapolis.")] = None,
    page_size: Annotated[int | None, Field(description="Max events per page, 1-250 (default 100).")] = None,
    page_token: Annotated[str | None, Field(description="nextPageToken from a previous list_events call, to continue pagination.")] = None,
) -> dict[str, Any]:
    """List events in the Google Calendar within an optional time window, optionally filtered by full-text query. Returns the events array and nextPageToken when more pages exist."""
    arguments: dict[str, Any] = {}
    if calendar_id:
        arguments["calendarId"] = calendar_id
    if start_time:
        arguments["startTime"] = start_time
    if end_time:
        arguments["endTime"] = end_time
    if full_text:
        arguments["fullText"] = full_text
    if time_zone:
        arguments["timeZone"] = time_zone
    if page_size:
        arguments["pageSize"] = page_size
    if page_token:
        arguments["pageToken"] = page_token
    return await call_google_calendar("list_events", arguments)


async def calendar_get_event(
    event_id: Annotated[str, Field(description="Exact event id returned by list_events or create_event.")],
    calendar_id: Annotated[str | None, Field(description="Calendar ID that located the event. Omit for the primary calendar.")] = None,
) -> dict[str, Any]:
    """Get one Google Calendar event by id."""
    arguments: dict[str, Any] = {"eventId": event_id}
    if calendar_id:
        arguments["calendarId"] = calendar_id
    return await call_google_calendar("get_event", arguments)


async def calendar_create_event(
    summary: Annotated[str, Field(description="Event title, e.g. \"[GS] CS 251 - HW5 (due)\".")],
    start_time: Annotated[str, Field(description="Start as an ISO 8601 string, e.g. 2026-11-13T23:59:00-05:00 (offset optional when time_zone is set). For all-day events pass midnight UTC, e.g. 2026-11-20T00:00:00Z.")],
    end_time: Annotated[str, Field(description="End (exclusive) as an ISO 8601 string, same rules as start_time.")],
    description: Annotated[str | None, Field(description="Event description; include the Gradescope assignment URL.")] = None,
    calendar_id: Annotated[str | None, Field(description="Exact calendar ID. Omit for the primary calendar.")] = None,
    time_zone: Annotated[str | None, Field(description="IANA time zone, e.g. America/Indiana/Indianapolis; also resolves timezone-less start/end times.")] = None,
    all_day: Annotated[bool, Field(description="All-day event; then start_time/end_time must be midnight UTC.")] = False,
) -> dict[str, Any]:
    """Create a Google Calendar event."""
    arguments: dict[str, Any] = {"summary": summary, "startTime": start_time, "endTime": end_time}
    if description:
        arguments["description"] = description
    if calendar_id:
        arguments["calendarId"] = calendar_id
    if time_zone:
        arguments["timeZone"] = time_zone
    if all_day:
        arguments["allDay"] = True
    return await call_google_calendar("create_event", arguments)


async def calendar_update_event(
    event_id: Annotated[str, Field(description="Exact event id to update.")],
    summary: Annotated[str | None, Field(description="New title. Only fields provided are changed.")] = None,
    description: Annotated[str | None, Field(description="New description.")] = None,
    start_time: Annotated[str | None, Field(description="New start as an ISO 8601 string, e.g. 2026-11-13T23:59:00-05:00.")] = None,
    end_time: Annotated[str | None, Field(description="New end (exclusive) as an ISO 8601 string. If omitted, the end moves with the start so the duration is kept.")] = None,
    time_zone: Annotated[str | None, Field(description="IANA time zone for the new start/end.")] = None,
    all_day: Annotated[bool | None, Field(description="Set to true to make the event all-day (start_time/end_time must then be midnight UTC).")] = None,
    calendar_id: Annotated[str | None, Field(description="Calendar ID that located the event. Omit for the primary calendar.")] = None,
) -> dict[str, Any]:
    """Update an existing Google Calendar event. Only provided fields are changed."""
    arguments: dict[str, Any] = {"eventId": event_id}
    if summary:
        arguments["summary"] = summary
    if description is not None:
        arguments["description"] = description
    if start_time:
        arguments["startTime"] = start_time
    if end_time:
        arguments["endTime"] = end_time
    if time_zone:
        arguments["timeZone"] = time_zone
    if all_day is not None:
        arguments["allDay"] = all_day
    if calendar_id:
        arguments["calendarId"] = calendar_id
    return await call_google_calendar("update_event", arguments)
