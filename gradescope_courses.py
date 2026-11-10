"""Dashboard (course list) parsing, ported from bruglet/gradescope-mcp."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from gradescope_parsers import nullable_text, text_content

SEASON_ORDER = {"spring": 1, "summer": 2, "fall": 3, "winter": 4}


def parse_term(term: str | None) -> tuple[int, int] | None:
    """Parse a term label like 'Fall 2026' into (year, season_order)."""
    if not term:
        return None
    match = re.search(r"(spring|summer|fall|winter)[\s,]*(\d{4})", term, re.I)
    if match:
        return int(match.group(2)), SEASON_ORDER[match.group(1).lower()]
    match = re.search(r"(\d{4})[\s,]*(spring|summer|fall|winter)", term, re.I)
    if match:
        return int(match.group(1)), SEASON_ORDER[match.group(2).lower()]
    year = re.search(r"\d{4}", term)
    if year:
        return int(year.group(0)), 0
    return None


def current_term_key(now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now()
    if now.month >= 8:
        return now.year, SEASON_ORDER["fall"]
    if now.month >= 5:
        return now.year, SEASON_ORDER["summer"]
    return now.year, SEASON_ORDER["spring"]


def select_current_courses(courses: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Keep only courses in the current term; fall back to the latest term seen."""
    current = current_term_key(now)
    matching = [c for c in courses if parse_term(c.get("term")) == current]
    if matching:
        return matching
    keyed = [(parse_term(c.get("term")) or (0, 0), c) for c in courses]
    if not keyed:
        return []
    latest = max(key for key, _ in keyed)
    return [c for key, c in keyed if key == latest]


def role_from_heading(value: str) -> str:
    heading = value.lower()
    if "instructor" in heading:
        return "instructor"
    if "teaching assistant" in heading or re.search(r"\bta\b", heading):
        return "ta"
    if "student" in heading:
        return "student"
    return "unknown"


def parse_course_list(
    course_list: Tag,
    role: str,
    courses: list[dict[str, Any]],
    seen_ids: set[str],
    inherited_term: str | None = None,
) -> None:
    current_term = inherited_term
    for child in course_list.children:
        if not isinstance(child, Tag):
            continue
        classes = child.get("class") or []
        if "courseList--term" in classes:
            current_term = nullable_text(text_content(child))
            continue
        if "courseList--coursesForTerm" in classes:
            for card in child.select("a.courseBox"):
                href = card.get("href") or ""
                match = re.search(r"/courses/(\d+)", href)
                if not match:
                    continue
                course_id = match.group(1)
                if course_id in seen_ids:
                    continue
                short_name = text_content(card.select_one(".courseBox--shortname"))
                full_name = text_content(card.select_one(".courseBox--name"))
                seen_ids.add(course_id)
                courses.append(
                    {
                        "id": course_id,
                        "name": full_name or short_name,
                        "short_name": short_name or full_name,
                        "term": current_term,
                        "role": role,
                        "url": href,
                    }
                )
            continue
        if "courseList--inactiveCourses" in classes:
            parse_course_list(child, role, courses, seen_ids, current_term)


def parse_dashboard(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    account = soup.select_one("#account-show") or soup
    role_map: dict[int, str] = {}
    for heading in account.select("h2.pageHeading, h2"):
        role = role_from_heading(text_content(heading))
        sibling = heading.find_next_sibling()
        while sibling is not None:
            if not isinstance(sibling, Tag):
                sibling = sibling.find_next_sibling()
                continue
            if "courseList" in (sibling.get("class") or []):
                role_map[id(sibling)] = role
                break
            if sibling.name == "h2":
                break
            sibling = sibling.find_next_sibling()

    title_element = soup.select_one("title")
    page_title = text_content(title_element).lower() if title_element else ""
    has_explicit_non_student = any(role in ("instructor", "ta") for role in role_map.values())
    use_student_fallback = bool(re.search(r"\byour courses\b", page_title)) and not has_explicit_non_student

    courses: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for course_list in account.select(".courseList"):
        mapped_role = role_map.get(id(course_list))
        if mapped_role == "unknown" and use_student_fallback:
            role = "student"
        elif mapped_role is not None:
            role = mapped_role
        else:
            role = "student" if use_student_fallback else "unknown"
        parse_course_list(course_list, role, courses, seen_ids)
    return courses
