<template>
  <div class="result-renderer">
    <!-- 顶部：标题 + 副标题（消息） -->
    <header class="result-renderer__header" v-if="resultType || hasMessage">
      <h3 class="result-renderer__title">{{ resultTitle }}</h3>
      <p v-if="hasMessage" class="result-renderer__subtitle">{{ result.message }}</p>
    </header>

    <!-- ① 指标卡 -->
    <section v-if="cards.length" class="result-section">
      <h4 class="result-section__heading">指标</h4>
      <div class="result-section__tiles">
        <div v-for="card in cards" :key="card.key" class="tile">
          <div class="tile__label">{{ card.label }}</div>
          <div class="tile__value">
            {{ formatCardValue(card.value) }}
            <span v-if="card.unit" class="tile__unit">{{ card.unit }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- ② 表格（自动发现 list[dict]） -->
    <section
      v-for="t in tables"
      :key="t.key"
      class="result-section"
    >
      <h4 class="result-section__heading">{{ t.label }}</h4>
      <div class="result-section__table-wrap" v-if="t.rows.length">
        <table class="result-section__table">
          <thead>
            <tr>
              <th v-for="col in t.columns" :key="col">{{ col }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, idx) in t.rows" :key="idx">
              <td v-for="col in t.columns" :key="col">{{ formatCell(row[col]) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="result-section__empty">无数据。</p>
    </section>

    <!-- ③ 影像（result.files 中的 .tif / .tiff） -->
    <section v-if="rasters.length" class="result-section">
      <h4 class="result-section__heading">栅格影像</h4>
      <div v-for="r in rasters" :key="r.name" class="raster-card">
        <div class="raster-card__header">
          <div class="raster-card__title">
            <span class="raster-card__label" v-if="r.label">{{ r.label }}</span>
            <code>{{ r.name }}</code>
            <span class="raster-card__size">{{ formatSize(r.size_bytes) }}</span>
          </div>
          <div class="raster-card__actions">
            <button
              type="button"
              class="button button--ghost"
              @click="openLightbox(r)"
            >🔍 放大预览</button>
            <a
              :href="r.previewUrl"
              target="_blank"
              rel="noopener"
              class="button button--ghost"
            >新窗口打开</a>
            <a
              :href="r.downloadUrl"
              :download="r.name"
              class="button button--ghost"
            >下载 TIF</a>
          </div>
        </div>

        <!-- 缩略图：加载失败时显示占位；点击触发 lightbox -->
        <button
          type="button"
          class="raster-card__img-wrap"
          @click="openLightbox(r)"
          :title="`点击放大：${r.name}`"
        >
          <img
            :src="r.previewUrl"
            :alt="r.name"
            class="raster-card__img"
            loading="lazy"
            @error="onImgError(r.name)"
            v-show="!imgError[r.name]"
          />
          <div v-if="imgError[r.name]" class="raster-card__img-error">
            图片加载失败
          </div>
        </button>

        <!-- 折叠的 OL 视图（像元查询 + 图例 + 统计） -->
        <details class="raster-card__advanced">
          <summary>高级视图（地图叠加 / 像元查询 / 统计）</summary>
          <div class="raster-card__advanced-body">
            <ResultViewer :job-id="jobId" :filename="r.name" />
          </div>
        </details>
      </div>
    </section>

    <!-- ④ 算法直接产出的非 tif 图像 -->
    <section v-if="imageFiles.length" class="result-section">
      <h4 class="result-section__heading">图像</h4>
      <div v-for="img in imageFiles" :key="img.name" class="raster-card">
        <div class="raster-card__header">
          <code>{{ img.name }}</code>
          <span class="raster-card__size">{{ formatSize(img.size_bytes) }}</span>
          <div class="raster-card__actions">
            <button
              type="button"
              class="button button--ghost"
              @click="openLightbox(img, imageFiles)"
            >🔍 放大预览</button>
            <a
              :href="img.downloadUrl"
              :download="img.name"
              class="button button--ghost"
            >下载</a>
          </div>
        </div>
        <button
          type="button"
          class="raster-card__img-wrap"
          @click="openLightbox(img, imageFiles)"
          :title="`点击放大：${img.name}`"
        >
          <img
            :src="img.downloadUrl"
            :alt="img.name"
            class="raster-card__img"
            loading="lazy"
            @error="onImgError(img.name)"
            v-show="!imgError[img.name]"
          />
          <div v-if="imgError[img.name]" class="raster-card__img-error">
            图片加载失败
          </div>
        </button>
      </div>
    </section>

    <!-- Lightbox：仿 el-image 的 preview-src-list 大图预览 -->
    <div
      v-if="lightbox.open"
      class="lightbox-mask"
      @click.self="closeLightbox"
      role="dialog"
      aria-modal="true"
    >
      <div class="lightbox">
        <div class="lightbox__header">
          <div class="lightbox__title">
            <span class="raster-card__label" v-if="lightboxCurrent?.label">{{ lightboxCurrent.label }}</span>
            <code>{{ lightboxCurrent?.name }}</code>
          </div>
          <div class="lightbox__actions">
            <button
              type="button"
              class="button button--ghost"
              :disabled="!lightboxHasPrev"
              @click="lightboxPrev"
            >‹ 上一张</button>
            <button
              type="button"
              class="button button--ghost"
              :disabled="!lightboxHasNext"
              @click="lightboxNext"
            >下一张 ›</button>
            <a
              v-if="lightboxCurrent"
              :href="lightboxCurrent.downloadUrl"
              :download="lightboxCurrent.name"
              class="button button--ghost"
            >下载</a>
            <button
              type="button"
              class="button button--ghost"
              @click="closeLightbox"
              title="关闭 (Esc)"
            >✕ 关闭</button>
          </div>
        </div>
        <div class="lightbox__body">
          <img
            :src="lightboxCurrent?.previewUrl || lightboxCurrent?.downloadUrl"
            :alt="lightboxCurrent?.name"
            class="lightbox__img"
          />
        </div>
        <div v-if="lightboxCount > 1" class="lightbox__footer">
          {{ lightbox.index + 1 }} / {{ lightboxCount }}
        </div>
      </div>
    </div>

    <!-- ⑤ 文件下载（非图像） -->
    <section v-if="otherFiles.length" class="result-section">
      <h4 class="result-section__heading">下载结果文件</h4>
      <ul class="result-section__files">
        <li v-for="f in otherFiles" :key="f.name">
          <code class="file-name">{{ f.name }}</code>
          <span class="file-size">{{ formatSize(f.size_bytes) }}</span>
          <a
            :href="f.downloadUrl"
            :download="f.name"
            class="button button--ghost"
          >下载</a>
        </li>
      </ul>
    </section>

    <!-- ⑥ 再运行一次 -->
    <div v-if="rerunPath" class="result-renderer__actions">
      <router-link :to="rerunPath" class="button">再运行一次</router-link>
    </div>

    <!-- 兜底：所有 section 都为空且没有 message -->
    <p v-if="isEmpty" class="result-renderer__empty">
      任务已完成，但未返回任何结构化结果。
    </p>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, watch } from 'vue'
import ResultViewer from './ResultViewer.vue'
import { jobFileUrl, jobPreviewUrl } from '../api/jobFiles'

const props = defineProps({
  jobId: { type: String, required: true },
  algorithm: { type: String, default: '' },
  result: { type: Object, default: () => ({}) }
})

// ----------------------------------------------------------------
// 顶部标题
// ----------------------------------------------------------------
const RESULT_TYPE_LABELS = {
  raster: '栅格结果',
  csv: 'CSV 结果',
  json: '结构化结果',
  metrics: '指标结果',
  gee_task: 'GEE 任务已提交'
}

const resultType = computed(() => props.result?.result_type || '')
const hasMessage = computed(() => Boolean(props.result?.message))

const resultTitle = computed(() => {
  return RESULT_TYPE_LABELS[resultType.value] || '运行结果'
})

// ----------------------------------------------------------------
// ① 指标卡：直接读 metrics.cards
// ----------------------------------------------------------------
const cards = computed(() => {
  const raw = props.result?.metrics?.cards
  if (!Array.isArray(raw)) return []
  return raw.filter((c) => c && c.key != null)
})

// ----------------------------------------------------------------
// ② 表格：自动发现 metrics 中 list[dict]
// 黑名单：cards 已渲染；train/target_metrics_final 是单 dict；
// confusion_matrix 是二维 list[list]；band_names/min_values/max_values 是裸 list
// ----------------------------------------------------------------
const TABLE_BLACKLIST = new Set([
  'cards',
  'train_metrics_final',
  'target_metrics_final',
  'confusion_matrix',
  'band_names',
  'min_values',
  'max_values',
  'min_max',
  'year',
  'samples',
  'gee_task_id',
  'rasters',
  'tables',
  'files',
  'images',
  'output',
])

const TABLE_LABEL_OVERRIDES = {
  county_predictions: '县级预测结果',
  predictions: '预测结果',
  rows: '记录',
  records: '记录',
  samples: '样本',
}

function humanize(key) {
  if (TABLE_LABEL_OVERRIDES[key]) return TABLE_LABEL_OVERRIDES[key]
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

const tables = computed(() => {
  const metrics = props.result?.metrics || {}
  const out = []
  for (const [key, val] of Object.entries(metrics)) {
    if (TABLE_BLACKLIST.has(key)) continue
    if (!Array.isArray(val) || val.length === 0) continue
    if (typeof val[0] !== 'object' || val[0] === null) continue
    const columns = Object.keys(val[0])
    if (columns.length === 0) continue
    out.push({ key, label: humanize(key), columns, rows: val })
  }
  return out
})

// ----------------------------------------------------------------
// ③ 影像：result.files 中的 .tif / .tiff
// ----------------------------------------------------------------
const TIF_RE = /\.tiff?$/i
const IMG_RE = /\.(png|jpe?g|webp|gif)$/i

function makeFileRefs(file) {
  return {
    ...file,
    downloadUrl: jobFileUrl(props.jobId, file.name),
    previewUrl: jobPreviewUrl(props.jobId, file.name),
  }
}

const rasters = computed(() => {
  const files = props.result?.files || []
  return files.filter((f) => TIF_RE.test(f.name)).map(makeFileRefs)
})

const imageFiles = computed(() => {
  const files = props.result?.files || []
  // 非 .tif 的图像（png/jpg/...）：预览接口只接受 .tif，这里只填 downloadUrl，
  // 不要把 previewUrl 也带上 —— 否则 lightbox / 缩略图会调到 /preview.png
  // 拿到 400，导致图片加载失败。
  return files
    .filter((f) => IMG_RE.test(f.name) && !TIF_RE.test(f.name))
    .map((f) => ({
      ...f,
      downloadUrl: jobFileUrl(props.jobId, f.name),
      previewUrl: jobFileUrl(props.jobId, f.name),
    }))
})

const otherFiles = computed(() => {
  const files = props.result?.files || []
  return files.filter((f) => !TIF_RE.test(f.name) && !IMG_RE.test(f.name)).map((f) => ({
    ...f,
    downloadUrl: jobFileUrl(props.jobId, f.name),
  }))
})

// ----------------------------------------------------------------
// "再运行一次" 链接 —— 仅在算法可重新提交时显示
// ----------------------------------------------------------------
const NON_RERUNNABLE = new Set(['algorithm_6'])

const rerunPath = computed(() => {
  if (!props.algorithm || NON_RERUNNABLE.has(props.algorithm)) return ''
  return `/algorithms/${props.algorithm}`
})

// ----------------------------------------------------------------
// 空结果判定
// ----------------------------------------------------------------
const isEmpty = computed(() =>
  cards.value.length === 0 &&
  tables.value.length === 0 &&
  rasters.value.length === 0 &&
  imageFiles.value.length === 0 &&
  otherFiles.value.length === 0 &&
  !hasMessage.value
)

// ----------------------------------------------------------------
// 格式化
// ----------------------------------------------------------------
function formatCardValue(v) {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'number') return fmtNum(v)
  if (typeof v === 'boolean') return v ? '是' : '否'
  return String(v)
}

function formatCell(v) {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'number') return fmtNum(v)
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function fmtNum(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000 || (Math.abs(v) < 0.01 && v !== 0)) return v.toExponential(3)
  // 整数走整数格式，小数保留 4 位
  if (Number.isInteger(v)) return v.toLocaleString('en-US')
  return v.toFixed(4)
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = Number(bytes)
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

// ----------------------------------------------------------------
// 图片加载失败标记（按文件名记录，刷新后自动清除）
// ----------------------------------------------------------------
const imgError = reactive({})

function onImgError(name) {
  if (name) imgError[name] = true
}

// ----------------------------------------------------------------
// 放大预览（仿 el-image preview-src-list）：支持上下张切换、Esc 关闭
// ----------------------------------------------------------------
const lightbox = reactive({
  open: false,
  list: [],
  index: 0,
})

const ALL_PREVIEW_FILES = computed(() => [...rasters.value, ...imageFiles.value])

function findPreviewList(target) {
  // 用户传入的就是单个对象时，从全局可预览列表里找出上下文；传入列表则直接用。
  if (Array.isArray(target)) return target
  return ALL_PREVIEW_FILES.value.filter(
    (f) => f.previewUrl || f.downloadUrl
  )
}

function openLightbox(target, list) {
  const items = list || findPreviewList(target)
  const idx = items.findIndex(
    (f) => f.name === target.name && f.downloadUrl === target.downloadUrl
  )
  if (items.length === 0) return
  lightbox.list = items
  lightbox.index = idx >= 0 ? idx : 0
  lightbox.open = true
  // 打开时锁住背景滚动
  if (typeof document !== 'undefined') {
    document.body.style.overflow = 'hidden'
  }
}

// 提供给模板使用的 lightbox 派生属性（避免在模板里写一长串 ? : 表达式）
const lightboxCurrent = computed(() => lightbox.list[lightbox.index] || null)
const lightboxCount = computed(() => lightbox.list.length)
const lightboxHasPrev = computed(() => lightbox.index > 0)
const lightboxHasNext = computed(() => lightbox.index < lightbox.list.length - 1)

function closeLightbox() {
  lightbox.open = false
  lightbox.list = []
  lightbox.index = 0
  if (typeof document !== 'undefined') {
    document.body.style.overflow = ''
  }
}

function lightboxPrev() {
  if (lightbox.index > 0) lightbox.index -= 1
}

function lightboxNext() {
  if (lightbox.index < lightbox.list.length - 1) lightbox.index += 1
}

function onKeydown(e) {
  if (!lightbox.open) return
  if (e.key === 'Escape') {
    e.preventDefault()
    closeLightbox()
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault()
    lightboxPrev()
  } else if (e.key === 'ArrowRight') {
    e.preventDefault()
    lightboxNext()
  }
}

if (typeof window !== 'undefined') {
  onMounted(() => window.addEventListener('keydown', onKeydown))
}

onBeforeUnmount(() => {
  if (typeof window !== 'undefined') {
    window.removeEventListener('keydown', onKeydown)
  }
  if (typeof document !== 'undefined') {
    document.body.style.overflow = ''
  }
})

// 当 result 引用变更时，清理掉过期的 imgError 标记
watch(
  () => props.result?.files,
  () => {
    const names = new Set((props.result?.files || []).map((f) => f.name))
    for (const key of Object.keys(imgError)) {
      if (!names.has(key)) delete imgError[key]
    }
  },
  { deep: true }
)
</script>

<style scoped>
.result-renderer__header {
  margin-bottom: 12px;
}
.result-renderer__title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
}
.result-renderer__subtitle {
  margin: 4px 0 0 0;
  font-size: 13px;
  color: var(--color-muted);
}
.result-renderer__empty {
  color: var(--color-muted);
  padding: 12px 0;
  margin: 0;
}

.result-section {
  margin-top: 16px;
}
.result-section__heading {
  margin: 0 0 8px 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}
.result-section__empty {
  margin: 0;
  color: var(--color-muted);
  font-size: 13px;
}
.result-section__tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 8px;
}

