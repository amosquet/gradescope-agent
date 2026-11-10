"""Dashboard (course list) parsing, ported from bruglet/gradescope-mcp."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from gradescope_parsers import nullable_text, text_content


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
