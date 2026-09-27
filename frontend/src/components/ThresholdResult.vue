<template>
  <div class="threshold-result">
    <!-- ① 任务信息 -->
    <div class="card info-card">
      <h3 class="info-card__title">任务信息</h3>
      <div class="info-card__grid">
        <div><span class="info-card__label">计算状态</span><span class="info-card__value">{{ statusLabel }}</span></div>
        <div><span class="info-card__label">GEE 项目</span><span class="info-card__value">{{ geeProject || '—' }}</span></div>
        <div>
          <span class="info-card__label">研究区</span>
          <span class="info-card__value" :title="roiTooltip">{{ roiName }}</span>
          <small v-if="roiLocalName" class="info-card__sub">
            （来自本地文件：{{ roiLocalName }}）
          </small>
        </div>
        <div><span class="info-card__label">数据年份</span><span class="info-card__value">{{ year ?? '—' }}</span></div>
      </div>
      <p v-if="jobMessage" class="info-card__message">{{ jobMessage }}</p>
    </div>

    <!-- ② 动态特征范围表 -->
    <div class="card">
      <h3 class="card__title">动态特征范围</h3>
      <table class="range-table">
        <thead>
          <tr>
            <th style="width: 30%;">特征</th>
            <th>2% 最小值</th>
            <th>98% 最大值</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(band, i) in bandNames" :key="band">
            <td><code class="range-table__band">{{ band }}</code></td>
            <td>{{ fmtBand(band, minValues[i]) }}</td>
            <td>{{ fmtBand(band, maxValues[i]) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ③ 醒目提醒卡片 -->
    <div class="card warning-card">
      <div class="warning-card__icon" aria-hidden="true">⚠️</div>
      <div class="warning-card__body">
        <strong class="warning-card__title">重要：请将以下结果填入后续特征提取代码</strong>
        <p class="warning-card__text">
          本页面计算得到的 <code>min_values</code>、<code>max_values</code>、<code>demMin</code>、<code>demMax</code>
          不是仅用于查看，而是需要<strong>完整替换</strong>后续 Python 代码中的对应参数。
        </p>
      </div>
    </div>

    <!-- ④ min_values 复制块 -->
    <div class="card code-card">
      <div class="code-card__header">
        <h3 class="code-card__title">min_values — 填写位置</h3>
        <button
          type="button"
          class="button button--copy"
          :class="{ 'button--copied': copied === 'min_values' }"
          @click="copyText('min_values', minValuesCode)"
        >
          {{ copied === 'min_values' ? '已复制 ✓' : '复制 min_values' }}
        </button>
      </div>
      <p class="code-card__hint">
        请找到后续特征提取代码中的 <code>min_values = [...]</code>，将原来的数组<strong>完整替换</strong>为下面生成的数组：
      </p>
      <pre class="code-block"><code>{{ minValuesCode }}</code></pre>
    </div>

    <!-- ⑤ max_values 复制块 -->
    <div class="card code-card">
      <div class="code-card__header">
        <h3 class="code-card__title">max_values — 填写位置</h3>
        <button
          type="button"
          class="button button--copy"
          :class="{ 'button--copied': copied === 'max_values' }"
          @click="copyText('max_values', maxValuesCode)"
        >
          {{ copied === 'max_values' ? '已复制 ✓' : '复制 max_values' }}
        </button>
      </div>
      <p class="code-card__hint">
        请找到后续特征提取代码中的 <code>max_values = [...]</code>，将原来的数组<strong>完整替换</strong>为下面生成的数组：
      </p>
      <pre class="code-block"><code>{{ maxValuesCode }}</code></pre>
    </div>

    <!-- ⑦ DEM 参数 -->
    <div class="card code-card">
      <div class="code-card__header">
        <h3 class="code-card__title">DEM 参数 — 填写位置</h3>
        <button
          type="button"
          class="button button--copy"
          :class="{ 'button--copied': copied === 'dem' }"
          @click="copyText('dem', demCode)"
        >
          {{ copied === 'dem' ? '已复制 ✓' : '复制 DEM 参数' }}
        </button>
      </div>
      <p class="code-card__hint">
        请将下面生成的 <code>demMin</code>、<code>demMax</code> 替换后续 Python 代码中的对应参数。
      </p>

      <div class="dem-row">
        <div class="dem-row__label">
          <strong>demMin</strong>
          <span class="dem-row__hint">→ 填入后续代码中的 <code>demMin</code></span>
        </div>
        <pre class="code-block code-block--inline"><code>{{ demMinCode }}</code></pre>
      </div>

      <div class="dem-row">
        <div class="dem-row__label">
          <strong>demMax</strong>
          <span class="dem-row__hint">→ 填入后续代码中的 <code>demMax</code></span>
        </div>
        <pre class="code-block code-block--inline"><code>{{ demMaxCode }}</code></pre>
      </div>

      <div class="dem-summary">
        <div>
          <span class="dem-summary__label">DEM 最低高程</span>
          <span class="dem-summary__value">{{ fmtDem(demMin) }} <small>m</small></span>
        </div>
        <div>
          <span class="dem-summary__label">DEM 最高高程</span>
          <span class="dem-summary__value">{{ fmtDem(demMax) }} <small>m</small></span>
        </div>
      </div>
    </div>

    <!-- ⑧ 一键复制全部 -->
    <div class="card copy-all-card">
      <button
        type="button"
        class="button button--primary button--copy-all"
        :class="{ 'button--copied': copied === 'all' }"
        @click="copyText('all', allCode)"
      >
        {{ copied === 'all' ? '已复制全部 ✓' : '一键复制全部参数' }}
      </button>
      <p class="copy-all-card__hint">
        会一次性复制 <code>min_values</code>、<code>max_values</code>、<code>demMin</code>、<code>demMax</code> 的完整赋值语句。
      </p>
    </div>

    <!-- ⑨ 再次运行 -->
    <div class="card rerun-card">
      <router-link
        :to="{ name: 'algorithm-detail', params: { id: 'crop_threshold' } }"
        class="button button--primary button--rerun"
      >
        再次运行本算法
      </router-link>
      <p class="rerun-card__hint">
        返回『甘蔗分类-获取阈值』表单，可以基于本次研究区/年份等参数做微调后重新提交。
      </p>
    </div>

    <!-- 兜底：结果不是预期结构时 -->
    <div v-if="!hasThresholdShape" class="card">
      <p class="list__empty">
        任务结果中未找到 <code>band_names</code> / <code>min_values</code> / <code>max_values</code>，
        无法渲染阈值结果。
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  jobId: { type: String, required: true },
  algorithm: { type: String, default: '' },
  result: { type: Object, default: () => ({}) },
  // 由父组件透传，便于页面顶部状态徽章 / GEE 项目等参数显示。
  status: { type: String, default: '' },
  geeProject: { type: String, default: '' },
  jobMessage: { type: String, default: '' },
})