.tile {
  background: #f8fafc;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 10px 12px;
}
.tile__label {
  font-size: 12px;
  color: var(--color-muted);
  margin-bottom: 4px;
}
.tile__value {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}
.tile__unit {
  font-size: 12px;
  font-weight: 400;
  color: var(--color-muted);
  margin-left: 4px;
}

.result-section__table-wrap {
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: 6px;
}
.result-section__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.result-section__table th,
.result-section__table td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--color-border);
  text-align: left;
  white-space: nowrap;
}
.result-section__table th {
  position: sticky;
  top: 0;
  background: #f3f4f6;
  z-index: 1;
  font-weight: 600;
}

.result-section__files {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  overflow: hidden;
}
.result-section__files li {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--color-border);
  font-size: 13px;
}
.result-section__files li:last-child {
  border-bottom: none;
}
.file-name {
  flex: 1;
  min-width: 0;
  word-break: break-all;
}
.file-size {
  color: var(--color-muted);
  font-size: 12px;
  white-space: nowrap;
}

/* 影像卡 */
.raster-card {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: #fafafa;
  margin-bottom: 12px;
  overflow: hidden;
}
.raster-card__header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #fff;
  border-bottom: 1px solid var(--color-border);
}
.raster-card__title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.raster-card__label {
  display: inline-block;
  background: #dcfce7;
  color: #166534;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
}
.raster-card__size {
  color: var(--color-muted);
  font-size: 12px;
}
.raster-card__actions {
  display: inline-flex;
  gap: 6px;
  margin-left: auto;
}
.raster-card__img-link {
  display: block;
  background: #fff;
}
.raster-card__img-wrap {
  display: block;
  width: 100%;
  padding: 0;
  margin: 0;
  background: #fff;
  border: none;
  cursor: zoom-in;
  text-align: center;
}
.raster-card__img-wrap:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: -2px;
}
.raster-card__img {
  display: block;
  width: 100%;
  height: auto;
  max-height: 480px;
  object-fit: contain;
  background: #ffffff;
  image-rendering: pixelated;
}
.raster-card__img-error {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 160px;
  color: var(--color-muted);
  background: #f5f7fa;
  font-size: 13px;
}

