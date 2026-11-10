"""Parser tests against the HTML fixtures copied from bruglet/gradescope-mcp."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from gradescope_assignments import parse_assignment_list
from gradescope_courses import parse_dashboard
from gradescope_detail import parse_submission_detail
from gradescope_parsers import deadline_to_iso
from gradescope_submissions import parse_regrade_requests, parse_submission_list

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_dashboard_roles_and_terms():
    courses = parse_dashboard(fixture("account.html"))
    assert [(c["id"], c["role"], c["term"]) for c in courses] == [
        ("101", "student", "Fall 2026"),
        ("102", "student", "Spring 2026"),
        ("999", "instructor", "Fall 2026"),
    ]


def test_dashboard_your_courses_fallback():
    courses = parse_dashboard(fixture("account-your-courses.html"))
    assert [(c["id"], c["role"], c["term"]) for c in courses] == [
        ("401", "student", "Fall 2026"),
        ("402", "student", "Spring 2026"),
    ]


def test_assignments_full_fields():
    now = int(datetime.fromisoformat("2026-08-19T00:00:00+00:00").timestamp() * 1000)
    assignments = parse_assignment_list(fixture("assignments.html"), now_ms=now)
    assert len(assignments) == 4
    first = assignments[0]
    assert first["type"] == "unknown"
    assert first["id"] == "201"
    assert first["name"] == "Project 1"
    assert first["due_date"] == "2026-08-20T23:59:00-07:00"
    assert first["due_date_iso"] == "2026-08-20T23:59:00-07:00"
    assert first["late_due_date"] == "2026-08-22T23:59:00-07:00"
    assert first["deadline_status"] == "before_due"
    assert first["released"] is True
    assert first["submission_status"] == "submitted"
    assert first["status_raw"] == "Submitted"
    assert first["submitted"] is True
    assert first["submitted_at"] == "2026-08-20T20:10:00-07:00"
    assert first["late"] is None
    assert first["points_possible"] == 100
    assert first["points_awarded"] == 95
    assert first["url"] == "/courses/101/assignments/201"

    assert assignments[1]["submission_status"] == "unsubmitted"
    assert assignments[1]["submitted"] is False
    assert assignments[1]["due_date"] == "Aug 25, 2026 at 11:59 PM"
    assert assignments[1]["late_due_date"] == "Aug 27, 2026 at 11:59 PM"
    assert assignments[1]["due_date_iso"] == "2026-08-25T23:59:00"

    assert assignments[2]["submission_status"] == "submitted"
    assert assignments[2]["late"] is True
    assert assignments[2]["lateness"] == "Late"
    assert assignments[3]["submission_status"] == "unknown"
    assert assignments[3]["submitted"] is None


def test_assignments_unlinked_table():
    assignments = parse_assignment_list(fixture("assignments-unlinked-table.html"), "101")
    assert [
        (a["id"], a["name"], a["due_date"], a["late_due_date"], a["submission_status"], a["submitted"], a["url"])
        for a in assignments
    ] == [
        ("701", "Homework 1", "2026-09-02T10:00:00-07:00", "2026-09-04T10:00:00-07:00", "unsubmitted", False, "/courses/101/assignments/701"),
        ("702", "Lab 2", "Sep 8 at 11:59PM", None, "submitted", True, "/courses/101/assignments/702"),
        ("703", "Worksheet 3", "Sep 10 at 11:59PM", None, "unsubmitted", False, "/courses/101/assignments/703"),
    ]


def test_assignments_active_late_deadline_and_hidden_columns():
    page = fixture("assignments-active-late-deadline.html")
    now = int(datetime.fromisoformat("2026-10-23T12:00:00-07:00").timestamp() * 1000)
    result = parse_assignment_list(page, "101", now)
    assert [(a["id"], a["due_date"], a["late_due_date"], a["deadline_status"]) for a in result] == [
        ("901", "2026-10-22 21:00:00 -0700", "2026-12-07 21:00:00 -0800", "accepting_late"),
        ("902", "2026-10-26 09:00:00 -0700", "2026-12-07 21:00:00 -0800", "before_due"),
        ("903", None, "2026-12-07 21:00:00 -0800", "unknown"),
    ]
    assert result[0]["due_date_iso"] == "2026-10-22T21:00:00-07:00"
    for marker in ("hidden", 'aria-hidden="true"'):
        hidden_page = page.replace('class="hidden-column"', marker)
        assert parse_assignment_list(hidden_page, "101", now) == result


def test_assignment_deadline_statuses():
    now = int(datetime.fromisoformat("2026-09-03T12:00:00-07:00").timestamp() * 1000)
    assignments = parse_assignment_list(fixture("assignment-deadlines.html"), "101", now)
    assert [(a["id"], a["due_date"], a["late_due_date"], a["deadline_status"]) for a in assignments] == [
        ("801", "2026-09-02T10:00:00-07:00", "2026-09-04T10:00:00-07:00", "accepting_late"),
        ("802", "2026-09-05T10:00:00-07:00", None, "before_due"),
        ("803", "2026-09-02 10:00:00 -0700", "2026-09-03 11:00:00 -0700", "closed"),
        ("804", None, "2026-09-04T10:00:00-07:00", "unknown"),
        ("805", "2026-09-02T10:00:00-07:00", "2026-09-02T10:00:00-07:00", "closed"),
        ("806", "TBD", None, "accepting_late"),
        ("807", "2026-09-05T10:00:00-07:00", None, "closed"),
        ("808", "Sep 5", None, "unknown"),
        ("809", "09/05/2026", None, "unknown"),
        ("810", None, "2026-09-06T10:00:00-07:00", "unknown"),
        ("813", None, None, "unknown"),
    ]
    assert assignments[0]["submission_status"] == "submitted"
    assert assignments[0]["late"] is True
    later = parse_assignment_list(fixture("assignment-deadlines-no-status.html"), "101", now)
    assert later[0]["deadline_status"] == "unknown"


def test_assignments_score_only_is_graded():
    assignments = parse_assignment_list(fixture("assignments-score-only.html"))
    first = assignments[0]
    assert first["submission_status"] == "graded"
    assert first["status_raw"] == "86.0 / 100.0"
    assert first["submitted"] is True
    assert first["points_awarded"] == 86
    assert first["points_possible"] == 100


def test_submission_list():
    submissions = parse_submission_list(fixture("submissions.html"))
    assert len(submissions) == 2
    assert [
        (s["id"], s["submission_status"], s["submitted_at"], s["late"], s["lateness"], s["score"], s["max_score"])
        for s in submissions
    ] == [
        ("301", "graded", "2026-08-20T20:10:00-07:00", False, "On time", 95, 100),
        ("302", "submitted", "2026-08-22T00:05:00-07:00", True, "5 minutes late", 98, 100),
    ]


def test_submission_score_only():
    submissions = parse_submission_list(fixture("submissions-score-only.html"))
    assert submissions[0] == {
        "id": "305",
        "score": 86,
        "max_score": 100,
        "submission_status": "graded",
        "status_raw": "86.0 / 100.0",
        "submitted": True,
        "submitted_at": "2026-08-30T12:20:00-07:00",
        "late": None,
        "lateness": None,
        "url": "/courses/101/assignments/205/submissions/305",
    }


def test_course_submission_links_filtered():
    submissions = parse_submission_list(fixture("course-submission-links.html"), "101", "201")
    assert [(s["id"], s["url"]) for s in submissions] == [
        ("301", "/courses/101/assignments/201/submissions/301"),
        ("302", "/courses/101/assignments/201/submissions/302"),
    ]
    assert parse_submission_list(fixture("course-submission-links.html"), "101", "999") == []


def test_submission_detail_html():
    detail = parse_submission_detail(fixture("submission-detail.html"))
    assert detail["score"] == 95
    assert detail["max_score"] == 100
    assert detail["submission_status"] == "graded"
    assert detail["submitted_at"] == "2026-08-20T20:10:00-07:00"
    assert len(detail["questions"]) == 2
    assert detail["questions"][0]["rubric_items"][0]["applied"] is True
    assert detail["questions"][0]["comments"][0] == "Clear explanation."


def test_submission_detail_live():
    detail = parse_submission_detail(fixture("submission-detail-live.html"))
    assert detail["score"] == 86
    assert detail["max_score"] == 100
    assert detail["submission_status"] == "graded"
    assert detail["submitted"] is True
    assert detail["questions"][0]["name"] == "Question 1"
    assert detail["questions"][0]["score"] == 12
    assert detail["questions"][0]["max_score"] == 20


def test_regrade_requests():
    requests = parse_regrade_requests(fixture("regrades.html"))
    assert len(requests) >= 1
    assert all("status" in r and "explanation" in r for r in requests)


def test_deadline_to_iso():
    assert deadline_to_iso("2026-08-20T23:59:00-07:00") == "2026-08-20T23:59:00-07:00"
    assert deadline_to_iso("2026-10-22 21:00:00 -0700") == "2026-10-22T21:00:00-07:00"
    assert deadline_to_iso("2026-08-20T20:10:00Z") == "2026-08-20T20:10:00+00:00"
    assert deadline_to_iso("Aug 25, 2026 at 11:59 PM") == "2026-08-25T23:59:00"
    assert deadline_to_iso("TBD") is None
    assert deadline_to_iso(None) is None


def test_select_current_courses():
    from datetime import datetime

    from gradescope_courses import select_current_courses

    courses = [
        {"id": "1", "term": "Fall 2023"},
        {"id": "2", "term": "Spring 2026"},
        {"id": "3", "term": "Fall 2026"},
        {"id": "4", "term": "Fall 2026"},
        {"id": "5", "term": None},
    ]
    now = datetime(2026, 11, 10)  # Fall 2026
    assert [c["id"] for c in select_current_courses(courses, now)] == ["3", "4"]
    # Fallback to latest term when the current term has no courses
    now2 = datetime(2027, 2, 1)  # Spring 2027, not present
    assert [c["id"] for c in select_current_courses(courses, now2)] == ["3", "4"]
