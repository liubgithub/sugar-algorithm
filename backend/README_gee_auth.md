# GEE 认证与项目不匹配的故障处理

本项目所有调用 Google Earth Engine (`earthengine-api`) 的算法（`crop_threshold`、`crop_classification`、`crop_features`、`harvest`）通过 `app.services.gee_auth.initialize_ee` 统一管理 GEE 初始化。

## 为什么要单独管理

`ee.Initialize(project=...)` 看似接受任意 project，**实际不会重新做 OAuth**：本机的 `~/.config/earthengine/credentials` 里记录了首次认证时绑定好的 `quota_project`，后续只能改 OAuth scope 才能换项目。否则 GEE 后端会返回：

```
Caller does not have required permission to use project <your-project>.
Grant the caller the roles/serviceusage.serviceUsageConsumer role …
```

## 设计原则：后端不自动认证

**FastAPI 进程不能、也不应自行调用 `ee.Authenticate(force=True)` 或 `gcloud auth application-default login`**：

- 服务端通常没有交互式终端、键盘和显示器，浏览器 OAuth 流程无处发起。
- `gcloud auth application-default login --project=<p>` 在后台进程里运行，用户既看不到 URL 也无法完成授权。
- 即使能驱动，凭据文件属于"操作 GEE 的那个人的账号"，而 OAuth 必须在**该用户实际使用的工作站**上完成，与 FastAPI 进程无关。

所以 `gee_auth.py` 的职责只有一条：**检测项目不匹配并抛出明确的错误**，由前端提示用户回本机去重新认证。

## 端到端流程

1. **用户**启动 FastAPI（开发机/服务器）。
2. **用户**在算法表单里填了某个 GEE Project ID（例如 `sodium-ray-505904-i3`）。
3. **后端**`initialize_ee(project)` 检测到 `_CURRENT_PROJECT` 与传入 project 不一致，调用 `ee.Initialize(project=...)`，由 earthengine-api 把后端的 OAuth scope 错误冒出来。
4. **后端**捕获后包装成 `GeeAuthRequired(project)`，消息包含：
   ```
   当前 GEE 认证与请求项目 'sodium-ray-505904-i3' 不匹配。
   请在本机执行：

       earthengine authenticate --force

   完成后重新运行本算法即可。
   ```
5. **算法**捕获 `GeeAuthRequired` 后返回 `make_result(status="failed", message=str(exc))`，任务标 `failed`。
6. **前端**JobDetailView 在 `failed` 状态下渲染红色预格式化块 + "复制命令"按钮（点击复制 `earthengine authenticate --force`）。
7. **用户**在本机终端粘贴运行 → 浏览器打开 OAuth 同意页 → 同意 → 凭据文件以新 project 重写。
8. **用户**回到页面重新提交算法 → `initialize_ee` 看到 `_CURRENT_PROJECT` 是 `None`（之前失败时没缓存），再次 `ee.Initialize(project=...)` 顺利成功。

## 服务账号的特殊路径

如果设置了环境变量 `EE_SERVICE_ACCOUNT_FILE=<path-to-sa.json>`，`initialize_ee` 会走服务账号分支：

- 直接 `ee.Initialize(credentials, project=project)`，**不需要浏览器 OAuth**。
- 服务账号可以被授权访问任意 project，所以"project 不匹配"在这种情况下不是问题——只要 SA 本身被授权了目标 project 即可。

这是生产/无头环境的推荐配置。

## 代码位置

| 文件 | 角色 |
| --- | --- |
| `backend/app/services/gee_auth.py` | `initialize_ee()`、`GeeAuthRequired`、`is_auth_mismatch_error()` |
| `backend/app/algorithms/{crop_threshold,crop_classification,crop_features,harvest}/algorithm.py` | 每个 `run()` 都在 `_initialize_ee` 周围捕获 `GeeAuthRequired` 并返回失败 result |
| `backend/app/services/job_service.py:_query_gee_status` | 任务状态轮询也走 `initialize_ee`，确保出错时安静降级到 `None` 而不影响任务状态显示 |
| `frontend/src/views/JobDetailView.vue` | 渲染失败卡片中的中文指引 + "复制命令"按钮 |

## 常见问题

### Q: 我已经在本机跑过 `earthengine authenticate` 了，为什么还报不匹配？
A: 检查是否带上了 `--force`。没 `--force` 时 earthengine-api 会看到已有合法凭据直接复用，**不会重新授权新 project**。

### Q: 我可以加一个 "Auto re-auth" 按钮让后端替我跑吗？
A: 不行。原因见上文"设计原则"。

### Q: 服务账号怎么配置？
A: 在 GCP 控制台创建 Service Account → 下载 JSON → 设置 `EE_SERVICE_ACCOUNT_FILE=/abs/path/to/sa.json` → 在目标 project 里给该 SA 授予 `Service Usage Consumer` 角色 → 重启 FastAPI。

### Q: 为什么错误消息里有换行？
A: 前端把 `job.message` 渲染在 `<pre>` 块里保留换行；外加"复制命令"按钮一键复制最关键的那行。