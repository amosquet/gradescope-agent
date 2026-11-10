"""Tests for the Gradescope client's course-list cache."""

from __future__ import annotations

import asyncio

import gradescope_client as gc


class FakeTime:
    """Stand-in for the time module so asyncio's own clock is untouched."""

    def __init__(self, start: float, step: float):
        self.now = start
        self.step = step

    def monotonic(self) -> float:
        value = self.now
        self.now += self.step
        return value


def _patch_courses(monkeypatch, clock: FakeTime):
    calls = {"n": 0}

    class FakeClient:
        async def fetch_page(self, url_path):
            calls["n"] += 1
            assert url_path == "/account"
            return "<html></html>"

    async def fake_get_client():
        return FakeClient()

    monkeypatch.setattr(gc, "get_client", fake_get_client)
    monkeypatch.setattr(gc, "parse_dashboard", lambda html: [{"id": "1", "role": "student"}])
    monkeypatch.setattr(gc, "time", clock)
    monkeypatch.setattr(gc, "_course_list_cache", None)
    return calls


def test_student_courses_cached_within_ttl(monkeypatch):
    calls = _patch_courses(monkeypatch, FakeTime(1000.0, step=0.0))

    async def run():
        first = await gc.list_student_courses()
        second = await gc.list_student_courses()
        return first, second

    first, second = asyncio.run(run())
    assert first == second == [{"id": "1", "role": "student"}]
    assert calls["n"] == 1


def test_student_courses_cache_expires(monkeypatch):
    calls = _patch_courses(monkeypatch, FakeTime(1000.0, step=gc.COURSE_LIST_CACHE_TTL + 1))

    async def run():
        await gc.list_student_courses()
        await gc.list_student_courses()

    asyncio.run(run())
    assert calls["n"] == 2