const STATUS_LABELS = {
  queued: '等待中',
  running: '运行中',
  submitted: '等待中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

// 把 GEE asset 路径的最后一段映射成可读名；未命中则回退到 asset 路径本身。
const ROI_NAME_MAP = {
  guangxi: '广西',
}

const copied = ref('')

const metrics = computed(() => props.result?.metrics || {})

const bandNames = computed(() => metrics.value.band_names || [])
const minValues = computed(() => metrics.value.min_values || [])
const maxValues = computed(() => metrics.value.max_values || [])
const year = computed(() => metrics.value.year)
const roiAssetId = computed(() => metrics.value.roi_asset_id)
// 本地文件信息：如果任务是以上传文件方式运行，job.params 里会带 roi_local_path；
// 任务信息卡的研究区行额外显示本地文件名（hover tooltip）。
const jobParams = computed(() => props.result?.params || {})
const roiLocalPath = computed(() => jobParams.value?.roi_local_path || '')
const roiLocalName = computed(() => {
  const p = roiLocalPath.value
  if (!p) return ''
  return p.split(/[\\/]/).pop() || p
})
const roiName = computed(() => {
  const asset = roiAssetId.value || ''
  const tail = asset.split('/').pop() || ''
  return ROI_NAME_MAP[tail] || tail || asset || '—'
})
const roiTooltip = computed(() => {
  const lines = []
  if (roiLocalName.value) lines.push(`本地文件：${roiLocalName.value}`)
  if (roiAssetId.value) lines.push(`GEE Asset：${roiAssetId.value}`)
  return lines.join('\n')
})
const demMin = computed(() => metrics.value.dem_min)
const demMax = computed(() => metrics.value.dem_max)

const hasThresholdShape = computed(() =>
  Array.isArray(bandNames.value)
  && bandNames.value.length > 0
  && Array.isArray(minValues.value)
  && Array.isArray(maxValues.value)
  && minValues.value.length === bandNames.value.length
  && maxValues.value.length === bandNames.value.length
)

const statusLabel = computed(() => STATUS_LABELS[props.status] || props.status || '—')

// ---------- 数字格式化：按波段类型选小数位，绝不使用科学计数法 ----------
// 表里 11 个波段大致分两类：
//   - 反射率 / 指数（B2..B12, NDVI, EVI, NDWI） → 6 位小数（数值范围 0~1）
//   - 后向散射 dB（VV, VH）                    → 4 位小数（数值范围 -50~0）
// DEM 用 2 位小数（单位 m）。
const _REF_BANDS = new Set(['B2', 'B3', 'B4', 'B8', 'B11', 'B12', 'NDVI', 'EVI', 'NDWI'])
const _S1_BANDS = new Set(['VV', 'VH'])

function _fmtFixed(v, decimals) {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return v.toFixed(decimals)
}

function fmtBand(band, v) {
  if (_S1_BANDS.has(band)) return _fmtFixed(v, 4)
  if (_REF_BANDS.has(band)) return _fmtFixed(v, 6)
  // 兜底：未知波段按 6 位小数
  return _fmtFixed(v, 6)
}

function fmtDem(v) {
  return _fmtFixed(v, 2)
}

// 兼容旧调用点：保持 fmtNum 名字可用，但行为改为「默认 6 位小数」，
// 避免再有组件内其它地方意外走科学计数法。
function fmtNum(v) {
  return _fmtFixed(v, 6)
}

// ---------- 代码片段构造（全部用 fmtBand / fmtDem，禁用科学计数法） ----------
function _fmtForCode(band, v) {
  // 代码复制块希望反射率指数保留尽量多精度（6 位），dB 4 位，与表内一致。
  return fmtBand(band, v)
}

const minValuesCode = computed(() => {
  const lines = bandNames.value.map((b, i) => `    ${_fmtForCode(b, minValues.value[i])},`).join('\n')
  return `min_values = [\n${lines}\n]`
})

const maxValuesCode = computed(() => {
  const lines = bandNames.value.map((b, i) => `    ${_fmtForCode(b, maxValues.value[i])},`).join('\n')
  return `max_values = [\n${lines}\n]`
})

const demMinCode = computed(() => `demMin = ${fmtDem(demMin.value)}`)
const demMaxCode = computed(() => `demMax = ${fmtDem(demMax.value)}`)

const demCode = computed(() => `${demMinCode.value}\n${demMaxCode.value}`)

const allCode = computed(
  () => `${minValuesCode.value}\n\n${maxValuesCode.value}\n\n${demMinCode.value}\n${demMaxCode.value}`
)

// ---------- 复制 ----------
async function copyText(key, text) {
  try {
    await navigator.clipboard.writeText(text)
  } catch (_) {
    // 浏览器拒绝剪贴板时降级为 prompt，让用户手动 Ctrl+C。
    window.prompt('请按 Ctrl+C 复制以下内容：', text)
  }
  copied.value = key
  setTimeout(() => {
    if (copied.value === key) copied.value = ''
  }, 2000)
}
</script>

<style scoped>
.threshold-result {
  display: flex;
  flex-direction: column;
}

/* ① 任务信息 */
.info-card__title {
  margin: 0 0 10px 0;
  font-size: 14px;
  font-weight: 600;
}
.info-card__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px 16px;
  font-size: 13px;
}
.info-card__label {
  display: inline-block;
  color: var(--color-muted);
  margin-right: 6px;
  min-width: 64px;
}
.info-card__value {
  color: var(--color-text);
  font-weight: 500;
  word-break: break-all;
}
.info-card__sub {
  display: block;
  margin-top: 2px;
  color: var(--color-muted);
  font-size: 11px;
  word-break: break-all;
}
.info-card__message {
  margin: 10px 0 0 0;
  font-size: 12px;
  color: var(--color-muted);
}

