# 算法运行平台（FastAPI + Vue 3）——真实甘蔗单产持续估产

前端上传/选择参数文件 → 后端运行真实算法（predicted26）→ 生成结果传回前端展示与下载。
当前平台只保留一个真实算法：**广西甘蔗单产持续估产26**。

## 一、总体架构

```
frontend (Vue 3 + Vite + Element Plus，端口 5173)
   │  ① 选算法 / ② 上传或选择参数文件 / ③ 点运行
   ▼  HTTP（/api 由 Vite 代理到 8000）
backend (FastAPI，端口 8000)
   ├─ app/routers/        接口：算法列表、数据列表、文件上传、运行、结果下载
   ├─ app/services/       算法注册表（自动扫描算法文件）、tif→png 预览渲染
   ├─ algorithms/         算法文件夹：predicted26.py + 分块栅格执行器
   ├─ data/               数据文件夹：算法参数文件（前端上传的文件也存这里）
   └─ outputs/{run_id}/   每次运行的结果（tif / png / csv）
```

一次完整运行的调用链：

```
前端 ParamForm 收集参数
  → POST /api/run {algorithm_id: "predicted26", inputs: {excel: "...", ...}}
  → run.py 把“文件名”解析成服务器本地绝对路径（backend/data/ 下）
  → registry 按 algorithm_id 加载 algorithms/predicted26.py
  → 调用 predicted26.run(inputs, workdir)
       ├─ ① 县级表训练 Ridge → 折叠参数（秒级）
       ├─ ② 分块逐像元栅格计算 → 写出估产 tif（约 1-5 分钟）
       └─ ③ 组装 {message, metrics, tables, rasters}
  → run.py 把结果 tif 渲染成 png 预览图，拼上下载链接
  → 前端 ResultPanel 展示指标 / 表格 / 图片，并提供下载
```

## 二、目录结构

```
backend/
├─ app/
│  ├─ main.py               FastAPI 入口（CORS、路由注册）
│  ├─ config.py             路径常量（BASE_DIR / ALGORITHMS_DIR / DATA_DIR / OUTPUTS_DIR）
│  ├─ schemas.py            请求与响应数据模型
│  ├─ routers/
│  │  ├─ algorithms.py      算法列表 / 数据文件列表 / 文件上传
│  │  └─ run.py             运行算法 / 下载结果（含参数校验与路径穿越防护）
│  └─ services/
│     ├─ registry.py        算法注册表：扫描 algorithms/*.py，按需加载
│     └─ result_builder.py  tif → png 预览图渲染
├─ algorithms/
│  ├─ predicted26.py        真实算法：训练→折叠→分块逐像元计算（512×512 窗口，自动重投影）
│  └─ future2.py            GEE 算法：Python-GEE 接口提取 133 波段时序特征（波段_月份命名，免上传 GEE 资产）
├─ data/                    参数文件（建模表 Excel、9 幅月度影像、甘蔗掩膜）
├─ outputs/{run_id}/        每次运行的结果
├─ scripts/
│  ├─ test_real_algo.py     不启动后端，直接调用算法 run() 验证全流程
│  └─ test_api.py           启动后端后，验证全部 HTTP 接口
└─ requirements.txt

frontend/
├─ src/
│  ├─ api/index.js          axios 封装：算法列表 / 数据列表 / 上传 / 运行
│  ├─ App.vue               页面骨架：选算法 → 参数表单 → 结果面板
│  └─ components/
│     ├─ AlgorithmSelector.vue   算法下拉选择 + 参数说明
│     ├─ ParamForm.vue           参数表单（选文件 / 上传文件 / 选月份）
│     └─ ResultPanel.vue         结果展示（指标卡片、表格、tif 预览与下载）
└─ vite.config.js           /api 代理到 http://127.0.0.1:8000
```

## 三、启动方式

**后端**（Python 3.11，依赖已在 `backend/venv`）：

