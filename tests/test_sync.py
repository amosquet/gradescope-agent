"""Tests for the one-shot sync planning logic."""

from __future__ import annotations

import asyncio

from sync import desired_start_key, event_time, existing_start_key, plan_events


def _course(course_id, short, name, assignments):
    return {"id": course_id, "short_name": short, "name": name, "assignments": assignments}


def _assignment(aid, name, due_iso, late_iso=None, status="before_due", released=True):
    return {
        "id": aid,
        "name": name,
        "due_date_iso": due_iso,
        "late_due_date_iso": late_iso,
        "deadline_status": status,
        "released": released,
        "url": f"/courses/1/assignments/{aid}",
        "submission_status": "unsubmitted",
    }


def test_plan_events_due_and_late():
    courses = [
        _course("1", "ECE 302", "ECE 30200", [
            _assignment("10", "HW 8", "2026-11-13T23:59:00-05:00", "2026-11-14T08:59:00-05:00"),
            _assignment("11", "HW 9", "2026-11-12T23:59:00-05:00"),
            _assignment("12", "Old HW", "2026-09-04T23:59:00-04:00", status="closed"),
            _assignment("13", "No date", None),
        ])
    ]
    events = plan_events(courses, "America/Indiana/Indianapolis")
    summaries = [e["summary"] for e in events]
    assert summaries == [
        "[GS] ECE 302 - HW 8 (due)",
        "[GS] ECE 302 - HW 8 (LATE deadline)",
        "[GS] ECE 302 - HW 9 (due)",
    ]
    due = events[0]
    assert due["times"]["startTime"] == "2026-11-13T23:59:00-05:00"
    assert due["times"]["timeZone"] == "America/Indiana/Indianapolis"
    assert due["times"]["endTime"] == "2026-11-14T00:59:00-05:00"
    assert "https://www.gradescope.com/courses/1/assignments/10" in due["description"]


def test_plan_events_skips_closed_and_same_late():
    courses = [
        _course("1", "CS 251", "CS 25100", [
            _assignment("20", "Closed", "2026-01-01T00:00:00-05:00", status="closed"),
            _assignment("21", "Same late", "2026-11-20T23:59:00-05:00", "2026-11-20T23:59:00-05:00"),
            _assignment("22", "Unreleased", "2026-11-21T23:59:00-05:00", released=False),
        ])
    ]
    events = plan_events(courses, None)
    assert [e["summary"] for e in events] == ["[GS] CS 251 - Same late (due)"]


def test_event_time_all_day_and_naive():
    # date-only -> all-day, midnight UTC (connector requirement for allDay)
    assert event_time("2026-11-13", "America/Indiana/Indianapolis") == {
        "allDay": True,
        "startTime": "2026-11-13T00:00:00Z",
        "endTime": "2026-11-14T00:00:00Z",
    }
    # naive timestamp -> flat strings + timeZone
    naive = event_time("2026-11-13T23:59:00", "America/Indiana/Indianapolis")
    assert naive == {
        "startTime": "2026-11-13T23:59:00",
        "endTime": "2026-11-14T00:59:00",
        "timeZone": "America/Indiana/Indianapolis",
    }
    # offset-aware timestamp
    offset = event_time("2026-11-13T23:59:00-05:00", None)
    assert offset == {"startTime": "2026-11-13T23:59:00-05:00", "endTime": "2026-11-14T00:59:00-05:00"}
    assert event_time(None, None) is None


def test_start_key_matches_google_event_shape():
    # naive planned time in the calendar tz must equal the stored offset form
    times = event_time("2026-11-13T23:59:00", "America/Indiana/Indianapolis")
    existing = {"start": {"dateTime": "2026-11-13T23:59:00-05:00", "timeZone": "America/Indiana/Indianapolis"}}
    assert desired_start_key(times, "America/Indiana/Indianapolis") == existing_start_key(existing)
    # all-day planned event must equal the stored date form
    allday = event_time("2026-11-13", None)
    assert desired_start_key(allday, None) == existing_start_key({"start": {"date": "2026-11-13"}}) == "2026-11-13"


def test_pick_primary_calendar():
    from sync import pick_primary_calendar

    calendars = [
        {"id": "3trlb35csvqrpd0og6rca5ibgo@group.calendar.google.com", "summary": "TrollClan Events"},
        {"id": "me@example.com", "summary": "me@example.com"},
        {"id": "en.french#holiday@group.v.calendar.google.com", "summary": "Holidays"},
    ]
    assert pick_primary_calendar(calendars)["id"] == "me@example.com"
    flagged = [{"id": "x@group.calendar.google.com", "primary": True}, {"id": "a@b.com"}]
    assert pick_primary_calendar(flagged)["id"] == "x@group.calendar.google.com"
    assert pick_primary_calendar([]) is None


