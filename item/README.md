# Sugarcane Remote Sensing Platform (Phase 1 MVP)

甘蔗遥感算法 Web 平台 - 第一阶段 MVP。

## 结构

```
backend/   FastAPI 服务、算法注册表、任务存储
frontend/  Vue 3 + Vite 前端
```

## 启动

### 后端

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

或使用脚本：`./run.sh` (Linux/macOS) / `run.bat` (Windows)。

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173`，通过 vite proxy 把 `/api/*`
转发到后端 `http://127.0.0.1:8000`。

## 第一阶段已完成

- 项目目录结构
- FastAPI 基础服务
- Vue 基础页面
- Algorithm Registry
- Job 数据模型（SQLite）
- `GET /api/algorithms`
- `GET /api/algorithms/{id}`
- `POST /api/jobs`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs`
- 一个 Demo 算法，用于端到端验证任务流程

## 第二阶段计划

1. 文件上传（multipart）API
2. 算法参数更严格的 Pydantic 校验
3. 结果文件下载接口（GeoTIFF / CSV / JSON）
4. OpenLayers 集成展示 GeoTIFF
5. 把真实算法逻辑接入 `yield_local` 等 Adapter（不修改数学逻辑）
6. GEE 任务启动与状态查询接入