"""One-shot export of professor-provided assignment files: Gradescope -> Google Drive.

Scans current-term courses, downloads each file linked on the course page
(typically the provided PDF template, served via a signed URL), and uploads it
to a per-course subfolder of a root Drive folder. Idempotent: files already
present in the target folder (matched by name) are skipped, never overwritten.
"""

from __future__ import annotations

import asyncio
from typing import Any

from drive import DriveError, drive_ensure_folder, drive_find_file, drive_upload_file
from gradescope_client import (
    GradescopeError,
    fetch_assignment_file,
    fetch_course_assignments_and_files,
    list_current_student_courses,
)

DEFAULT_ROOT_FOLDER = "Gradescope Assignments"


def _course_label(course: dict[str, Any]) -> str:
    return course.get("short_name") or course.get("name") or f"course-{course.get('id')}"


async def export_assignment_files_to_drive(root_folder: str = DEFAULT_ROOT_FOLDER) -> dict[str, Any]:
    """Download professor-provided assignment files from current-term courses and
    upload them to Google Drive (root folder + one subfolder per course)."""
    courses = await list_current_student_courses()

    async def scan(course: dict[str, Any]) -> dict[str, Any]:
        assignments, files = await fetch_course_assignments_and_files(course["id"])
        return {**course, "assignments": assignments, "files": files}

    scanned = list(await asyncio.gather(*(scan(course) for course in courses)))

    root = await drive_ensure_folder(root_folder)
    root_id = root.get("id")
    if not root_id:
        raise DriveError(f"could not resolve Drive folder {root_folder!r}")

    uploaded: list[dict[str, Any]] = []
    skipped: list[str] = []
    errors: list[str] = []
    folders: dict[str, str] = {}

    for course in scanned:
        files = course.get("files") or []
        if not files:
            continue
        label = _course_label(course)
        try:
            folder = await drive_ensure_folder(label, parent_id=root_id)
            folder_id = folder.get("id")
            if not folder_id:
                raise DriveError(f"could not resolve Drive folder for {label!r}")
            folders[label] = folder_id
        except Exception as error:  # noqa: BLE001 - report per-course, keep going
            errors.append(f"folder {label}: {error}")
            continue
        for file_info in files:
            name = file_info["name"]
            try:
                existing = await drive_find_file(folder_id, name)
                if existing is not None:
                    skipped.append(f"{label}/{name}")
                    continue
                content, mime_type = await fetch_assignment_file(file_info["url"])
                result = await drive_upload_file(name, folder_id, content, mime_type)
                uploaded.append(
                    {
                        "name": f"{label}/{name}",
                        "id": result.get("id") if isinstance(result, dict) else None,
                        "size": len(content),
                        "assignment_id": file_info.get("assignment_id"),
                    }
                )
            except Exception as error:  # noqa: BLE001 - report per-file, keep going
                errors.append(f"{label}/{name}: {error}")

    return {
        "root_folder": root_folder,
        "root_folder_id": root_id,
        "folders": folders,
        "courses_scanned": [_course_label(c) for c in scanned],
        "files_found": sum(len(c.get("files") or []) for c in scanned),
        "files_uploaded": uploaded,
        "files_skipped_existing": skipped,
        "errors": errors,
    }
