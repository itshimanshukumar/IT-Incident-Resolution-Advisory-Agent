# ResolveIT — IT Incident Resolution Advisory Agent

This project retains the original agent API and adds the existing ResolveIT Employee Portal and IT Staff Portal. The UI is served by the same FastAPI application, so both portals use same-origin `/api/*` endpoints.

## Run locally

Use Python 3.10 or newer. From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn agent.main:app --reload --host 127.0.0.1 --port 8000
```

Open:

- Employee Portal: `http://127.0.0.1:8000/employee-ui/`
- IT Staff Portal: `http://127.0.0.1:8000/it_department/` (or `/it`)
- API docs: `http://127.0.0.1:8000/docs`
- Original API health: `http://127.0.0.1:8000/health`
- UI API health: `http://127.0.0.1:8000/api/health`

The on-disk employee frontend directory is named **`employee ui`** as requested. It is mounted at `/employee-ui` to avoid spaces in browser URLs.

## Demo portal accounts

These development accounts are seeded automatically into the UI's local SQLite database on first startup:

- Employee: `E1001` / `Employee@123`
- Employee: `E1002` / `Employee@123`
- IT staff: `IT1001` / `Admin@123`

Change demo credentials and configure a strong `JWT_SECRET_KEY` before any non-demo deployment. Never commit `.env` or real API keys.

## Integration design

- The original endpoints (`/check-outage`, `/search-incidents`, `/escalation-rule`, `/tickets`, `/troubleshooting`, `/resolve-incident`, and `/sessions/*`) are retained.
- The UI's existing `/api/*` handlers are attached to the same FastAPI app for login, chat, tickets, dashboard, outages, audit logs, and guide management.
- The UI backend's SQLite database and CSV/guide assets live under `data/ui_data/`. This isolation prevents the UI ticket schema and UI workflows from overwriting the original agent's CSV data or session files.
- The UI backend enforces signed-token authentication and server-side role checks for IT administration. Client-supplied role headers are not trusted.
- The UI chat backend uses Groq or Gemini if configured. Without a provider API key, deterministic/local behavior remains available for supported paths, including matching active-outage handling.

## Tests

Run the deterministic test suite from the project root:

```powershell
python -m pytest -q
```

The integration tests check that the original API still works, both portals are served, the UI API is reachable, portal role separation is enforced, unauthenticated administrative actions are denied, and the chat UI API returns the active-outage response for the seeded Chennai Wi-Fi outage.
