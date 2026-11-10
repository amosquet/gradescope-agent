"""Submission detail parsing (scores, rubric items, comments).

Ported from bruglet/gradescope-mcp, combining the HTML outline with the
AssignmentSubmissionViewer react props.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from gradescope_parsers import (
    late_from_text,
    nullable_text,
    parse_number,
    parse_score,
    status_from_raw,
    status_with_score,
    submitted_from_status,
    text_content,
    value_content,
)


def unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def question_sections(root: Tag) -> list[Tag]:
    primary = root.select(".question, [data-question-id], .rubric-question, .question-group, [class*='question-group']")
    if primary:
        return primary
    return root.select("[class*='question-']")


def parse_question_results(root: Tag) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in question_sections(root):
        heading = section.select_one(
            ".question-title, .submissionOutline--sectionHeading, .submissionOutlineQuestion--sectionHeading, .name, h2, h3, h4"
        )
        name = nullable_text(text_content(heading) if heading is not None else None) or nullable_text(text_content(section))
        if not name:
            continue
        score = None
        for selector in (
            ".submissionOutlineQuestion--weightAndScore",
            "[class*='weightAndScore']",
            "[class*='score'], .points",
        ):
            element = section.select_one(selector)
            if element is not None:
                score = parse_score(text_content(element))
                if score:
                    break
        key = f"{name}|{score[0] if score else ''}|{score[1] if score else ''}"
        if key in seen:
            continue
        seen.add(key)
        rubric_items = []
        for rubric in section.select(".rubricItem, [class*='rubric-item'], .rubric-row"):
            description = nullable_text(
                text_content(rubric.select_one(".rubricItem--description, .description, td:first-child"))
            )
            if not description:
                continue
            points_element = rubric.select_one(".rubricItem--points, .points, td:last-child")
            points = parse_number(text_content(points_element)) if points_element is not None else None
            classes = " ".join(rubric.get("class") or [])
            applied = bool(rubric.select_one(".rubricItem--selected, .selected, .applied, .checked")) or bool(
                re.search(r"(?:^|\s)(?:rubricItem--)?(?:selected|applied|checked)(?:\s|$)", classes)
            )
            rubric_items.append({"description": description, "points": points, "applied": applied})
        comments = unique_strings([text_content(c) for c in section.select(".comment, [class*='comment'], .annotation")])
        questions.append(
            {
                "name": name,
                "score": score[0] if score else None,
                "max_score": score[1] if score else None,
                "scoring_type": None,
                "rubric_items": rubric_items,
                "score_adjustments": [],
                "comments": comments,
            }
        )
    return questions


def is_json_record(value: Any) -> bool:
    return isinstance(value, dict)


def record_value(record: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def scalar_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return nullable_text(str(value))


def scalar_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    if isinstance(value, str):
        trimmed = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", trimmed):
            return float(trimmed)
    return None


def numeric_identifier(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        return value.strip()
    return None


def record_number(record: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        number = scalar_number(record.get(key))
        if number is not None:
            return number
    return None


MAX_SCORE_KEYS = [
    "max_score", "maxScore", "points_possible", "pointsPossible",
    "max_points", "maxPoints", "total_points", "totalPoints", "weight", "points",
]
EXPLICIT_MAX_SCORE_KEYS = [k for k in MAX_SCORE_KEYS if k != "points"]


def question_id_from_record(record: dict[str, Any], include_record_id: bool) -> str | None:
    direct = numeric_identifier(record_value(record, ["question_id", "questionId", "questionID"]))
    if direct:
        return direct
    question = record.get("question")
    if is_json_record(question):
        return question_id_from_record(question, True)
    nested = numeric_identifier(question)
    if nested:
        return nested
    return numeric_identifier(record.get("id")) if include_record_id else None


def question_name_from_record(record: dict[str, Any]) -> str | None:
    for key in ("name", "title", "question_name", "questionName", "display_name", "displayName", "label"):
        value = scalar_text(record.get(key))
        if value and not re.match(r"^\(no title\)$", value, re.I):
            return value
    numbered = scalar_text(record.get("numbered_title"))
    if numbered:
        return numbered if re.match(r"^question\b", numbered, re.I) else f"Question {numbered}"
    question = record.get("question")
    if is_json_record(question):
        return question_name_from_record(question)
    return None


def question_max_score(record: dict[str, Any]) -> float | None:
    return record_number(record, MAX_SCORE_KEYS)


def question_score(record: dict[str, Any]) -> tuple[float | None, float | None]:
    score_value = record_value(record, ["score", "points_awarded", "pointsAwarded", "earned_score", "earnedScore"])
    if score_value is None:
        score_value = record.get("points")
    score_text = scalar_text(score_value)
    if score_text:
        pair = parse_score(score_text)
        if pair:
            return pair
    return scalar_number(score_value), record_number(record, EXPLICIT_MAX_SCORE_KEYS)


def collect_question_metadata(
    value: Any, metadata: dict[str, dict[str, Any]], inherited_id: str | None = None, depth: int = 0
) -> None:
    if depth > 8:
        return
    if isinstance(value, list):
        for item in value:
            collect_question_metadata(item, metadata, inherited_id, depth + 1)
        return
    if not is_json_record(value):
        return
    question_id = question_id_from_record(value, True) or inherited_id
    name = question_name_from_record(value)
    max_score = question_max_score(value)
    scoring_raw = value.get("scoring_type")
    scoring_type = scoring_raw if scoring_raw in ("positive", "negative") else None
    if question_id and (name is not None or max_score is not None or scoring_type is not None):
        previous = metadata.get(question_id) or {}
        metadata[question_id] = {
            "name": previous.get("name") or name,
            "max_score": previous["max_score"] if previous.get("max_score") is not None else max_score,
            "scoring_type": previous["scoring_type"] if previous.get("scoring_type") is not None else scoring_type,
        }
    for key, child in value.items():
        if isinstance(child, (list, dict)):
            collect_question_metadata(child, metadata, numeric_identifier(key) or question_id, depth + 1)


def object_entries(value: Any) -> list[tuple[dict[str, Any], str | None]]:
    if isinstance(value, list):
        return [(item, None) for item in value if is_json_record(item)]
    if not is_json_record(value):
        return []
    return [(item, numeric_identifier(key)) for key, item in value.items() if is_json_record(item)]


def parse_viewer_props(root: Tag) -> dict[str, Any] | None:
    viewer = root.select_one('div[data-react-class="AssignmentSubmissionViewer"][data-react-props]')
    raw = viewer.get("data-react-props") if viewer is not None else None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if is_json_record(parsed) else None


def parse_viewer_rubrics(props: dict[str, Any], metadata: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rubrics: dict[str, list[dict[str, Any]]] = {}
    if props.get("grades_visible") is False:
        return rubrics
    seen: set[str] = set()

    def position(entry: tuple[dict[str, Any], str | None]) -> float:
        value = scalar_number(entry[0].get("position"))
        return value if value is not None else float("inf")

    for value, key in sorted(object_entries(props.get("rubric_items")), key=position):
        question_id = question_id_from_record(value, False)
        weight = scalar_number(value.get("weight"))
        if not question_id or weight is None or not isinstance(value.get("present"), bool):
            continue
        item_id = numeric_identifier(value.get("id")) or key
        identity = f"{question_id}:{item_id}" if item_id else None
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        scoring_type = metadata.get(question_id, {}).get("scoring_type")
        if scoring_type == "negative":
            points: float | None = 0.0 if weight == 0 else -weight
        elif scoring_type == "positive":
            points = weight
        else:
            points = None
        rubrics.setdefault(question_id, []).append(
            {"description": scalar_text(value.get("description")) or "", "points": points, "applied": value["present"]}
        )
    return rubrics


def parse_viewer_questions(props: dict[str, Any]) -> list[dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    collect_question_metadata(props.get("questions"), metadata)
    collect_question_metadata(props.get("outline"), metadata)
    rubrics = parse_viewer_rubrics(props, metadata)

    inorder = props.get("inorder_leaf_question_ids")
    inorder_ids = [numeric_identifier(n) for n in inorder] if isinstance(inorder, list) else []
    order = {qid: index for index, qid in enumerate(qid for qid in inorder_ids if qid)}
    entries = [
        {"value": value, "key": key, "index": index, "id": question_id_from_record(value, False) or key}
        for index, (value, key) in enumerate(object_entries(props.get("question_submissions")))
    ]

    def sort_key(entry: dict[str, Any]) -> tuple[int, int]:
        entry_order = order.get(entry["id"]) if entry["id"] else None
        if entry_order is None:
            return (1, entry["index"])
        return (0, entry_order)

    entries.sort(key=sort_key)
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        meta = metadata.get(entry["id"]) if entry["id"] else None
        score_value, max_value = question_score(entry["value"])
        max_score = max_value if max_value is not None else (meta or {}).get("max_score")
        name = (
            question_name_from_record(entry["value"])
            or (meta or {}).get("name")
            or (f"Question {index + 1}" if score_value is not None or max_score is not None else None)
        )
        if not name:
            continue
        key = (
            entry["id"]
            or f"{name}|{score_value if score_value is not None else ''}|{max_score if max_score is not None else ''}"
        )
        if key in seen:
            continue
        seen.add(key)
        if props.get("grades_visible") is False:
            evaluations: list[dict[str, Any]] = []
        else:
            evaluations = [value for value, _ in object_entries(entry["value"].get("evaluations"))]
        score_adjustments = []
        for evaluation in evaluations:
            points = scalar_number(evaluation.get("points"))
            if points is not None:
                score_adjustments.append({"points": points, "comment": scalar_text(evaluation.get("comments"))})
        questions.append(
            {
                "name": name,
                "score": score_value,
                "max_score": max_score,
                "scoring_type": (meta or {}).get("scoring_type"),
                "rubric_items": rubrics.get(entry["id"], []) if entry["id"] else [],
                "score_adjustments": score_adjustments,
                "comments": unique_strings([scalar_text(e.get("comments")) or "" for e in evaluations]),
            }
        )
    return questions


def parse_viewer_submission(root: Tag) -> dict[str, Any] | None:
    props = parse_viewer_props(root)
    if not props:
        return None
    raw_submission = props.get("assignment_submission")
    assignment_submission = raw_submission if is_json_record(raw_submission) else {}
    raw_assignment = props.get("assignment")
    assignment = raw_assignment if is_json_record(raw_assignment) else {}
    score_value = record_value(assignment_submission, ["score", "points_awarded", "pointsAwarded"])
    score_text = scalar_text(score_value)
    score_pair = parse_score(score_text) if score_text else None
    score = score_pair[0] if score_pair else scalar_number(score_value)
    max_score = (
        (score_pair[1] if score_pair else None)
        or record_number(assignment_submission, MAX_SCORE_KEYS)
        or record_number(assignment, MAX_SCORE_KEYS)
    )
    questions = parse_viewer_questions(props)
    total_question_max = (
        sum(q["max_score"] for q in questions)
        if questions and all(q["max_score"] is not None for q in questions)
        else None
    )
    resolved_max = max_score if max_score is not None else total_question_max
    status_raw_candidate = scalar_text(
        record_value(assignment_submission, ["submission_status", "submissionStatus", "status"])
    )
    raw_status = status_from_raw(status_raw_candidate)
    status = status_with_score(
        raw_status, (score, resolved_max) if score is not None and resolved_max is not None else None
    )
    explicit_submitted = record_value(assignment_submission, ["submitted", "is_submitted", "isSubmitted"])
    submitted = explicit_submitted if isinstance(explicit_submitted, bool) else submitted_from_status(status)
    submitted_at = scalar_text(
        record_value(assignment_submission, ["submitted_at", "submittedAt", "created_at", "createdAt"])
    )
    lateness = scalar_text(record_value(assignment_submission, ["lateness", "late_by", "lateBy", "late_text"]))
    explicit_late = record_value(assignment_submission, ["late", "is_late", "isLate"])
    late = explicit_late if isinstance(explicit_late, bool) else late_from_text([lateness, status_raw_candidate])
    return {
        "grades_visible": props.get("grades_visible") is not False,
        "score": score,
        "max_score": resolved_max,
        "submission_status": status,
        "status_raw": None if raw_status == "unknown" else status_raw_candidate,
        "submitted": submitted,
        "submitted_at": submitted_at,
        "late": late,
        "lateness": lateness,
        "questions": questions,
    }


def parse_submission_detail(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    summary = soup.select_one(".submissionOutline, [class*='submission-outline'], main") or soup
    score_element = summary.select_one(".submissionOutline--score, [class*='total-score'], .score")
    score = parse_score(text_content(score_element)) if score_element is not None else None
    status_element = summary.select_one(".submissionStatus, [data-status], [class*='status']")
    viewer_submission = parse_viewer_submission(soup)
    html_status_raw = nullable_text(value_content(status_element))
    status_raw = html_status_raw or (viewer_submission or {}).get("status_raw")
    status = (
        status_from_raw(html_status_raw)
        if html_status_raw is not None
        else (viewer_submission or {}).get("submission_status", "unknown")
    )
    resolved_score = (score[0] if score else None) if score else (viewer_submission or {}).get("score")
    resolved_max = (score[1] if score else None) if score else (viewer_submission or {}).get("max_score")
    submission_status = status_with_score(
        status, (resolved_score, resolved_max) if resolved_score is not None and resolved_max is not None else None
    )
    late_element = summary.select_one("[class*='late'], [class*='lateness']")
    late_text = (
        value_content(late_element)
        or (viewer_submission or {}).get("lateness")
        or (status_raw if status_raw and re.search(r"\blate\b", status_raw, re.I) else None)
    )
    html_submitted_at = value_content(summary.select_one("time[datetime], [class*='submitted']"))
    html_questions = parse_question_results(soup)
    viewer_questions = (viewer_submission or {}).get("questions", [])
    if viewer_questions:
        questions = []
        for viewer_question in viewer_questions:
            html_match = next((h for h in html_questions if h["name"] == viewer_question["name"]), None)
            questions.append(
                {
                    **viewer_question,
                    "score": (html_match or {}).get("score", viewer_question["score"]),
                    "max_score": (html_match or {}).get("max_score", viewer_question["max_score"]),
                    "rubric_items": viewer_question["rubric_items"] or (html_match or {}).get("rubric_items", []),
                    "comments": unique_strings(
                        list(viewer_question["comments"]) + (html_match or {}).get("comments", [])
                    ),
                }
            )
    else:
        questions = html_questions
    if viewer_submission and viewer_submission.get("grades_visible") is False:
        questions = [{**q, "rubric_items": [], "score_adjustments": [], "comments": []} for q in questions]
    return {
        "score": resolved_score,
        "max_score": resolved_max,
        "submission_status": submission_status,
        "status_raw": status_raw,
        "submitted": (viewer_submission or {}).get("submitted", submitted_from_status(submission_status)),
        "submitted_at": html_submitted_at or (viewer_submission or {}).get("submitted_at"),
        "late": (viewer_submission or {}).get("late", late_from_text([late_text, status_raw])),
        "lateness": late_text,
        "questions": questions,
    }
