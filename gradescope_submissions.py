"""Submission list and regrade request parsing, ported from bruglet/gradescope-mcp."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from gradescope_parsers import (
    BASE_ORIGIN,
    first_matching_cell,
    header_names,
    late_from_text,
    nullable_text,
    parse_score,
    status_from_raw,
    status_with_score,
    submitted_from_status,
    text_content,
    value_content,
)


def submission_container(link: Tag) -> Tag:
    for finder in (
        lambda: link.find_parent("tr"),
        lambda: link.find_parent(".submission-row"),
        lambda: link.find_parent(attrs={"class": re.compile("submission-card")}),
        lambda: link.find_parent(attrs={"class": re.compile("submission")}),
    ):
        parent = finder()
        if parent is not None:
            return parent
    return link


def submission_from_container(container: Tag, submission_id: str, url: str) -> dict[str, Any]:
    cells = container.find_all(["td", "th"])
    headers = header_names(container)
    container_text = text_content(container)
    status_cell = first_matching_cell(cells, headers, re.compile(r"(^| )status($| )"), re.compile(r"status|submission"))
    score_cell = first_matching_cell(cells, headers, re.compile(r"score|grade|points"), re.compile(r"score|grade|points"))
    submitted_at_cell = first_matching_cell(
        cells,
        headers,
        re.compile(r"submitted at|submission time|timestamp|submitted"),
        re.compile(r"submitted|timestamp|time|date"),
    )
    late_cell = first_matching_cell(cells, headers, re.compile(r"late|lateness"), re.compile(r"late|lateness"))

    status_raw = nullable_text(value_content(status_cell))
    score_text = value_content(score_cell) or next(
        (text_content(cell) for cell in cells if parse_score(text_content(cell))), None
    )
    score = parse_score(score_text) if score_text else None
    status = status_with_score(status_from_raw(status_raw), score)
    late_text = value_content(late_cell) or (
        status_raw if status_raw and re.search(r"\blate\b", status_raw, re.I) else None
    )
    late = late_from_text([late_text, status_raw])
    submitted_at = value_content(submitted_at_cell) or value_content(container.select_one("time[datetime]"))

    return {
        "id": submission_id,
        "score": score[0] if score else None,
        "max_score": score[1] if score else None,
        "submission_status": status,
        "status_raw": status_raw
        or (container_text if re.search(r"submitted|graded|missing|late|not submitted", container_text, re.I) else None),
        "submitted": submitted_from_status(status),
        "submitted_at": submitted_at,
        "late": late,
        "lateness": late_text,
        "url": url,
    }


def submission_path_from_value(
    value: str | None, course_id: str | None = None, assignment_id: str | None = None
) -> str | None:
    if not value:
        return None
    try:
        url = urljoin("https://www.gradescope.com/", value)
        if f"{urlparse(url).scheme}://{urlparse(url).netloc}" != BASE_ORIGIN:
            return None
        match = re.match(r"^/courses/(\d+)/assignments/(\d+)/submissions/(\d+)/?$", urlparse(url).path)
        if not match:
            return None
        if course_id and match.group(1) != course_id:
            return None
        if assignment_id and match.group(2) != assignment_id:
            return None
        return f"/courses/{match.group(1)}/assignments/{match.group(2)}/submissions/{match.group(3)}"
    except ValueError:
        return None


def parse_submission_list(
    html: str, course_id: str | None = None, assignment_id: str | None = None
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    submissions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for link in soup.select('a[href*="/submissions/"]'):
        href = submission_path_from_value(link.get("href"), course_id, assignment_id)
        id_match = re.search(r"/submissions/(\d+)$", href) if href else None
        if not href or not id_match:
            continue
        submission_id = id_match.group(1)
        if submission_id in seen_ids:
            continue
        seen_ids.add(submission_id)
        submissions.append(submission_from_container(submission_container(link), submission_id, href))
    return submissions


def regrade_status_from_raw(raw: str | None) -> str:
    if not raw:
        return "unknown"
    value = raw.lower()
    if re.search(r"pending|open|awaiting", value):
        return "pending"
    if re.search(r"approved|accepted|granted", value):
        return "approved"
    if re.search(r"denied|rejected|declined", value):
        return "denied"
    if re.search(r"resolved|closed|complete|completed", value):
        return "resolved"
    return "unknown"


def parse_regrade_requests(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    requests: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in soup.select("tr, .regradeRequest, [class*='regradeRequest'], [data-regrade-request]"):
        row_text = text_content(candidate)
        link = candidate.select_one('a[href*="regrade"]')
        href = link.get("href") if link else None
        id_match = re.search(r"regrade_requests/(\d+)", href) if href else None
        if not id_match and href:
            id_match = re.search(r"/(\d+)$", href)
        request_id = id_match.group(1) if id_match else None
        if not request_id and not re.search(r"regrade", row_text, re.I):
            continue
        cells = candidate.find_all(["td", "th"])
        headers = header_names(candidate)
        status_cell = first_matching_cell(cells, headers, re.compile(r"status"), re.compile(r"status"))
        question_cell = first_matching_cell(cells, headers, re.compile(r"question"), re.compile(r"question"))
        explanation_cell = first_matching_cell(
            cells, headers, re.compile(r"explanation|reason"), re.compile(r"explanation|reason")
        )
        response_cell = first_matching_cell(cells, headers, re.compile(r"response|reply"), re.compile(r"response|reply"))
        created_cell = first_matching_cell(
            cells, headers, re.compile(r"created|requested|date|time"), re.compile(r"created|requested|date|time")
        )
        status_raw = nullable_text(value_content(status_cell))
        question_name = nullable_text(value_content(question_cell))
        explanation = nullable_text(value_content(explanation_cell))
        response = nullable_text(value_content(response_cell))
        created_at = nullable_text(value_content(created_cell))
        key = request_id or f"{question_name or ''}|{created_at or ''}|{explanation or row_text}"
        if key in seen:
            continue
        seen.add(key)
        requests.append(
            {
                "id": request_id,
                "question_name": question_name,
                "status": regrade_status_from_raw(status_raw),
                "status_raw": status_raw,
                "explanation": explanation,
                "response": response,
                "created_at": created_at,
                "url": href,
            }
        )
    return requests