/* ② 范围表 */
.range-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.range-table th,
.range-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-border);
  text-align: left;
}
.range-table thead th {
  background: #f3f4f6;
  font-weight: 600;
}
.range-table tbody tr:last-child td {
  border-bottom: none;
}
.range-table__band {
  background: #eef2ff;
  color: #1e3a8a;
  padding: 1px 8px;
  border-radius: 4px;
  font-weight: 600;
}

/* ③ 醒目提醒 */
.warning-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  background: #fef3c7;
  border-color: #fcd34d;
}
.warning-card__icon {
  font-size: 22px;
  line-height: 1;
  margin-top: 2px;
}
.warning-card__body {
  flex: 1;
}
.warning-card__title {
  display: block;
  font-size: 14px;
  margin-bottom: 4px;
  color: #92400e;
}
.warning-card__text {
  margin: 0;
  font-size: 13px;
  color: #78350f;
  line-height: 1.55;
}
.warning-card__text code {
  background: rgba(146, 64, 14, 0.1);
  padding: 0 4px;
  border-radius: 3px;
}

/* ④⑤⑦ 代码块卡片 */
.code-card__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 6px;
}
.code-card__title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.code-card__hint {
  margin: 4px 0 10px 0;
  font-size: 13px;
  color: var(--color-text);
  line-height: 1.55;
}
.code-card__hint code {
  background: #f3f4f6;
  padding: 0 4px;
  border-radius: 3px;
  font-size: 12px;
}

