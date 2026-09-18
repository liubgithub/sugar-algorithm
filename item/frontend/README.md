# Frontend (Vue 3 + Vite)

Phase 1 MVP for the Sugarcane Remote Sensing Platform.

## Start

```bash
npm install
npm run dev
```

The dev server runs on `http://127.0.0.1:5173` and proxies `/api/*` to the
FastAPI backend at `http://127.0.0.1:8000`.

## Pages

| Route | Purpose |
| --- | --- |
| `/` | Overview and quick-start guide |
| `/algorithms` | List of registered algorithms |
| `/algorithms/:id` | Parameter form + submit a job |
| `/jobs` | List of jobs with status |
| `/jobs/:jobId` | Job status, progress, result |

## Layout

```
src/
  main.js                # app bootstrap
  App.vue                # layout shell
  style.css              # base styles
  router/                # vue-router config
  api/                   # axios wrapper + per-resource modules
  stores/                # Pinia stores (algorithms, jobs)
  views/                 # route components
```