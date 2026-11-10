"""Tests for assignment-file link parsing and the Drive export flow."""

from __future__ import annotations

import asyncio
from pathlib import Path

from gradescope_files import file_name_from_url, is_file_url, parse_assignment_files

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_assignment_page_pdf_link():
    files = parse_assignment_files(fixture("assignment-pdf.html"))
    assert len(files) == 1
    entry = files[0]
    assert entry["name"] == "Reaction_Maze_.pdf"
    assert entry["assignment_id"] == "701"
    assert entry["url"].startswith("https://production-gradescope-uploads.s3-us-west-2.amazonaws.com/")
    assert "X-Amz-Signature=" in entry["url"]


def test_parse_course_page_template_urls():
    files = parse_assignment_files(fixture("assignment-pdf-template-attribute.html"))
    assert files, "expected at least one file link"
    assert all(f["assignment_id"] for f in files)
    urls = [f["url"] for f in files]
    assert len(urls) == len(set(urls)), "links must be deduplicated"
    assert any(f["name"].endswith(".pdf") for f in files)


def test_is_file_url_rules():
    assert is_file_url("https://production-gradescope-uploads.s3-us-west-2.amazonaws.com/x/y.pdf?sig=1")
    assert is_file_url("https://www.gradescope.com/courses/1/assignments/2/files/a.zip")
    assert not is_file_url("http://production-gradescope-uploads.s3-us-west-2.amazonaws.com/x.pdf")
    assert not is_file_url("https://evil.example.com/x.pdf")
    assert not is_file_url("https://www.gradescope.com/courses/1")
    assert file_name_from_url("https://h/a/b/My%20File.pdf?x=1") == "My_File.pdf"


def test_export_flow_uploads_new_and_skips_existing(monkeypatch):
    import filesync

    courses = [{"id": "1", "short_name": "CS 251", "name": "CS 25100", "role": "student", "url": "/courses/1"}]
    files = [
        {
            "name": "HW1.pdf",
            "url": "https://production-gradescope-uploads.s3-us-west-2.amazonaws.com/u/a/HW1.pdf?sig=1",
            "assignment_id": "10",
        }
    ]

    async def fake_courses():
        return courses

    async def fake_scan(course_id):
        assert course_id == "1"
        return ([{"id": "10"}], files)

    downloads: list[str] = []

    async def fake_fetch(url):
        downloads.append(url)
        return (b"%PDF-fake", "application/pdf")

    async def fake_ensure_folder(name, parent_id=None):
        return {"id": f"folder-{name}"}

    async def fake_find_missing(folder_id, name):
        return None

    async def fake_upload(name, folder_id, content, mime_type):
        return {"id": "file-1"}

    monkeypatch.setattr(filesync, "list_current_student_courses", fake_courses)
    monkeypatch.setattr(filesync, "fetch_course_assignments_and_files", fake_scan)
    monkeypatch.setattr(filesync, "fetch_assignment_file", fake_fetch)
    monkeypatch.setattr(filesync, "drive_ensure_folder", fake_ensure_folder)
    monkeypatch.setattr(filesync, "drive_find_file", fake_find_missing)
    monkeypatch.setattr(filesync, "drive_upload_file", fake_upload)

    summary = asyncio.run(filesync.export_assignment_files_to_drive())
    assert summary["files_found"] == 1
    assert [u["name"] for u in summary["files_uploaded"]] == ["CS 251/HW1.pdf"]
    assert summary["files_uploaded"][0]["size"] == len(b"%PDF-fake")
    assert downloads == [files[0]["url"]]
    assert not summary["errors"]

    async def fake_find_existing(folder_id, name):
        return {"id": "file-1"}

    monkeypatch.setattr(filesync, "drive_find_file", fake_find_existing)
    downloads.clear()
    rerun = asyncio.run(filesync.export_assignment_files_to_drive())
    assert rerun["files_skipped_existing"] == ["CS 251/HW1.pdf"]
    assert not rerun["files_uploaded"]
    assert not downloads
