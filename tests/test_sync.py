"""Tests for the one-shot sync planning logic."""

from __future__ import annotations

from sync import event_time, plan_events


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
    assert due["start"]["dateTime"] == "2026-11-13T23:59:00-05:00"
    assert due["start"]["timeZone"] == "America/Indiana/Indianapolis"
    assert due["end"]["dateTime"] == "2026-11-14T00:59:00-05:00"
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
    start, end = event_time("2026-11-13", "America/Indiana/Indianapolis")
    assert start == {"date": "2026-11-13"}
    assert end == {"date": "2026-11-14"}
    start, end = event_time("2026-11-13T23:59:00", "America/Indiana/Indianapolis")
    assert start["dateTime"] == "2026-11-13T23:59:00"
    assert start["timeZone"] == "America/Indiana/Indianapolis"
    assert end["dateTime"] == "2026-11-14T00:59:00"
    assert event_time(None, None) is None


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