def test_sync_sends_connector_shaped_create_args(monkeypatch):
    """Regression: the google_calendar connector's create_event/update_event take
    flat startTime/endTime strings, not nested start/end objects."""
    import sync

    courses = [{"id": "1", "short_name": "CS 251", "name": "CS 25100", "term": "Fall 2026", "role": "student", "url": "/courses/1"}]
    assignments = [
        _assignment("10", "HW 8", "2026-11-13T23:59:00-05:00", "2026-11-14T08:59:00-05:00"),
        _assignment("11", "HW 9", "2026-11-25"),
    ]

    async def fake_courses():
        return courses

    async def fake_assignments(course_id):
        assert course_id == "1"
        return assignments

    calls = []

    async def fake_gcal(tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        if tool_name == "list_calendars":
            return {"calendars": [{"id": "me@example.com", "summary": "me", "timeZone": "America/Indiana/Indianapolis", "primary": True}]}
        if tool_name == "list_events":
            return {"events": []}
        if tool_name == "create_event":
            return {"id": f"evt-{len(calls)}", "htmlLink": "https://calendar.google.com/x", "start": {"dateTime": arguments.get("startTime")}}
        raise AssertionError(f"unexpected tool {tool_name}")

    monkeypatch.setattr(sync, "list_current_student_courses", fake_courses)
    monkeypatch.setattr(sync, "fetch_course_assignments", fake_assignments)
    monkeypatch.setattr(sync, "call_google_calendar", fake_gcal)

    summary = asyncio.run(sync.sync_homework_to_calendar())

    creates = [args for name, args in calls if name == "create_event"]
    assert len(creates) == 3  # HW 8 due + LATE, HW 9 all-day due
    assert not summary["errors"]
    assert len(summary["events_created"]) == 3
    for args in creates:
        assert isinstance(args["startTime"], str) and isinstance(args["endTime"], str)
        assert "start" not in args and "end" not in args
        assert args["calendarId"] == "me@example.com"
    by_title = {args["summary"]: args for args in creates}
    assert by_title["[GS] CS 251 - HW 8 (due)"]["startTime"] == "2026-11-13T23:59:00-05:00"
    assert by_title["[GS] CS 251 - HW 8 (due)"]["timeZone"] == "America/Indiana/Indianapolis"
    allday = by_title["[GS] CS 251 - HW 9 (due)"]
    assert allday["allDay"] is True
    assert allday["startTime"] == "2026-11-25T00:00:00Z"
    assert allday["endTime"] == "2026-11-26T00:00:00Z"


def test_sync_updates_instead_of_recreating(monkeypatch):
    """An existing [GS] event with the same instant (offset form) is skipped;
    a changed one is updated with flat time args."""
    import sync

    courses = [{"id": "1", "short_name": "CS 251", "name": "CS 25100", "role": "student", "url": "/courses/1"}]
    assignments = [_assignment("10", "HW 8", "2026-11-13T23:59:00")]  # naive, calendar tz

    async def fake_courses():
        return courses

    async def fake_assignments(course_id):
        return assignments

    existing = {
        "id": "evt-existing",
        "summary": "[GS] CS 251 - HW 8 (due)",
        "description": "old",
        "start": {"dateTime": "2026-11-13T23:59:00-05:00", "timeZone": "America/Indiana/Indianapolis"},
    }
    calls = []

    async def fake_gcal(tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        if tool_name == "list_calendars":
            return {"calendars": [{"id": "me@example.com", "summary": "me", "timeZone": "America/Indiana/Indianapolis", "primary": True}]}
        if tool_name == "list_events":
            return {"events": [existing]}
        return {}

    monkeypatch.setattr(sync, "list_current_student_courses", fake_courses)
    monkeypatch.setattr(sync, "fetch_course_assignments", fake_assignments)
    monkeypatch.setattr(sync, "call_google_calendar", fake_gcal)

    summary = asyncio.run(sync.sync_homework_to_calendar())
    updates = [args for name, args in calls if name == "update_event"]
    creates = [args for name, args in calls if name == "create_event"]
    assert not creates
    assert len(updates) == 1  # description differs -> update, same instant
    assert updates[0]["eventId"] == "evt-existing"
    assert updates[0]["startTime"] == "2026-11-13T23:59:00"
    assert updates[0]["endTime"] == "2026-11-14T00:59:00"
    assert summary["events_updated"] == ["[GS] CS 251 - HW 8 (due)"]
