"""Assignment list parsing, ported from bruglet/gradescope-mcp."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from gradescope_parsers import (
    BASE_URL,
    cell_by_header,
    deadline_date_content,
    deadline_status_from,
    deadline_to_iso,
    first_matching_cell,
    header_names,
    is_hidden_column,
    late_from_text,
    nullable_text,
    parse_score,
    row_assignment_id,
    row_assignment_path,
    status_from_raw,
    status_with_score,
    submitted_from_status,
    text_content,
    value_content,
)


def assignment_type(row: Tag, href: str) -> str:
    type_element = row.select_one(".assignment-type, .badge, [data-assignment-type]")
    label = nullable_text(text_content(type_element))
    if label:
        return label.lower()
    if "programming" in href:
        return "programming"
    if "online" in href:
        return "online"
    return "unknown"


def assignment_from_row(row: Tag, course_id: str | None, now_ms: int) -> dict[str, Any] | None:
    link = row.select_one('a[href*="/assignments/"]')
    linked_href = link.get("href") if link is not None else ""
    linked_match = re.search(r"/assignments/(\d+)", linked_href)
    linked_id = linked_match.group(1) if linked_match else None
    row_path = row_assignment_path(row, course_id)
    row_path_match = re.search(r"/assignments/(\d+)", row_path) if row_path else None
    assignment_id = linked_id or (row_path_match.group(1) if row_path_match else None) or row_assignment_id(row)
    if not assignment_id:
        return None
    href = linked_href or row_path or (f"/courses/{course_id}/assignments/{assignment_id}" if course_id else "")
    if not href:
        return None

    cells = row.find_all(["td", "th"])
    headers = header_names(row)
    row_text = text_content(row)

    late_due_cell = first_matching_cell(
        cells,
        headers,
        re.compile(r"late due|late deadline|late submission deadline", re.I),
        re.compile(r"late.*due|late.*deadline|late.*date", re.I),
    )
    if late_due_cell is None:
        late_due_cell = next(
            (
                cell
                for cell in cells
                if re.search(r"\blate\s+(?:due\s+date|deadline)\b", text_content(cell), re.I)
                or any(
                    re.search(r"\blate\s+(?:due\s+date|deadline)\b", el.get("aria-label") or "", re.I)
                    for el in cell.select("[aria-label]")
                )
            ),
            None,
        )
    due_cell = cell_by_header(
        cells, headers, re.compile(r"^(?:due|due date|deadline|deadline date)$")
    )
    if due_cell is None:
        due_cell = first_matching_cell(
            cells, headers, re.compile(r"^(?!.*late).*(due|deadline)"), re.compile(r"due|deadline|date")
        )
    status_cell = first_matching_cell(cells, headers, re.compile(r"(^| )status($| )"), re.compile(r"status|submission"))
    score_cell = first_matching_cell(cells, headers, re.compile(r"score|grade|points"), re.compile(r"score|grade|points"))
    submitted_at_cell = first_matching_cell(
        cells, headers, re.compile(r"submitted at|submission time|timestamp"), re.compile(r"submitted|timestamp|time")
    )
    late_cell = first_matching_cell(
        cells, headers, re.compile(r"^(?!.*due).*(late|lateness|overdue)"), re.compile(r"late(?!.*due)|lateness")
    )

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
    late_due_date = deadline_date_content(late_due_cell, "late", late_due_cell is not due_cell)
    padded_headers = headers + [""] * max(0, len(cells) - len(headers))
    labeled_due_date = next(
        (
            date
            for cell, header in zip(cells, padded_headers)
            if not is_hidden_column(cell)
            and (cell is due_cell or cell is late_due_cell or re.search(r"due|deadline", header))
            for date in [deadline_date_content(cell, "regular", False)]
            if date is not None
        ),
        None,
    )
    due_cell_index = next((i for i, c in enumerate(cells) if c is due_cell), None)
    due_header = headers[due_cell_index] if due_cell_index is not None and due_cell_index < len(headers) else ""
    due_date = labeled_due_date or deadline_date_content(
        due_cell if due_cell is not None and not is_hidden_column(due_cell) else None,
        "regular",
        (due_cell is not late_due_cell) and not re.search(r"\breleased?\b", due_header, re.I),
    )
    deadline_text = " ".join(
        text_content(cell) for cell in list(dict.fromkeys([c for c in (due_cell, late_due_cell) if c is not None]))
    )
    name_cell = cell_by_header(cells, headers, re.compile(r"^(?:name|assignment)$"))
    if name_cell is None and cells:
        name_cell = cells[0]

    return {
        "id": assignment_id,
        "name": nullable_text(text_content(link)) or text_content(name_cell),
        "type": assignment_type(row, href),
        "due_date": due_date,
        "due_date_iso": deadline_to_iso(due_date),
        "late_due_date": late_due_date,
        "late_due_date_iso": deadline_to_iso(late_due_date),
        "deadline_status": deadline_status_from(due_date, late_due_date, deadline_text, now_ms),
        "released": not row.select_one(".unreleased, .draft") and not re.search(r"\bunreleased\b", row_text, re.I),
        "submission_status": status,
        "status_raw": status_raw,
        "submitted": submitted_from_status(status),
        "submitted_at": value_content(submitted_at_cell),
        "late": late,
        "lateness": late_text,
        "points_possible": score[1] if score else None,
        "points_awarded": score[0] if score else None,
        "url": href if href.startswith("/") else urljoin(BASE_URL + "/", href),
    }


def parse_assignment_list(html: str, course_id: str | None = None, now_ms: int | None = None) -> list[dict[str, Any]]:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    soup = BeautifulSoup(html, "html.parser")
    assignments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in soup.select("tr, .assignment-row"):
        assignment = assignment_from_row(row, course_id, now_ms)
        if not assignment or assignment["id"] in seen_ids:
            continue
        seen_ids.add(assignment["id"])
        assignments.append(assignment)
    return assignments
