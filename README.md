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
  Gradescope tools, the `google_calendar` connector
  (`run_as="deployment"`, `credentials_name="homework-sync"` so scheduled runs
  work without a signed-in user), and two workspace secrets
  (`gradescope_email`, `gradescope_password`) bound to
  `GRADESCOPE_EMAIL` / `GRADESCOPE_PASSWORD`.
- Sync behavior: for every released assignment with a due date, one event
  `[GS] <course> - <assignment> (due)` at the due time (all-day if timeless),
  plus a `(LATE deadline)` event when a distinct late deadline exists.
  Existing `[GS]` events are matched by title and only updated when changed —
  runs are idempotent.

## Deploy

The managed deployment builds this repo's `Dockerfile` from the pushed commit:

- Studio → Secrets Manager: create `gradescope_email` and `gradescope_password`.
- Studio → Connectors → Google Calendar → Credentials: a workspace-shared
  connection named `homework-sync`.
- Deploy with the Mistral MCP `deploy_workflow_deployment` (or
  `uv run --env-file .env agents deploy`), binding the two secrets.
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
