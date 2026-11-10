"""Google Drive access for the agent, via the google_drive connector.

Same connectors-API pattern as gcal.py: the worker calls
``POST /v1/connectors/google_drive/tools/<tool>/call?credentials_name=homework-sync``
with the connector owner's Mistral API key, so the deployment's service
account never touches user-scope OAuth. Requires a Google Drive connection
named ``homework-sync`` in Studio (same name as the calendar one).

NB: the tool names/arguments below are grouped in the TOOL_* constants and
the wrappers — adjust them there if Studio's google_drive connector schema
differs (check Studio -> Connectors -> Google Drive, or
``GET /v1/connectors/google_drive/tools``).
"""

from __future__ import annotations

import base64
from typing import Any

from connectors_api import ConnectorError, DEFAULT_CREDENTIALS_NAME, call_connector_tool

GOOGLE_DRIVE = "google_drive"
CREDENTIALS_NAME = DEFAULT_CREDENTIALS_NAME

FOLDER_MIME = "application/vnd.google-apps.folder"

TOOL_LIST_FILES = "list_files"
TOOL_CREATE_FOLDER = "create_folder"
TOOL_UPLOAD_FILE = "upload_file"


class DriveError(Exception):
    pass


async def call_google_drive(tool_name: str, arguments: dict[str, Any]) -> Any:
    try:
        return await call_connector_tool(GOOGLE_DRIVE, tool_name, arguments, CREDENTIALS_NAME)
    except ConnectorError as error:
        raise DriveError(str(error)) from error


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def drive_list_files(query: str | None = None, page_size: int = 100) -> list[dict[str, Any]]:
    arguments: dict[str, Any] = {"pageSize": page_size}
    if query:
        arguments["query"] = query
    result = await call_google_drive(TOOL_LIST_FILES, arguments)
    if isinstance(result, dict):
        return result.get("files") or []
    return []


async def drive_ensure_folder(name: str, parent_id: str | None = None) -> dict[str, Any]:
    """Find a non-trashed folder by name (optionally under a parent), else create it."""
    query = f"name='{_escape_query(name)}' and mimeType='{FOLDER_MIME}' and trashed=false"
    if parent_id:
        query += f" and '{_escape_query(parent_id)}' in parents"
    existing = await drive_list_files(query)
    if existing:
        return existing[0]
    arguments: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        arguments["parents"] = [parent_id]
    return await call_google_drive(TOOL_CREATE_FOLDER, arguments)


async def drive_find_file(folder_id: str, name: str) -> dict[str, Any] | None:
    query = f"'{_escape_query(folder_id)}' in parents and name='{_escape_query(name)}' and trashed=false"
    files = await drive_list_files(query)
    return files[0] if files else None


async def drive_upload_file(name: str, folder_id: str, content: bytes, mime_type: str = "application/octet-stream") -> dict[str, Any]:
    return await call_google_drive(
        TOOL_UPLOAD_FILE,
        {
            "name": name,
            "parents": [folder_id],
            "mimeType": mime_type,
            "contentBase64": base64.b64encode(content).decode("ascii"),
        },
    )
