# Backend (FastAPI)

Phase 1 MVP for the Sugarcane Remote Sensing Platform.

## Start

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Or run the helper script:

- Linux / macOS: `./run.sh`
- Windows: `run.bat`

The service listens on `http://127.0.0.1:8000`.

## Endpoints (Phase 1)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness probe |
| GET | `/api/algorithms` | List registered algorithms |
| GET | `/api/algorithms/{id}` | Inspect one algorithm |
| POST | `/api/jobs` | Create a job for an algorithm |
| GET | `/api/jobs` | List jobs (debug helper) |
| GET | `/api/jobs/{job_id}` | Poll job status |

## Layout

```
app/
  main.py                 # FastAPI app factory + middleware + routes
  api/                    # API layer (FastAPI routers)
  schemas/                # Pydantic request/response models
  services/               # Service layer (job orchestration, registry facade)
  algorithms/             # Algorithm layer (one subpackage per algorithm)
    base.py               # Abstract base + result helper
    registry.py           # In-memory registry + bootstrap
    yield_local/          # LOCAL stub
    harvest/              # GEE stub
    crop_threshold/       # GEE stub
    crop_features/        # GEE stub
    crop_classification/  # GEE stub
    algorithm_6/          # empty placeholder
    demo/                 # working demo that validates the full flow
  storage/                # SQLite-backed job store
  core/                   # config
data/
  sugarcane.db            # SQLite database (auto-created)
  jobs/<job_id>/          # Per-job working directory
  results/                # Reserved for exported result artifacts
```