/* ------------------------------------------------------------------
 * Lightbox (仿 el-image 大图预览)
 * z-index 设到 1000+ 确保盖在所有弹层之上；OL 默认 .ol-viewport 是 10，
 * 之前手工提到 11 仍可能被浮层覆盖，这里直接抬到 1000 杜绝冲突。
 * ------------------------------------------------------------------ */
.lightbox-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.78);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.lightbox {
  background: #0f172a;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  max-width: min(1200px, 96vw);
  max-height: 96vh;
  width: 100%;
  overflow: hidden;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.45);
}
.lightbox__header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: #1e293b;
  color: #f1f5f9;
  border-bottom: 1px solid #334155;
}
.lightbox__title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  flex: 1;
  min-width: 0;
}
.lightbox__title code {
  color: #cbd5e1;
  word-break: break-all;
}
.lightbox__actions {
  display: inline-flex;
  gap: 6px;
  flex-shrink: 0;
}
.lightbox__body {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0f172a;
  padding: 12px;
  overflow: auto;
}
.lightbox__img {
  display: block;
  max-width: 100%;
  max-height: calc(96vh - 110px);
  object-fit: contain;
  image-rendering: pixelated;
  background: #fff;
}
.lightbox__footer {
  padding: 6px 16px;
  text-align: center;
  font-size: 12px;
  color: #94a3b8;
  background: #1e293b;
  border-top: 1px solid #334155;
}
/* Lightbox 内的按钮在深色底上要有更高对比度 */
.lightbox .button--ghost {
  background: transparent;
  color: #cbd5e1;
  border-color: #475569;
}
.lightbox .button--ghost:hover:not(:disabled) {
  background: rgba(148, 163, 184, 0.15);
}
.lightbox .button--ghost:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.raster-card__advanced {
  border-top: 1px solid var(--color-border);
  background: #fff;
}
.raster-card__advanced summary {
  padding: 8px 12px;
  cursor: pointer;
  font-size: 13px;
  color: var(--color-muted);
  user-select: none;
}
.raster-card__advanced summary:hover {
  background: #f8fafc;
}
.raster-card__advanced-body {
  padding: 12px;
}

.button--ghost {
  background: transparent;
  color: var(--color-primary);
  border: 1px solid var(--color-border);
  padding: 4px 10px;
  font-size: 12px;
  text-decoration: none;
  display: inline-block;
  border-radius: 4px;
}
.button--ghost:hover {
  background: rgba(37, 99, 235, 0.06);
  text-decoration: none;
}

.result-renderer__actions {
  margin-top: 16px;
  display: flex;
  gap: 8px;
}
</style>