.code-block {
  margin: 0;
  padding: 12px 14px;
  background: #1e1e1e;
  color: #d4d4d4;
  border-radius: 6px;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre;
  overflow-x: auto;
}
.code-block code {
  font-family: inherit;
}
.code-block--inline {
  display: inline-block;
  margin: 4px 0;
  padding: 6px 10px;
}

/* 复制按钮 */
.button--copy {
  flex-shrink: 0;
  font-size: 12px;
  padding: 4px 12px;
}
.button--copied {
  background: var(--color-success);
}

/* ⑨ 再次运行 */
.rerun-card {
  text-align: center;
  background: #f8fafc;
}
.button--rerun {
  font-size: 14px;
  padding: 10px 24px;
  text-decoration: none;
  display: inline-block;
}
.rerun-card__hint {
  margin: 8px 0 0 0;
  font-size: 12px;
  color: var(--color-muted);
}

/* ⑦ DEM 行 */
.dem-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 0;
  border-top: 1px dashed var(--color-border);
}
.dem-row:first-of-type {
  border-top: none;
}
.dem-row__label {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.dem-row__hint {
  color: var(--color-muted);
  font-size: 12px;
}
.dem-row__hint code {
  background: #f3f4f6;
  padding: 0 4px;
  border-radius: 3px;
}
.dem-summary {
  display: flex;
  gap: 24px;
  margin-top: 12px;
  padding: 10px 12px;
  background: #f8fafc;
  border-radius: 6px;
  font-size: 13px;
}
.dem-summary__label {
  display: block;
  color: var(--color-muted);
  margin-bottom: 2px;
}
.dem-summary__value {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}
.dem-summary__value small {
  font-size: 11px;
  font-weight: 400;
  color: var(--color-muted);
  margin-left: 2px;
}

/* ⑧ 一键复制 */
.copy-all-card {
  text-align: center;
  background: #f8fafc;
}
.button--copy-all {
  font-size: 14px;
  padding: 10px 24px;
}
.copy-all-card__hint {
  margin: 8px 0 0 0;
  font-size: 12px;
  color: var(--color-muted);
}
.copy-all-card__hint code {
  background: #f3f4f6;
  padding: 0 4px;
  border-radius: 3px;
}
</style>