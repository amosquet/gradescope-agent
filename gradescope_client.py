"""Authenticated read-only Gradescope client and high-level operations.

Ported from bruglet/gradescope-mcp: Gradescope has no official public API, so
this logs in with the account's email/password form (the only non-GET is the
required POST /login) and parses authenticated HTML pages. Cookies stay in
memory; same-origin URLs and bounded redirects are enforced.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from gradescope_assignments import parse_assignment_list
from gradescope_courses import parse_dashboard
from gradescope_detail import parse_submission_detail
from gradescope_submissions import parse_regrade_requests, parse_submission_list

BASE_URL = "https://www.gradescope.com"
BASE_ORIGIN = "https://www.gradescope.com"
USER_AGENT = "GradescopeHomeworkAgent/1.0"
MAX_REDIRECTS = 5
MIN_REQUEST_INTERVAL = 1.0


class GradescopeError(Exception):
    """Expected failure surfaced to the model (bad credentials, page moved...)."""


def extract_csrf_token(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.select_one('meta[name="csrf-token"]')
    if meta and meta.get("content"):
        return str(meta["content"])
    form_input = soup.select_one('input[name="authenticity_token"]')
    if form_input and form_input.get("value"):
        return str(form_input["value"])
    return None


def is_login_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return bool(
        soup.select_one('form[action="/login"]')
        or soup.select_one('form[action$="/login"]')
        or soup.select_one('input[name="session[email]"]')
        or soup.select_one('input[name="session[password]"]')
    )


class GradescopeClient:
    def __init__(self, email: str, password: str, min_request_interval: float = MIN_REQUEST_INTERVAL) -> None:
        self._email = email
        self._password = password
        self._cookies: dict[str, str] = {}
        self._csrf_token: str | None = None
        self._authenticated = False
        self._last_request_time = 0.0
        self._min_request_interval = min_request_interval
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            timeout=30.0,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    def _extract_set_cookies(self, response: httpx.Response) -> None:
        for header in response.headers.get_list("set-cookie"):
            match = re.match(r"^([^=]+)=([^;]*)", header)
            if match:
                self._cookies[match.group(1)] = match.group(2)

    def _cookie_header(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self._cookies.items())

    def _resolve_url(self, url_path: str) -> str:
        url = urljoin(BASE_URL + "/", url_path)
        if f"{urlparse(url).scheme}://{urlparse(url).netloc}" != BASE_ORIGIN:
            raise GradescopeError("Gradescope requests must remain on the same origin")
        return url

    async def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            await asyncio.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.monotonic()

    async def login(self) -> None:
        login_page = await self._http.get(self._resolve_url("/login"))
        self._extract_set_cookies(login_page)
        if not login_page.is_success:
            raise GradescopeError(f"Gradescope login page failed ({login_page.status_code})")
        self._csrf_token = extract_csrf_token(login_page.text)
        if not self._csrf_token:
            raise GradescopeError("Failed to extract CSRF token from Gradescope login page")

        response = await self._http.post(
            self._resolve_url("/login"),
            data={
                "utf8": "✓",
                "authenticity_token": self._csrf_token,
                "session[email]": self._email,
                "session[password]": self._password,
                "session[remember_me]": "1",
                "commit": "Log In",
                "session[remember_me_sso]": "0",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": self._cookie_header(),
                "Referer": self._resolve_url("/login"),
                "Origin": BASE_ORIGIN,
            },
        )
        self._extract_set_cookies(response)

        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise GradescopeError("Gradescope login succeeded without a redirect location")
            if "/login" in location or "/sessions" in location:
                raise GradescopeError("Gradescope login failed: invalid email or password")
            dashboard = await self._http.get(self._resolve_url(location), headers={"Cookie": self._cookie_header()})
            self._extract_set_cookies(dashboard)
            if not dashboard.is_success:
                raise GradescopeError(f"Gradescope dashboard failed ({dashboard.status_code})")
            if is_login_page(dashboard.text):
                raise GradescopeError("Gradescope authentication returned the login page")
            new_token = extract_csrf_token(dashboard.text)
            if new_token:
                self._csrf_token = new_token
            self._authenticated = True
            return

        if response.status_code == 200:
            body = response.text
            if is_login_page(body) or "Invalid email/password" in body:
                raise GradescopeError("Gradescope login failed: invalid email or password")
            self._authenticated = True
            return

        raise GradescopeError(f"Gradescope login failed with status {response.status_code}")

    async def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            await self.login()

    async def fetch_page(self, url_path: str) -> str:
        async with self._lock:
            return await self._fetch_with_redirects(url_path, 0)

    async def _fetch_with_redirects(self, url_path: str, redirect_depth: int) -> str:
        if redirect_depth > MAX_REDIRECTS:
            raise GradescopeError("Gradescope returned too many redirects")
        await self._ensure_authenticated()
        await self._throttle()

        url = self._resolve_url(url_path)
        response = await self._http.get(url, headers={"Cookie": self._cookie_header()})
        self._extract_set_cookies(response)

        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise GradescopeError("Gradescope returned a redirect without a location")
            if "/login" in location:
                self._authenticated = False
                await self.login()
                return await self._fetch_with_redirects(url_path, redirect_depth + 1)
            return await self._fetch_with_redirects(location, redirect_depth + 1)

        if not response.is_success:
            raise GradescopeError(f"Gradescope GET {url_path} failed ({response.status_code})")

        html = response.text
        if is_login_page(html):
            self._authenticated = False
            await self.login()
            return await self._fetch_with_redirects(url_path, redirect_depth + 1)
        new_token = extract_csrf_token(html)
        if new_token:
            self._csrf_token = new_token
        return html

    async def close(self) -> None:
        await self._http.aclose()


_client: GradescopeClient | None = None
_client_lock = asyncio.Lock()


async def get_client() -> GradescopeClient:
    global _client
    async with _client_lock:
        if _client is None:
            email = (os.environ.get("gradescope_email") or os.environ.get("GRADESCOPE_EMAIL") or "").strip()
            password = os.environ.get("gradescope_password") or os.environ.get("GRADESCOPE_PASSWORD") or ""
            if not email or not password:
                raise GradescopeError(
                    "gradescope_email / gradescope_password are not configured for this deployment"
                )
            _client = GradescopeClient(email, password)
        return _client


async def list_student_courses() -> list[dict[str, Any]]:
    client = await get_client()
    courses = parse_dashboard(await client.fetch_page("/account"))
    return [course for course in courses if course["role"] == "student"]


async def require_student_course(course_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d+", course_id):
        raise GradescopeError("course_id must be numeric")
    for course in await list_student_courses():
        if course["id"] == course_id:
            return course
    raise GradescopeError(f"Course {course_id} is not available under the logged-in account's Student Courses")


async def list_course_assignments(course_id: str) -> list[dict[str, Any]]:
    await require_student_course(course_id)
    client = await get_client()
    return parse_assignment_list(await client.fetch_page(f"/courses/{course_id}"), course_id)


async def list_assignment_submissions(course_id: str, assignment_id: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"\d+", assignment_id):
        raise GradescopeError("assignment_id must be numeric")
    await require_student_course(course_id)
    client = await get_client()
    # The student course page links directly to the student's own submission
    # detail pages; the assignment-level submissions index is instructor-only.
    return parse_submission_list(await client.fetch_page(f"/courses/{course_id}"), course_id, assignment_id)


async def get_submission_detail(course_id: str, assignment_id: str, submission_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d+", submission_id):
        raise GradescopeError("submission_id must be numeric")
    summaries = await list_assignment_submissions(course_id, assignment_id)
    summary = next((s for s in summaries if s["id"] == submission_id), None)
    if summary is None:
        raise GradescopeError(f"Submission {submission_id} was not found for assignment {assignment_id}")
    client = await get_client()
    detail = parse_submission_detail(await client.fetch_page(summary["url"]))
    merged = {**summary}
    for key in ("score", "max_score", "status_raw", "submitted", "submitted_at", "late", "lateness"):
        if detail.get(key) is not None:
            merged[key] = detail[key]
    if detail.get("submission_status") != "unknown":
        merged["submission_status"] = detail["submission_status"]
    merged["questions"] = detail["questions"]
    return merged


async def list_assignment_regrade_requests(course_id: str, assignment_id: str) -> list[dict[str, Any]]:
    await require_student_course(course_id)
    client = await get_client()
    primary_error: Exception | None = None
    try:
        requests = parse_regrade_requests(
            await client.fetch_page(f"/courses/{course_id}/assignments/{assignment_id}/regrade_requests")
        )
        if requests:
            return requests
    except Exception as error:
        primary_error = error
    submissions = await list_assignment_submissions(course_id, assignment_id)
    requests = []
    for submission in submissions:
        requests.extend(parse_regrade_requests(await client.fetch_page(submission["url"])))
    if not requests and primary_error is not None:
        raise GradescopeError(f"Unable to read student regrade requests: {primary_error}")
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for request in requests:
        key = request["id"] or request["url"] or f"{request['question_name']}|{request['created_at']}|{request['explanation']}"
        if key not in seen:
            seen.add(key)
            merged.append(request)
    return merged
