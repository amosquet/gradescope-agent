"""Parse professor-provided assignment file links from Gradescope pages.

Gradescope serves assignment files (e.g. the provided PDF template) through
time-limited signed URLs on its uploads host. The course assignments table
carries them per row in ``data-template-url`` attributes, and the assignment
page's submit dialog links them via ``a.js-downloadPDFTemplate[href]``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Tag

from gradescope_parsers import text_content

UPLOADS_HOST = "production-gradescope-uploads.s3-us-west-2.amazonaws.com"

FILE_EXTENSIONS = (
    ".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".txt",
    ".py", ".java", ".c", ".h", ".cpp", ".ipynb", ".md", ".tex", ".json",
)


def is_file_url(url: str) -> bool:
    """https URLs on Gradescope's signed uploads host, or same-origin file paths."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        return False
    if parsed.netloc == UPLOADS_HOST:
        return True
    if parsed.netloc == "www.gradescope.com":
        return parsed.path.lower().endswith(FILE_EXTENSIONS)
    return False


def sanitize_name(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or None


def file_name_from_url(url: str, fallback: str | None = None) -> str:
    path = unquote(urlparse(url.strip()).path)
    from_url = sanitize_name(path.rsplit("/", 1)[-1])
    return from_url or sanitize_name(fallback) or "assignment_file"


def _assignment_id_from_element(element: Tag) -> str | None:
    # The download link sits next to (not inside) the submit form, so walk up
    # the ancestors and take the first /assignments/<id> action found.
    node: Tag | None = element
    while node is not None:
        action = node.get("action")
        if action:
            match = re.search(r"/assignments/(\d+)", str(action))
            if match:
                return match.group(1)
        for form in node.find_all("form"):
            match = re.search(r"/assignments/(\d+)", str(form.get("action") or ""))
            if match:
                return match.group(1)
        node = node.parent if isinstance(node.parent, Tag) else None
    return None


def parse_assignment_files(html: str) -> list[dict[str, Any]]:
    """File links found on a course assignments page or an assignment page.

    Each entry: {"name", "url", "assignment_id"} (assignment_id may be None).
    Deduplicated by URL.
    """
    soup = BeautifulSoup(html, "html.parser")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: str | None, fallback_name: str | None, assignment_id: str | None) -> None:
        if not url:
            return
        url = url.strip()
        if not is_file_url(url) or url in seen:
            return
        seen.add(url)
        files.append(
            {
                "name": file_name_from_url(url, fallback_name),
                "url": url,
                "assignment_id": assignment_id,
            }
        )

    for anchor in soup.select("a[href]"):
        add(anchor.get("href"), text_content(anchor), _assignment_id_from_element(anchor))
    for element in soup.select("[data-template-url]"):
        add(
            element.get("data-template-url"),
            text_content(element),
            (element.get("data-assignment-id") or None),
        )
    return files
