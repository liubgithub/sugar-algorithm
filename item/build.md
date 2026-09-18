####
你现在负责开发一个“甘蔗遥感算法 Web 平台”。

技术栈：

* 前端：Vue 3 + Vite + Axios + Vue Router + Pinia
* 地图：OpenLayers
* 后端：Python FastAPI
* 算法：现有 Python 算法，不允许擅自改变核心数学逻辑
* GEE：Python Earth Engine API
* 本地栅格：Rasterio
* 数据存储：第一阶段使用 SQLite + 本地文件目录
* 不引入 Docker、Celery、Redis 等复杂基础设施，先完成可运行 MVP

核心业务：

Vue 前端负责：

1. 选择算法
2. 动态显示该算法需要的参数
3. 上传本地文件
4. 提交运行任务
5. 显示任务状态
6. 获取结果
7. GeoTIFF 在 OpenLayers 中展示
8. CSV/JSON/GeoTIFF 提供下载
9. 分类算法展示 OA、Kappa、混淆矩阵等结果

FastAPI 后端负责：

1. 算法列表
2. 参数定义
3. 文件上传
4. 参数校验
5. 创建 Job
6. 启动算法
7. 管理 Job 状态
8. 管理结果文件
9. GEE Task 状态查询
10. 返回结果给 Vue

重要架构原则：

不要把算法代码直接写进 FastAPI 路由。

采用：

API Layer
→ Service Layer
→ Algorithm Layer
→ Result Layer

所有算法统一提供：

run(params, job_dir)

每个算法必须返回统一结果结构，例如：

{
"status": "submitted",
"result_type": "raster|csv|json|metrics|gee_task",
"files": [],
"metrics": {},
"gee_task_id": null,
"message": ""
}

建立 Algorithm Registry，使前端通过：

GET /api/algorithms

获取算法列表。

目前算法包括：

1. 本地持续估产2
   类型：LOCAL
   特点：

* 不依赖 GEE 运行
* Excel、月度 TIF、参数等输入在本地
* 使用 pandas、sklearn、rasterio
* 生成本地 GeoTIFF
* 保留现有 RidgeCV、StandardScaler、SimpleImputer、历史县级特征折叠、动态特征、面积加权和 FINAL_RESULT_SCALE 逻辑
* 支持大栅格分块计算
* 不允许把它改成 GEE 算法

2. 甘蔗收割
   类型：GEE
   特点：

* Python 调用 Earth Engine API
* Sentinel-1
* RVI、NDPI、Texture
* 基期和监测期
* RVI_ABSOLUTE_LOW
* RVI_DROP_MIN
* 使用甘蔗掩膜
* Export.image.toDrive
* task.start()
* 后端提交 GEE Task 后立即返回 task ID，不等待大型 getInfo

3. 甘蔗分类-获取阈值
   类型：GEE
   特点：

* Sentinel-2 + Sentinel-1 + NASADEM
* 计算 B2、B3、B4、B8、B11、B12、NDVI、EVI、NDWI、VV、VH
* percentile 2/98
* 获取 DEM min/max
* 返回 JSON 阈值结果
* 仅允许对少量最终数值使用 getInfo

4. 甘蔗分类-生成特征
   类型：GEE
   特点：

* 根据年份、研究区、样本点生成月度特征
* Sentinel-2 + Sentinel-1
* 12个月时序
* 归一化
* 缺失值填补
* DEM
* reduceRegions
* Export.table.toDrive
* 输出 CSV
* task.start() 后立即返回 GEE task ID
* 不要在任务提交后执行大规模 getInfo 检查

5. 甘蔗分类-分类
   类型：GEE
   特点：

* 使用生成的特征
* Random Forest
* 训练/验证划分
* OA、Kappa、混淆矩阵
* 全图分类
* Export.image.toDrive
* 返回 GEE task ID 和可获得的精度指标

6. 第六个算法
   先建立空的 Algorithm Adapter，不猜测算法逻辑，等待接入真实 Python 文件。

任务机制：

所有耗时算法必须采用异步 Job 模式。

POST /api/jobs

返回：

{
"job_id": "...",
"status": "queued"
}

GET /api/jobs/{job_id}

返回：

{
"job_id": "...",
"status": "queued|running|submitted|completed|failed",
"progress": 0,
"message": "...",
"result": {}
}

对于 LOCAL 算法：
FastAPI 启动独立后台任务/子进程执行 Python 算法，避免阻塞 HTTP 请求。

对于 GEE 算法：
Python 创建 GEE Export Task → task.start() → 保存 gee_task_id → 后端后续查询任务状态。

绝对禁止：

* 在 HTTP 请求中同步等待长时间 GEE 计算
* 在 GEE 大任务后调用大量 getInfo
* 把整个算法复制到 main.py
* 修改算法数学逻辑
* 擅自更换 GEE 数据集
* 擅自修改波段
* 擅自修改阈值
* 擅自修改模型参数
* 擅自删除已有分块栅格计算
* 为了“优化代码”而改变算法结果

目录要求：

backend/
app/
main.py
api/
schemas/
services/
algorithms/
yield_local/
harvest/
crop_threshold/
crop_features/
crop_classification/
algorithm_6/
storage/
core/

frontend/
src/
api/
views/
components/
stores/
router/

先不要一次性实现全部功能。

第一阶段只完成：

1. 项目目录
2. FastAPI 基础服务
3. Vue 基础页面
4. Algorithm Registry
5. Job 数据模型
6. GET /api/algorithms
7. POST /api/jobs
8. GET /api/jobs/{job_id}
9. 一个假的 demo algorithm 验证完整任务流程

完成第一阶段后停止，不要继续开发后续功能。

代码要求：

