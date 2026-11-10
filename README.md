# gradescope-homework-sync

A Mistral Agents SDK agent (managed deployment on Mistral Cloud) that reads
your Gradescope account — the same read-only student surface as
[bruglet/gradescope-mcp](https://github.com/bruglet/gradescope-mcp):
`list_courses`, `list_assignments`, `list_submissions`, `get_submission`,
`list_regrade_requests` — and syncs homework deadlines to Google Calendar via
the `google_calendar` connector.

## How it works

- `gradescope_client.py` logs in to gradescope.com with the account's
  email/password form (the only non-GET is the required `POST /login`), keeps
  cookies in memory, enforces same-origin requests and bounded redirects, and
  throttles to 1 request/second. Parsers live in `gradescope_parsers.py`,
  `gradescope_courses.py`, `gradescope_assignments.py`,
  `gradescope_submissions.py`, `gradescope_detail.py`.
- `main.py` defines the `gradescope-homework-sync` agent: five read-only
  Gradescope tools plus five Google Calendar tools (`gcal.py`). Calendar writes
  go through the connectors API
  (`POST /v1/connectors/google_calendar/tools/<tool>/call?credentials_name=homework-sync`)
  authenticated with a Mistral API key of the connector owner, because the
  deployment's service account cannot use user-scope OAuth credentials. Three
  workspace secrets are bound at deploy time: `gradescope_email`,
  `gradescope_password`, `mistral_calendar_api_key`.
- Sync behavior: for every released assignment with a due date, one event
  `[GS] <course> - <assignment> (due)` at the due time (all-day if timeless),
  plus a `(LATE deadline)` event when a distinct late deadline exists.
  Existing `[GS]` events are matched by title and only updated when changed —
  runs are idempotent.

## Assignment file export (branch `feature/assignment-file-export`)

`export_assignment_files_to_drive` downloads professor-provided assignment
files linked on current-term course pages (usually the provided PDF, served
via Gradescope's time-limited signed S3 URLs) and uploads them to Google Drive
under a root folder ("Gradescope Assignments") with one subfolder per course.
Idempotent: files already present in the target folder are skipped, never
overwritten. Setup: add a Google Drive connection named `homework-sync` in
Studio next to the calendar one — the worker reaches it through the connectors
API with `mistral_calendar_api_key`, same as the calendar. Downloads are
https-only and restricted to gradescope.com and its signed uploads host; the
Gradescope session cookie is never sent off-origin; files are capped at 100 MB.

## Deploy

The managed deployment builds this repo's `Dockerfile` from the pushed commit:

- Studio → Secrets Manager: create `gradescope_email`, `gradescope_password`,
  and `mistral_calendar_api_key` (a Mistral API key of the account that owns
  the `homework-sync` Google Calendar connection; connector access scope
  "Private and shared connectors").
- Studio → Connectors → Google Calendar → Credentials: a user connection named
  `homework-sync`.
- Deployment → Settings tab: bind the three secrets to env vars of the same
  name, then deploy with the Mistral MCP `deploy_workflow_deployment` (or
  `uv run --env-file .env agents deploy`).
- A workflow schedule runs it daily at 08:00 America/Indiana/Indianapolis.

## Interact

- Chat: Vibe Code Web link printed by the deploy
  (`https://chat.mistral.ai/managed-agents/gradescope-homework-sync?source=agents_api`),
  or `uv run --env-file .env agents chat gradescope-homework-sync`.
- From Vibe (this CLI): trigger a run with the Mistral MCP
  `execute_workflow` tool (`workflow_identifier="gradescope-homework-sync"`,
  input `{"message": "Sync my Gradescope homework to my calendar now."}`), then
  read the result with `get_workflow_execution`. Sessions/traces appear in
  Studio → Managed Agents.

## Tests

`python3 -m uv run pytest` — parser tests run against the HTML fixtures copied
from the reference repository (`tests/fixtures/`).

Gradescope's pages and undocumented endpoints may change, and scraping may be
against Gradescope's Terms of Service; use only with an account you are
authorized to use. Never commit credentials or cookies.
