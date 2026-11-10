"""HTML parsers for Gradescope's student pages.

Ported from the student read-only surface of bruglet/gradescope-mcp
(https://github.com/bruglet/gradescope-mcp).
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://www.gradescope.com"
BASE_ORIGIN = "https://www.gradescope.com"


def text_content(element: Any) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", element.get_text()).strip()


def nullable_text(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = re.sub(r"\s+", " ", value).strip()
    return trimmed or None


def value_content(element: Tag | None) -> str | None:
    if element is None:
        return None
    machine = element.select_one("time[datetime]")
    if machine is not None:
        return nullable_text(machine.get("datetime"))
    return nullable_text(text_content(element))


def parse_number(text: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_score(text: str) -> tuple[float, float] | None:
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)", text.replace(",", ""))
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def header_names(row: Tag) -> list[str]:
    table = row.find_parent("table")
    if table is None:
        return []
    header_row = table.select_one("thead tr") or table.select_one("tr")
    if header_row is None or header_row is row:
        return []
    return [normalized_header(text_content(cell)) for cell in header_row.find_all(["th", "td"])]


def cell_by_header(cells: list[Tag], headers: list[str], matcher: re.Pattern[str]) -> Tag | None:
    for index, header in enumerate(headers):
        if matcher.search(header):
            return cells[index] if index < len(cells) else None
    return None


def cell_by_class(cells: list[Tag], matcher: re.Pattern[str]) -> Tag | None:
    for cell in cells:
        classes = " ".join(cell.get("class") or []).lower()
        if matcher.search(classes):
            return cell
    return None


def first_matching_cell(
    cells: list[Tag], headers: list[str], header_matcher: re.Pattern[str], class_matcher: re.Pattern[str]
) -> Tag | None:
    cell = cell_by_header(cells, headers, header_matcher)
    if cell is None:
        cell = cell_by_class(cells, class_matcher)
    return cell


def assignment_path_from_value(value: str | None, course_id: str | None) -> str | None:
    if not value or not course_id:
        return None
    try:
        url = urljoin(BASE_URL + "/", value)
        if f"{urlparse(url).scheme}://{urlparse(url).netloc}" != BASE_ORIGIN:
            return None
        match = re.match(rf"^/courses/{re.escape(course_id)}/assignments/(\d+)/?$", urlparse(url).path)
        if match:
            return f"/courses/{course_id}/assignments/{match.group(1)}"
    except ValueError:
        return None
    return None


def row_assignment_path(row: Tag, course_id: str | None) -> str | None:
    if not course_id:
        return None
    for element in [row, *row.find_all(True)]:
        for attribute in ("href", "data-url", "data-href", "action"):
            path = assignment_path_from_value(element.get(attribute), course_id)
            if path:
                return path
    return None


def row_assignment_id(row: Tag) -> str | None:
    candidates: list[tuple[str, int]] = []
    for element in [row, *row.find_all(True)]:
        for attribute in ("data-assignment-id", "data-assignment_id", "data-assignment"):
            value = (element.get(attribute) or "").strip()
            if re.fullmatch(r"\d+", value):
                candidates.append((value, 0))
        element_id = (element.get("id") or "").strip()
        match = re.match(r"^assignment[-_](\d+)$", element_id, re.I)
        if match:
            candidates.append((match.group(1), 1))
        data_id = (element.get("data-id") or "").strip()
        if re.fullmatch(r"\d+", data_id):
            candidates.append((data_id, 2))
    if not candidates:
        return None
    best = min(priority for _, priority in candidates)
    best_ids = {value for value, priority in candidates if priority == best}
    return next(iter(best_ids)) if len(best_ids) == 1 else None


def text_before_descendant(root: Tag, target: Tag) -> str | None:
    text: list[str] = []

    def visit(node: Any) -> bool:
        for child in node.children:
            if child is target:
                return True
            if isinstance(child, Tag):
                if visit(child):
                    return True
            else:
                text.append(str(child))
        return False

    return "".join(text) if visit(root) else None


DEADLINE_LABEL_RE = re.compile(
    r"\b(?:released?(?:\s+date)?|late\s+(?:due(?:\s+date)?|deadline)|due(?:\s+date)?|deadline)\b", re.I
)


def deadline_label_kind(text: str) -> str | None:
    labels = DEADLINE_LABEL_RE.findall(text)
    if not labels:
        return None
    last = labels[-1].lower()
    if last.startswith("release"):
        return "release"
    return "late" if last.startswith("late") else "regular"


def deadline_date_content(cell: Tag | None, kind: str, allow_unlabeled: bool) -> str | None:
    if cell is None:
        return None
    times = cell.find_all("time")
    for time_element in times:
        parent = time_element.parent if isinstance(time_element.parent, Tag) else cell
        label = (
            deadline_label_kind(time_element.get("aria-label") or "")
            or deadline_label_kind(text_content(time_element))
            or deadline_label_kind(parent.get("aria-label") or "")
            or deadline_label_kind(text_before_descendant(parent, time_element) or "")
        )
        if label == kind or (label is None and allow_unlabeled):
            return nullable_text(time_element.get("datetime") or text_content(time_element))
    if times:
        return None

    text = text_content(cell)
    labels = [(m.start(), m.group(0)) for m in DEADLINE_LABEL_RE.finditer(text)]
    matching = next((l for l in labels if deadline_label_kind(l[1]) == kind), None)
    if matching is not None:
        following = next((l for l in labels if l[0] > matching[0]), None)
        value = text[matching[0] + len(matching[1]) : following[0] if following else len(text)]
    else:
        if not allow_unlabeled or labels:
            return None
        value = text
    value = re.split(r";|\b(?:accepting\s+late\s+submissions|closed|closes\s+in)\b", value, flags=re.I)[0]
    return nullable_text(re.sub(r"^\s*(?::|at\b)\s*", "", value, flags=re.I))


def is_hidden_column(cell: Tag) -> bool:
    classes = " ".join(cell.get("class") or [])
    return (
        bool(re.search(r"\bhidden-column\b", classes))
        or cell.has_attr("hidden")
        or cell.get("aria-hidden") == "true"
    )


def complete_deadline_epoch(value: str | None) -> int | None:
    if not value:
        return None
    timestamp = value
    gradescope_date = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ([+-]\d{2})(\d{2})$", timestamp)
    if gradescope_date:
        timestamp = f"{gradescope_date.group(1)}T{gradescope_date.group(2)}{gradescope_date.group(3)}:{gradescope_date.group(4)}"
    elif not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$", timestamp, re.I):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


HUMAN_DATE_FORMATS = (
    "%b %d, %Y at %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y at %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y at %I:%M%p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
)


def deadline_to_iso(value: str | None) -> str | None:
    """Normalize a Gradescope deadline string to ISO 8601.

    Offset-aware when the source carries timezone information, otherwise a
    naive local ISO string (Gradescope renders human labels in course time).
    """
    if value:
        timestamp = value
        gradescope_date = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ([+-]\d{2})(\d{2})$", timestamp)
        if gradescope_date:
            timestamp = f"{gradescope_date.group(1)}T{gradescope_date.group(2)}{gradescope_date.group(3)}:{gradescope_date.group(4)}"
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$", timestamp, re.I):
            try:
                return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).isoformat()
            except ValueError:
                return None
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    cleaned = re.sub(r"\s+(PDT|PST|MDT|MST|CDT|CST|EDT|EST|UTC|GMT|Z)$", "", cleaned).strip()
    for fmt in HUMAN_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).isoformat()
        except ValueError:
            continue
    return None


def deadline_status_from(
    due_date: str | None, late_due_date: str | None, deadline_text: str, now_ms: int
) -> str:
    if re.search(r"accepting\s+late\s+submissions", deadline_text, re.I):
        return "accepting_late"
    if re.search(r"\bclosed\b", deadline_text, re.I):
        return "closed"
    due = complete_deadline_epoch(due_date)
    late_due = complete_deadline_epoch(late_due_date)
    if due is not None and late_due is not None and late_due < due:
        return "unknown"
    if due is not None and due > now_ms:
        return "before_due"
    if due is not None and late_due is not None and late_due > due and late_due > now_ms:
        return "accepting_late"
    if late_due is not None and late_due <= now_ms:
        return "closed"
    return "unknown"


def status_from_raw(raw: str | None) -> str:
    if not raw:
        return "unknown"
    value = raw.lower()
    if re.search(r"not submitted|unsubmitted|no submission|missing", value):
        return "unsubmitted"
    if re.search(r"graded|score released|returned", value):
        return "graded"
    if re.search(r"submitted|turned in|on time|late", value):
        return "submitted"
    return "unknown"


def submitted_from_status(status: str) -> bool | None:
    if status in ("submitted", "graded"):
        return True
    if status == "unsubmitted":
        return False
    return None


def status_with_score(status: str, score: tuple[float, float] | None) -> str:
    return "graded" if status == "unknown" and score else status


def late_from_text(values: list[str | None]) -> bool | None:
    text = " ".join(v for v in values if v).lower()
    if re.search(r"\blate\b|overdue", text):
        return True
    if re.search(r"\bon time\b|on-time|not late", text):
        return False
    return None