* 中文注释只写必要内容
* 变量名使用英文
* API 使用 REST 风格
* Pydantic 负责参数验证
* Axios 统一封装
* 不重复代码
* 不建立不必要的抽象
* 保持 MVP 简单
* 每完成一个阶段必须先保证项目能够启动
* 如果已有代码与当前架构冲突，优先适配已有算法，而不是重写算法
* 修改文件前先检查现有文件，不要凭空创建重复实现

现在只执行“第一阶段”，完成后告诉我：

1. 创建了哪些文件
2. 每个文件作用
3. 如何启动后端
4. 如何启动前端
5. 如何测试 API
6. 下一阶段需要做什么

不要提前实现第二阶段。
####
现在进入第二阶段：只接入“本地持续估产2”。

不要修改其他算法。

目标：

将现有“本地持续估产2.py”封装成：

backend/app/algorithms/yield_local/algorithm.py

要求：

1. 保留原算法计算逻辑。

2. 将原来的脚本入口改造成：

   run(params, job_dir)

3. 参数由 FastAPI 传入，不再依赖硬编码运行参数。

4. 支持：

   * Excel 输入文件
   * 月度 GeoTIFF 输入
   * 甘蔗掩膜
   * target_month
   * train_start_year
   * train_end_year
   * final_result_scale

5. 不使用 GEE API 执行该算法。

6. 保留 Rasterio 分块读取。

7. 保留现有特征构建逻辑。

8. 保留现有模型参数还原逻辑。

9. 保留现有历史县级项折叠逻辑。

10. 保留 GeoTIFF 输出。

结果统一返回：

{
"status": "completed",
"result_type": "raster",
"files": [
{
"name": "...tif",
"url": "..."
}
],
"metrics": {},
"message": "..."
}

增加：

GET /api/algorithms/yield_local

返回该算法参数定义。

前端生成参数表单。

运行过程：

Vue
→ POST /api/jobs
→ FastAPI 创建 job
→ 后台启动本地算法
→ job 状态 running
→ 算法完成
→ 保存 GeoTIFF
→ job 状态 completed
→ Vue 查询 job
→ 显示结果

先不要做地图。

先验证：

1. API 能创建任务
2. Python 算法能运行
3. 能生成 GeoTIFF
4. 前端能看到 completed
5. 前端能下载 GeoTIFF

完成后停止。
###
现在只接入“甘蔗收割”算法。

不要修改本地持续估产2。

将现有 shouge Python 代码封装成：

backend/app/algorithms/harvest/algorithm.py

保留原有：

* Sentinel-1
* RVI
* NDPI
* Texture
* baseStart/baseEnd
* currentStart/currentEnd
* ROI
* sugarcane mask
* RVI_ABSOLUTE_LOW
* RVI_DROP_MIN
* Export.image.toDrive
* scale
* crs
* maxPixels

将参数改成 params。

核心流程：

run(params, job_dir)
→ ee.Initialize(...)
→ 构建 GEE Image
→ Export.image.toDrive(...)
→ task.start()
→ 返回 task.id

不要等待 GEE 任务完成。

不要在任务提交后执行大规模 getInfo。

返回：

{
"status": "submitted",
"result_type": "gee_task",
"gee_task_id": "...",
"message": "GEE导出任务已提交"
}

实现：

GET /api/jobs/{job_id}

如果 job 有 gee_task_id，则查询 GEE Task 状态并转换成：

queued
running
completed
failed

如果 completed，再读取/确认对应结果文件。

先不要实现地图。

只验证：

1. Vue提交参数
2. FastAPI创建job
3. GEE task成功创建
4. 返回task ID
5. 前端显示GEE任务状态

完成后停止。
###
现在接入甘蔗分类三个独立算法：

1. crop_threshold
2. crop_features
3. crop_classification

三个算法必须保持独立，不要合并成一个算法。

依赖关系：

crop_threshold
↓
保存 threshold JSON
↓
crop_features
↓
生成 CSV
↓
crop_classification
↓
生成分类 GeoTIFF + 精度结果

crop_threshold：

输入：

* year
* roi_asset_id

输出：

* min_values
* max_values
* dem_min
* dem_max

只允许 getInfo 获取最终少量数值。

crop_features：

输入：

* year
* roi_asset_id
* samples_asset_id
* label_property
* threshold JSON

执行：

* Sentinel-2
* Sentinel-1
* 月度特征
* normalization
* missing value filling
* time series stacking
* DEM
* reduceRegions
* Export.table.toDrive

task.start() 后立即返回 GEE task ID。

不要执行 first_feature.toDictionary().getInfo() 等大检查。

crop_classification：

输入：

* feature asset / feature CSV对应的GEE资产
* roi
* label_property
* year
* random forest trees
* train ratio

执行：

* training / validation
* Random Forest
* confusion matrix
* OA
* Kappa
* full classification
* Export.image.toDrive

返回：

* GEE task ID
* OA
* Kappa
* confusion matrix

不要改变现有算法参数和 GEE 数据集。

三个算法分别拥有自己的参数 schema。

完成后停止。
###
现在只实现结果展示。

前端使用 OpenLayers。

目标：

后端完成 GeoTIFF 结果后，前端可以：

1. 查看结果
2. 地图缩放
3. 平移
4. 图层开关
5. 查看像元值
6. 下载原始 GeoTIFF

支持：

* 连续型单产 GeoTIFF
* 二值收割 GeoTIFF
* 分类 GeoTIFF

不要修改算法。

优先采用简单可靠的方案：
后端生成结果统计信息和预览图层；
如果当前环境不适合浏览器直接解析 GeoTIFF，不要强行增加复杂 GIS 服务，先提供 PNG/瓦片预览 + 原始 GeoTIFF 下载。

保持 MVP 简洁。