```bash
cd backend
venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- 接口文档（Swagger）：http://127.0.0.1:8000/docs

**前端**：

```bash
cd frontend
npm install   # 首次
npm run dev   # http://localhost:5173（/api 自动代理到 8000）
```

## 四、核心机制讲解（学习重点）

### 1. 算法注册表：为什么加一个算法文件就能用

`app/services/registry.py` 启动时扫描 `algorithms/*.py`（跳过 `_` 开头的辅助文件），
每个算法文件只要符合两个约定就会被平台自动识别，**不需要改任何平台代码**：

```python
ALGO_META = {                     # ① 参数声明：前端据此动态生成表单
    "id": "predicted26",          #    id 必须等于文件名
    "name": "广西甘蔗单产持续估产",
    "description": "...",
    "params": [
        {"name": "excel", "label": "县级建模表 Excel", "type": "xlsx", "required": True},
        {"name": "cane_mask", "label": "甘蔗掩膜 tif", "type": "tif", "required": False},
        {"name": "target_month", "label": "目标月份", "type": "month", "default": "2026-06"},
    ],
}

def run(inputs: dict, workdir: Path) -> dict:   # ② 运行入口
    ...
```

- `type` 决定前端渲染什么控件：`csv/tif/xlsx/json` → 文件下拉 + 上传按钮；`month` → 月份选择器；
  `text` → 单行输入框；`number` → 数值输入框。
- `required=False` 的参数不选也能运行；`default` 用于预填表单。
- 标量参数（month/text/number）由 `run.py` 校验格式后原样（number 转 float）传给算法，
  文件参数则解析为 `backend/data/` 下的服务器绝对路径。
- 算法抛出的异常会被平台捕获并转成中文错误消息返回给前端。

### 2. 参数文件如何从浏览器到达算法（回答“本地路径要不要改”）

**本地跑脚本时**，`predicted26.py` 里写死的是开发机绝对路径：

```python
INPUT_FILE = Path("D:/postgraduate_0/algorithm_review/数据代码封装/.../GX_....xlsx")
```

**部署成 Web 服务后必须改掉**，原因是：浏览器只能传“文件名”，真正的文件必须已经在
服务器磁盘上，否则多台电脑访问就会去别人电脑上找文件。改造方式是两层：

- **第一层（前端）**：表单提交的是文件名（如 `"月度影像_2026/2026_04_NDVI.tif"`），
  由 `app/routers/run.py` 解析成服务器端路径，并做校验（拒绝 `..` 穿越、检查文件存在与类型）。
- **第二层（算法）**：`run()` 收到的 `inputs` 已经是服务器上的绝对路径 `Path`，
  算法内部一律使用 `inputs` 传入的路径，不再读全局写死的绝对路径。
  文件里的 `INPUT_FILE` 等默认值也改成了**相对本文件位置推导**的路径
  （`Path(__file__).resolve().parents[1] / "data" / ...`），
  这样换一台机器部署、换个盘符都不用改代码。

### 3. 文件上传是怎么实现的

- 后端 `POST /api/upload`：接收 `multipart/form-data`（`file` + 可选 `folder`），
  校验后缀（只允许 csv/tif/tiff/xlsx）、校验子目录（必须在 `backend/data/` 内），
  保存后返回 `{name, type, size_mb}`。
- 前端 `ParamForm.vue`：每个文件参数旁有「上传文件」按钮和可选子目录输入框，
  上传成功后把返回的 `name` 填进表单，并通知 App 刷新文件下拉列表。
- 上传和选择等价：上传只是把文件放进 `backend/data/`，后续运行流程完全一样。

### 4. predicted26.run() 内部做了什么（真实算法流程）

`algorithms/predicted26.py` 底部的 `run()` 是算法与平台的唯一接口，分三步：

```python
def run(inputs, workdir):
    # ① 县级建模：读 Excel → 按榨季聚合样本 → RidgeCV 训练
    #    → 把县级历史项（历史单产/面积）折叠进截距（因为 GEE/栅格无法逐像元算历史）
    model = run_folded_formula_workflow(args)      # 秒级，产出 folded 参数表
    folded = model["folded_full"]

    # ② 分块栅格计算：真实影像 28240×20358（单幅约 2.3GB）不能整幅进内存，
    #    按 512×512 窗口逐块读 9 幅月度影像 → 构造特征 → 线性公式预测 → 写 tif
    stats = write_window_tif_outputs(...)          # 约 1-5 分钟
    # 线性公式：Final = (folded_intercept + Σ coef_i × pixel_feature_i) × FINAL_RESULT_SCALE

    # ③ 组装平台返回结构
    return {"message": ..., "metrics": [...], "tables": [...], "rasters": [...]}
```

- **训练口径**：2020-2024 榨季训练，目标 2026-06（目标榨季不进入训练，避免数据泄露）。
- **影像自动发现**：选“样例”一张 tif（如 `2026_04_NDVI.tif`），算法按目标月份在该
  目录自动查找 `{年}_{月:02d}_{变量}.tif` 命名的全部影像（NDVI/气温/降水 × Apr~Jun 共 9 幅）。
- **网格不一致**：气温/降水是 77×55 粗网格，掩膜是 84718×59217，执行器用
  WarpedVRT 按窗口延迟重投影到 NDVI 参考网格（不需要预先手动对齐）。
- **结果含义**：原始模型结果单位 t/ha，乘 `FINAL_RESULT_SCALE=0.055` 得到最终 t/亩。

### 5. 结果如何传回前端

算法返回三样东西，由 `run.py` 加工后交给前端：

| 算法产出 | 平台加工 | 前端展示 |
|---|---|---|
| `metrics`（float 指标） | 原样返回 | 指标卡片（数值 + 单位 + 小数位） |
| `tables`（表格 + 可选 csv 文件） | 生成 `/api/outputs/{run_id}/xx.csv` 下载链接 | 表格展示 + 下载 CSV |
| `rasters`（结果 tif 路径） | 用 matplotlib 渲染成 png 预览图 | 图片预览 + 下载 TIF |

结果文件全部写在 `outputs/{run_id}/`（每次运行独立目录），下载接口带
run_id 格式校验与路径穿越防护。

## 五、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/algorithms` | 算法列表（含参数定义，前端据此生成表单） |
| GET | `/api/data` | 数据文件夹文件列表 |
| POST | `/api/upload` | 上传文件（multipart：`file` + 可选 `folder`） |
| POST | `/api/run` | `{algorithm_id, inputs: {参数名: 文件名}}`，同步运行并返回完整结果 |
| GET | `/api/outputs/{run_id}/{filename}` | 下载/预览结果文件 |
| GET | `/health` | 健康检查 |

## 六、验证方式

不启动后端，直接验证算法全流程（分块计算约 1-5 分钟）：

```bash
cd backend
venv/Scripts/python scripts/test_real_algo.py
```

启动后端后验证全部接口（含上传、运行、下载、错误用例）：

```bash
venv/Scripts/python scripts/test_api.py
```

## 七、接入新算法（核心规范）

在 `backend/algorithms/` 下新增一个 .py 文件即可，**重启后端生效，无需改动其他代码**：

```python
ALGO_META = {
    "id": "my_algo",                      # 等于文件名
    "name": "我的算法",
    "description": "算法说明（显示在前端）。",
    "params": [                            # 前端据此动态生成表单
        {"name": "input_csv", "label": "输入数据", "type": "csv", "required": True},
        {"name": "input_tif", "label": "输入影像", "type": "tif", "required": False},
        {"name": "target_month", "label": "预测月份", "type": "month", "default": "2026-06"},
    ],
}

def run(inputs: dict, workdir: Path) -> dict:
    """inputs = {参数名: data/ 下的服务器绝对路径 或 月份字符串}；
    workdir = outputs/{run_id}/。返回：
    {
      "message": "运行说明",
      "metrics": [{"name": "指标名", "value": 1.23, "unit": "t/亩", "decimals": 3}],
      "tables":  [{"name": "表名", "columns": [...], "rows": [[...], ...],
                   "file": workdir / "结果表.csv"}],      # file 可选，提供后前端可下载
      "rasters": [{"name": "图名", "tif": workdir / "结果.tif"}],  # 后端自动渲染 png 预览
    }
    """
```

约定：
- csv 用 pandas 写出，编码 `utf-8-sig`（Excel 兼容）；tif 用 rasterio 写单波段 float32
- 表格 rows 中 NaN 需转 None、numpy 标量转原生类型（参考 predicted26 的 `_json_rows`）
- 结果 tif/png/csv 写在 `workdir` 下（文件名即前端展示与下载的文件名）
- 算法抛异常时前端会显示对应错误消息
- 数据文件放 `backend/data/`（可上传、可手放），前端下拉列表自动出现

## 八、GEE 算法接入说明（future2.py 与 predicted26.py 的区别）

`future2.py` 用 Python-GEE 接口把计算放在 Earth Engine 服务器端，和本地算法有几处关键差异，
也是从原生脚本改造为平台算法的要点（原生脚本的算法核心：月度合成、指数、归一化、
年际中值填补、{波段}_{月份} 命名均原样保留，只在其外围做平台化适配）：

### 1. 没有"本地文件路径"，参数是"GEE 资产/项目 + 样本 CSV"

predicted26 的参数是服务器本地文件（Excel、tif）；GEE 算法的数据在云端，
本地需要的只有**样本点表**。改造后算法参数为：样本 CSV、标签列名、年份、
云量阈值、采样分辨率、GEE 项目 ID、可选服务账号 JSON、可选已有资产 ID。

### 2. 鉴权不能放在模块顶部，必须放在 run() 内

原生脚本顶部有 `ee.Authenticate()`（会打开浏览器）和 `ee.Initialize(...)`。
平台注册表在**每次列出算法时都会执行模块代码**，模块级鉴权会让
`GET /algorithms` 卡死。改造后 `init_gee()` 在 run() 内按需执行：
本机已有 `earthengine authenticate` 登录时直接复用（本项目开发机已登录，
默认零配置可用）；部署到服务器时上传服务账号 JSON（新增 `json` 参数类型）。

### 3. 无需把 CSV 上传成 GEE 表资产（核心优化）

原生脚本要求先手动把坐标文件上传为 `projects/.../assets/table` 再引用。
改造后 `parse_sample_csv()` / `features_from_dataframe()` 把 CSV 在**客户端
直接构造成 ee.FeatureCollection**，随计算请求一起发给 GEE（15000 点约 3MB，
远低于请求上限），完全免去上传资产这一步骤；同时保留「已有资产 ID」参数
供已上传的用户直接引用。

### 4. 不用 Drive 导出，改用同步 getInfo + 块级 AOI 构图 + 并行提取（内存修复核心）

原生脚本用 `Export.table.toDrive` 异步导出（批量任务的内存限制宽松，所以
原生脚本"能用"）。但平台要在一个 HTTP 请求内返回结果，只能改用
`sampleRegions().getInfo()` 同步取回——**交互式 getInfo 有 8GB 用户内存限制，
直接同步取回在真实研究区必报 `User memory limit exceeded`**。经隔离实验定位
（实验脚本见 `scripts/tmp_memtest.py`、`tmp_fctest.py`、`tmp_extent_test.py`）：

- **与点数、波段数、tileScale 都无关**：用全样本 AOI 构的图，哪怕只采 250 点、
  只取 1 个月 11 个波段也失败；
- **元凶是影像的 AOI**：GEE 按影像构图区域物化影像集/瓦片，全研究区 133 波段
  远超 8GB；
- **解法是每块独立构图**：每块用自己块级的小 AOI 调用 `buildFeatureImage()`
  构图，每次求值只物化局部瓦片（实测 0.75° 范围 2000 点 133 波段稳定通过）。

因此提取架构（`build_spatial_chunks` / `extract_chunk` / `extract_all_features`）：

- 样本点落到 0.25° 网格单元，合并成「点数 ≤2000 且经纬度跨度 ≤0.5°」的
  空间块（跨度上限是内存安全的关键约束；15000 点 → 110 块，本地已验证）；
- 每块用**块级 AOI 独立构图** + 客户端只构造块内的点 FC（不再 inList 过滤），
  再 `sampleRegions().getInfo()` 同步取回；
- 8 线程并行提取 110 块，单块失败自动重试一次，总时长几分钟；
- 结果按 point_id 合并回原始 CSV 表。

预览图改用 `getDownloadURL` 直接下载小尺寸 NDVI 合成 tif（最长边约 2000 像元），
同样不依赖 Drive；失败时自动降级跳过，不影响主结果。

### 5. 空影像月份的占位波段（健壮性修复）

原生脚本假设 12 个月都有 Sentinel-1/2 数据，某月数据缺失时（如该区域当月
无 S1 VV/VH 影像）`median()` 返回无波段影像，后续 `select('VH')` 会报
`Band pattern did not match`。改造后用 `empty_named_image()` 为缺失月份
构造**名称齐全、值全为空掩膜**的占位波段：12 个月的堆叠结构永远稳定，
缺失月份随后被 `unmask(annual_median)` 用全年中值填补，或保留为 null
（明确表示"该月无观测"，对下游建模更友好）。

### 6. 错误可见性

- 算法执行失败时，`run.py` 会把**完整堆栈打印到服务器控制台**（前端 toast
  只显示摘要），终端里可直接看到根因。
- 注册表按文件修改时间自动重载算法模块，并打印跳过原因
  （缺少 ALGO_META / 缺少 run()），不会出现"文件已改好但服务器还用旧内容"的困惑。

输出：`样本特征表.csv`（原始列 + 133 波段）、`波段有效性统计.csv`、
全年 NDVI 预览 tif；Web 端表格只回传前 300 行，完整数据走 CSV 下载
（避免 15000×136 的大 JSON 拖垮浏览器）。
