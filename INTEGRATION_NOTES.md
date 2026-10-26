# ResolveIT UI Integration Notes

## What changed

1. Added the Employee Portal assets under `employee ui/` (renamed from the original `it_agent_ui` folder) and mounted them at `/employee-ui/`.
2. Added the IT Staff Portal under `it_department/` and mounted it at `/it_department/`.
3. Added the UI backend package under `backend/` and attached its `/api/*` handlers to the existing `agent.main:app` application.
4. Preserved the original agent endpoints and their implementations. The integration only appends new UI routes and static mounts.
5. Isolated UI database and UI CSV/guide data under `data/ui_data/` so UI ticket writes do not overwrite the original project's `data/tickets.csv`, `data/outages.csv`, `data/sessions.csv`, or incident data.
6. Merged required Python dependencies into `requirements.txt` and added `.env.example`, `.gitignore`, and setup instructions in `README.md`.
7. Fixed the Employee Portal's IT ticket-detail request to send its existing bearer token; the API correctly requires verified authentication for ticket details.
8. Added integration tests for legacy endpoint availability, both static portals, login and role separation, admin authorization, ticket-list access control, and chat/outage behavior.

## Verification performed

- Python compilation: passed.
- JavaScript syntax checks (`node --check`): passed.
- Project tests: **20 passed**.
- Local Uvicorn smoke test: `/health`, `/api/health`, `/employee-ui/`, and `/it_department/` returned HTTP 200; `/employee` and `/it` redirect to their respective portals.
- Security smoke tests: forged `x-user-role: it` headers do not grant access to dashboard, audit, outage administration, or guide re-index endpoints.

## Operational note

The UI backend intentionally uses a separate SQLite database in `data/ui_data/resolveit.db`. This preserves the original CSV-based agent backend and avoids destructive schema/data changes. The two backends are exposed by one FastAPI process, but their ticket stores are not automatically synchronized. A later migration to a single shared ticket repository would be a separate, explicitly scoped change.
