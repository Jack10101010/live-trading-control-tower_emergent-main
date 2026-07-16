# Live Trading Control Tower

A read-only operations console for live trading. **React + TypeScript + Vite**
frontend backed by a **FastAPI** service. The backend serves a frozen world
fixture (`backend/fixtures/world.v1.json`) today and runs entirely without a
database.

## Local Development

The project runs locally with standard tooling only — no external platform
dependencies. Two processes: the FastAPI backend and the Vite dev server.

### Backend (FastAPI)

```bash
cd backend
pip install -r requirements.txt
uvicorn server:app --reload
```

- Serves on **http://localhost:8000**
- API is mounted under `/api` — e.g. http://localhost:8000/api/world
- **Fixture mode (default):** with no `MONGO_URL` set, the backend serves
  `backend/fixtures/world.v1.json` and never touches a database. This is the
  normal local mode.
- **Optional Mongo mode:** set `MONGO_URL` (copy `backend/.env.example` to
  `backend/.env`) to point at a running MongoDB instance. If Mongo is
  unreachable the backend logs a warning and stays up in fixture mode — it
  never crashes on a missing or unavailable database.

### Frontend (React + Vite)

```bash
cd frontend
npm install
npm run dev
```

- Serves on **http://localhost:3000**
- With no `.env`, Vite proxies all `/api/*` requests to the backend on
  port 8000, so frontend and backend share an origin (no CORS setup needed).
- To call the backend directly instead of via the proxy, copy
  `frontend/.env.example` to `frontend/.env` and set `REACT_APP_BACKEND_URL`.

### Ports

| Service  | URL                     | Notes                     |
|----------|-------------------------|---------------------------|
| Frontend | http://localhost:3000   | Vite dev server           |
| Backend  | http://localhost:8000   | FastAPI; API under `/api` |

### Quick start (from a clean clone)

```bash
# terminal 1 — backend
cd backend && pip install -r requirements.txt && uvicorn server:app --reload

# terminal 2 — frontend
cd frontend && npm install && npm run dev
```

Then open http://localhost:3000. The app fetches `/api/world` on load and
renders the Fleet Overview.
