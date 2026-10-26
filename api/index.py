"""
Vercel ASGI entry point for ResolveIT FastAPI application.
This module adapts the FastAPI app for Vercel's Python serverless runtime.

Key notes for Vercel deployment:
- SQLite DB is stored in /tmp (ephemeral per-function instance)
- DATA_DIR must point to bundled read-only data files
- sentence-transformers is excluded (too large); keyword search is used instead
"""
import os
import sys
from pathlib import Path

# Ensure project root is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# ---- Vercel environment: redirect writable paths to /tmp ----
# The bundled data files are read-only; SQLite must live in /tmp
_tmp = Path("/tmp/resolveit")
_tmp.mkdir(parents=True, exist_ok=True)

# Only override if not already set (allows local dev to use its own paths)
if not os.environ.get("DB_PATH"):
    os.environ["DB_PATH"] = str(_tmp / "resolveit.db")

if not os.environ.get("DATA_DIR"):
    # Read-only bundled data (CSVs, guides, JSON) from the repo
    os.environ["DATA_DIR"] = str(_project_root / "data" / "ui_data")

# Copy mutable files (outages.csv, tickets.csv) to /tmp on cold start if not present
# so the app can write updates without failing on read-only FS
import shutil

_bundled_data = _project_root / "data" / "ui_data"
_mutable_files = ["outages.csv", "tickets.csv", "incidents.csv", "escalation_rules.json"]
for _fname in _mutable_files:
    _src = _bundled_data / _fname
    _dst = _tmp / _fname
    if _src.exists() and not _dst.exists():
        shutil.copy2(_src, _dst)

# Copy guides directory to /tmp for guide indexer (needs write access for index files)
_guides_src = _bundled_data / "guides"
_guides_dst = _tmp / "guides"
if _guides_src.exists() and not _guides_dst.exists():
    shutil.copytree(_guides_src, _guides_dst)

# Point data dir to /tmp where writable copies live
os.environ["DATA_DIR"] = str(_tmp)

# ---- JWT Secret: required in production ----
if not os.environ.get("JWT_SECRET_KEY"):
    # Vercel will inject this via environment variables dashboard
    # For safety, generate a deterministic dev secret if missing (not for real production)
    import hashlib
    os.environ["JWT_SECRET_KEY"] = hashlib.sha256(
        b"ResolveIT_Vercel_Dev_Secret_2026_AutoGen"
    ).hexdigest()

# ---- Import the main FastAPI application ----
# This triggers DB init, guide indexer load, etc.
from agent.main import app  # noqa: E402

# Vercel expects the ASGI app to be named 'app' or referenced via handler
# The @vercel/python runtime calls app directly as an ASGI handler
