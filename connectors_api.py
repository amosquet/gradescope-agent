"""Shared plumbing for calling Mistral connector tools through the connectors API.

The managed worker runs as the deployment's service account, which cannot use
user-scope OAuth connector credentials. Instead the worker holds a workspace
secret with a Mistral API key of the connector owner's account and calls
``POST /v1/connectors/<connector>/tools/<tool>/call?credentials_name=...`` as
that user. The key never enters the session or the logs; it is read from the
environment at call time.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

CONNECTORS_API = "https://api.mistral.ai/v1/connectors"
DEFAULT_CREDENTIALS_NAME = "homework-sync"


class ConnectorError(Exception):
    pass


_http_clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


async def _http_client() -> httpx.AsyncClient:
    """One AsyncClient per event loop, so the many connector calls in a run
    reuse keep-alive connections without leaking a client across loops."""
    loop = asyncio.get_running_loop()
    client = _http_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=60.0)
        _http_clients[loop] = client
    return client


def _content_text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content)


async def call_connector_tool(
    connector: str,
    tool_name: str,
    arguments: dict[str, Any],
    credentials_name: str = DEFAULT_CREDENTIALS_NAME,
) -> Any:
    api_key = (os.environ.get("mistral_calendar_api_key") or os.environ.get("MISTRAL_CALENDAR_API_KEY") or "").strip()
    if not api_key:
        raise ConnectorError("mistral_calendar_api_key is not configured for this deployment")
    url = f"{CONNECTORS_API}/{connector}/tools/{tool_name}/call"
    http = await _http_client()
    response = await http.post(
        url,
        params={"credentials_name": credentials_name},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"arguments": arguments},
    )
    if response.status_code != 200:
        raise ConnectorError(f"{connector} {tool_name} failed: HTTP {response.status_code} {response.text[:300]}")
    payload = response.json()
    metadata = payload.get("metadata") or {}
    if metadata.get("isError"):
        text = _content_text(payload.get("content"))
        raise ConnectorError(f"{connector} {tool_name} returned an error: {text[:500]}")
    structured = metadata.get("structuredContent") or metadata.get("structured_content")
    if structured is not None:
        return structured
    content = payload.get("content") or []
    if content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except (ValueError, TypeError):
            return content[0]["text"]
    return payload
