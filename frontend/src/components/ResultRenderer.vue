<template>
  <div class="result-renderer">
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

    <!-- ③ 影像（result.files 中的 .tif / .tiff）
         同一个 group 的多个 .tif 是「同一张图切成的分片」，拼起来才是完整
         结果 —— 这类 group 走 /result/mosaic/* 拼成一张图展示。 -->
    <section v-if="rasterGroups.length" class="result-section">
      <h4 class="result-section__heading">栅格影像</h4>
      <div v-for="g in rasterGroups" :key="g.key" class="raster-card">
        <div class="raster-card__header">
          <div class="raster-card__title">
            <span class="raster-card__label" v-if="g.label">{{ g.label }}</span>
            <code>{{ g.name }}</code>
            <span class="raster-card__size">{{ formatSize(g.size_bytes) }}</span>
            <span v-if="g.isMosaic" class="raster-card__shards">
              拼接自 {{ g.names.length }} 个分片
            </span>
          </div>
          <div class="raster-card__actions">
            <button
              type="button"
              class="button button--ghost"
              @click="openLightbox(g)"
            >🔍 放大预览</button>
            <a
              :href="g.previewUrl"
              target="_blank"
              rel="noopener"
              class="button button--ghost"
            >下载png</a>
            <a
              v-if="g.downloadUrl"
              :href="g.downloadUrl"
              :download="g.name"
              class="button button--ghost"
            >下载 TIF</a>
          </div>
        </div>

        <!-- 多分片时列出每个分片，方便单独下载原图 -->
        <div v-if="g.isMosaic" class="raster-card__shard-list">
          <span class="raster-card__shard-list-title">分片：</span>
          <a
            v-for="f in g.files"
            :key="f.name"
            :href="f.downloadUrl"
            :download="f.name"
            class="raster-card__shard"
          >{{ f.name }}</a>
        </div>

        <!-- 缩略图（左） + 图例（右） 横向布局 -->
        <div class="raster-card__body">
          <button
            type="button"
            class="raster-card__img-wrap"
            @click="openLightbox(g)"
            :title="`点击放大：${g.name}`"
          >
            <img
              :src="g.previewUrl"
              :alt="g.name"
              class="raster-card__img"
              loading="lazy"
              @error="onImgError(g.key)"
              v-show="!imgError[g.key]"
            />
            <div v-if="imgError[g.key]" class="raster-card__img-error">
              图片加载失败
            </div>
          </button>

          <!-- 右侧色带图例 -->
          <div class="raster-card__legend">
            <div v-if="infoMap[g.key]" class="legend">
              <div class="legend__title">
                <span class="legend__type-badge" :class="`legend__type-badge--${infoMap[g.key].type}`">
                  {{ typeLabelOf(infoMap[g.key].type) }}
                </span>
                <span class="legend__title-text">{{ legendTitleFor(g) }}</span>
              </div>

              <!-- continuous: 渐变色带 + 三档标注 -->
              <div
                v-if="infoMap[g.key].type === 'continuous'"
                class="legend-continuous"
              >
                <div class="legend-continuous__bar" :style="viridisGradient"></div>
                <div class="legend-continuous__ticks">
                  <span>{{ fmtNum(infoMap[g.key].stats.p2) }}</span>
                  <span>{{ fmtNum(infoMap[g.key].stats.mean) }}</span>
                  <span>{{ fmtNum(infoMap[g.key].stats.p98) }}</span>
                </div>
                <div class="legend-continuous__labels">
                  <span>低</span>
                  <span>中</span>
                  <span>高</span>
                </div>
                <div class="legend__hint">颜色由深到浅表示数值由低到高（P2→P98）</div>
              </div>

              <!-- binary: 前景 / 背景 两色块 -->
              <div
                v-else-if="infoMap[g.key].type === 'binary'"
                class="legend-binary"
              >
                <div
                  v-for="(color, cls) in infoMap[g.key].classes"
                  :key="cls"
                  class="legend-item"
                >
                  <span class="legend-item__swatch" :style="{ background: color }" />
                  <span class="legend-item__label">
                    {{ cls == 1 || cls == 255 ? '前景（有效）' : '背景（非有效）' }}
                  </span>
                  <span class="legend-item__count">
                    {{ fmtPx(binaryCountFor(infoMap[g.key], cls)) }}
                  </span>
                </div>
              </div>

              <!-- categorical: 每个类别一色块 -->
              <div v-else class="legend-categorical">
                <div
                  v-for="(color, cls) in infoMap[g.key].classes"
                  :key="cls"
                  class="legend-item"
                >
                  <span class="legend-item__swatch" :style="{ background: color }" />
                  <span class="legend-item__label">
                    {{ categoricalLabelFor(infoMap[g.key], cls) }}
                  </span>
                  <span class="legend-item__count">
                    {{ fmtPx(infoMap[g.key].stats.class_counts?.[cls] ?? 0) }}
                  </span>
                </div>
              </div>
            </div>
            <div v-else class="legend__loading">
              {{ infoErrors[g.key] ? `图例加载失败：${infoErrors[g.key]}` : '图例加载中…' }}
            </div>
          </div>
        </div>

        <!-- 像元统计：仅 continuous 显示在缩略图下方 -->
        <div
          v-if="infoMap[g.key] && infoMap[g.key].type === 'continuous'"
          class="raster-card__stats"
        >
          <h4 class="raster-card__stats-heading">像元统计</h4>
          <table class="result-viewer__stats">
            <tbody>
              <tr><th>min</th><td>{{ fmtNum(infoMap[g.key].stats.min) }}</td></tr>
              <tr><th>max</th><td>{{ fmtNum(infoMap[g.key].stats.max) }}</td></tr>
              <tr><th>mean</th><td>{{ fmtNum(infoMap[g.key].stats.mean) }}</td></tr>
              <tr><th>std</th><td>{{ fmtNum(infoMap[g.key].stats.std) }}</td></tr>
              <tr><th>p2</th><td>{{ fmtNum(infoMap[g.key].stats.p2) }}</td></tr>
              <tr><th>p98</th><td>{{ fmtNum(infoMap[g.key].stats.p98) }}</td></tr>
              <tr v-if="infoMap[g.key].stats.nodata_count">
                <th>nodata</th><td>{{ infoMap[g.key].stats.nodata_count }} px</td>
              </tr>
              <tr v-if="infoMap[g.key].effective_pixel_ratio != null">
                <th>有效率</th>
                <td>{{ (infoMap[g.key].effective_pixel_ratio * 100).toFixed(2) }}%</td>
              </tr>
            </tbody>
          </table>
        </div>
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
              v-if="lightboxCurrent && lightboxCurrent.downloadUrl"
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

    <!-- ⑤.b CSV 内联预览：解决「GEE 中运行成功，但前端没有实际结果可以展示」
         问题。crop_features 等算法 finalize() 会把 GEE Asset 下载成 CSV
         写到 job_dir；这里再 fetch 一遍解析成表格 + 下载链接，让用户
         不离开页面也能看到内容。表格最多展示前 50 行 + 后 5 行，超出
         时给出提示，避免超长 CSV 把页面渲染卡死。 -->
    <section v-if="csvPreviews.length" class="result-section">
      <h4 class="result-section__heading">CSV 表格预览</h4>
      <div v-for="csv in csvPreviews" :key="csv.name" class="csv-preview">
        <div class="csv-preview__header">
          <code class="file-name">{{ csv.name }}</code>
          <span class="file-size">{{ formatSize(csv.size_bytes) }}</span>
          <a
            :href="csv.downloadUrl"
            :download="csv.name"
            class="button button--ghost"
          >下载 CSV</a>
        </div>
        <p v-if="csv.error" style="color: var(--color-danger); font-size: 12px; margin: 4px 0;">
          {{ csv.error }}
        </p>
        <div v-else-if="csv.loading" style="font-size: 12px; color: var(--color-muted);">
          加载中…
        </div>
        <div v-else-if="!csv.rows.length" style="font-size: 12px; color: var(--color-muted);">
          CSV 内容为空。
        </div>
        <div v-else class="result-section__table-wrap">
          <table class="result-section__table">
            <thead>
              <tr>
                <th v-for="col in csv.columns" :key="col">{{ col }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, idx) in csv.rows" :key="idx">
                <td v-for="col in csv.columns" :key="col">{{ formatCell(row[col]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p
          v-if="csv.truncated && !csv.error"
          style="font-size: 12px; color: var(--color-muted); margin: 4px 0;"
        >
          共 {{ csv.totalRows }} 行，仅展示前 {{ csv.headRows }} 行 + 后 {{ csv.tailRows }} 行。
          点击上方「下载 CSV」获取完整数据。
        </p>
      </div>
    </section>

    <!-- ⑥ 再运行一次 -->
    <div v-if="rerunPath" class="result-renderer__actions">
      <router-link :to="rerunPath" class="button">再运行一次</router-link>
    </div>

    <!-- 兜底：所有 section 都为空（消息由任务信息卡单独展示，这里不重复） -->
    <p v-if="isEmpty" class="result-renderer__empty">
      {{ emptyHint }}
    </p>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, watch } from 'vue'
import {
  fetchJobInfo,
  fetchJobMosaicInfo,
  jobFileUrl,
  jobMosaicPreviewUrl,
  jobPreviewUrl
} from '../api/jobFiles'

const props = defineProps({
  jobId: { type: String, required: true },
  algorithm: { type: String, default: '' },
  result: { type: Object, default: () => ({}) }
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
const CSV_RE = /\.csv$/i

function makeFileRefs(file) {
  return {
    ...file,
    downloadUrl: jobFileUrl(props.jobId, file.name),
    previewUrl: jobPreviewUrl(props.jobId, file.name),
  }
}

// rasterGroups: 把 .tif 产物按 `group` 字段分组。
//   - 一个 group 只有一个文件 → 普通单图，走 /result/files/{name}/preview.png
//   - 一个 group 有多个文件 → 这些是「同一张图切开的分片」，只有拼起来才
//     完整，走 /result/mosaic/* 由后端拼成一张图再展示（和展示页上传多张
//     TIF 时用的是同一条拼接管线）。
// `group` 字段缺省时每个文件自成一组，行为与以前完全一致。
const rasterGroups = computed(() => {
  const files = (props.result?.files || []).filter((f) => TIF_RE.test(f.name))
  const bucket = new Map()
  for (const f of files) {
    const groupValue = f.group ?? ''
    const key = groupValue ? `group:${groupValue}` : `file:${f.name}`
    if (!bucket.has(key)) bucket.set(key, [])
    bucket.get(key).push(f)
  }
  return [...bucket.entries()].map(([key, items]) => {
    const refs = items.map(makeFileRefs)
    const names = refs.map((r) => r.name)
    const isMosaic = refs.length > 1
    return {
      key,
      label: refs[0].label || '',
      names,
      files: refs,
      isMosaic,
      // 单图显示文件名；拼接图显示一个整体标题（label 徽章另外单独显示）
      name: isMosaic ? `拼接结果（${refs.length} 个分片）` : names[0],
      size_bytes: refs.reduce((sum, r) => sum + (Number(r.size_bytes) || 0), 0),
      previewUrl: isMosaic
        ? jobMosaicPreviewUrl(props.jobId, names)
        : jobPreviewUrl(props.jobId, names[0]),
      // 拼接图没有单一的 TIF 可下载，只有分片
      downloadUrl: isMosaic ? '' : jobFileUrl(props.jobId, names[0]),
    }
  })
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

// CSV 文件单独抽出做表格预览（见下方 csvPreviews），其它非图像文件走「下载文件」列表。
const csvFiles = computed(() =>
  (props.result?.files || []).filter((f) => CSV_RE.test(f.name) && !TIF_RE.test(f.name))
)

const otherFiles = computed(() => {
  const files = props.result?.files || []
  return files
    .filter(
      (f) => !TIF_RE.test(f.name) && !IMG_RE.test(f.name) && !CSV_RE.test(f.name)
    )
    .map((f) => ({
      ...f,
      downloadUrl: jobFileUrl(props.jobId, f.name),
    }))
})

// ----------------------------------------------------------------
// ③.b 每个栅格组的 info（驱动色带图例 + 统计表）
// ----------------------------------------------------------------
// infoMap: { groupKey -> info payload }
//   单图组 → /api/jobs/{id}/result/files/{name}/info
//   拼接组 → /api/jobs/{id}/result/mosaic/info?files=a.tif,b.tif
const infoMap = reactive({})
const infoErrors = reactive({})

async function loadRasterInfos() {
  const groups = rasterGroups.value
  const wanted = new Set(groups.map((g) => g.key))
  // 删掉已不存在的 key
  for (const k of Object.keys(infoMap)) {
    if (!wanted.has(k)) {
      delete infoMap[k]
      delete infoErrors[k]
    }
  }
  // 并发 fetch 新出现的组
  const tasks = [...wanted]
    .filter((key) => !(key in infoMap) && !infoErrors[key])
    .map(async (key) => {
      const group = groups.find((g) => g.key === key)
      if (!group) return
      try {
        infoMap[key] = group.isMosaic
          ? await fetchJobMosaicInfo(props.jobId, group.names)
          : await fetchJobInfo(props.jobId, group.names[0])
      } catch (err) {
        infoErrors[key] = err?.message || '加载失败'
      }
    })
  await Promise.all(tasks)
}

// ----------------------------------------------------------------
// ⑤.b CSV 表格内联预览。fetch CSV 全文 → 客户端解析成 rows / columns →
// 表格渲染。表格只展示前 HEAD_ROWS + 后 TAIL_ROWS 行，避免超长 CSV 把
// 页面渲染卡死；用户可点「下载 CSV」拿到原始数据。
// ----------------------------------------------------------------
const HEAD_ROWS = 50
const TAIL_ROWS = 5

// csvPreviews: [{ name, size_bytes, columns, rows, headRows, tailRows,
//                 totalRows, truncated, loading, error, downloadUrl }]
const csvPreviews = reactive([])

function buildCsvPreviewEntry(file) {
  return {
    name: file.name,
    size_bytes: file.size_bytes,
    downloadUrl: jobFileUrl(props.jobId, file.name),
    columns: [],
    rows: [],
    headRows: HEAD_ROWS,
    tailRows: TAIL_ROWS,
    totalRows: 0,
    truncated: false,
    loading: true,
    error: ''
  }
}

// 客户端 CSV 解析：支持引号包裹的字段、转义引号 ""、逗号 / 分号 / Tab
// 分隔符（按文件首部 4KB 嗅探）。不引 SPEC 之外的 RFC4180 边缘细节
// （CRLF / BOM）—— crop_features 的 CSV 是 ee.FeatureCollection.getDownloadURL
// 出的标准格式，这套解析就够用。
function parseCsvText(text) {
  if (!text) return { columns: [], rows: [], totalRows: 0 }
  // 去 BOM
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1)
  const sample = text.slice(0, 4096)
  let delim = ','
  if (sample.includes('\t') && !sample.includes(',')) delim = '\t'
  else if (sample.includes(';') && !sample.includes(',')) delim = ';'
  const lines = []
  let cur = []
  let field = ''
  let inQuotes = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"'
          i += 1
        } else {
          inQuotes = false
        }
      } else {
        field += ch
      }
    } else {
      if (ch === '"') {
        inQuotes = true
      } else if (ch === delim) {
        cur.push(field)
        field = ''
      } else if (ch === '\n') {
        cur.push(field)
        lines.push(cur)
        cur = []
        field = ''
      } else if (ch === '\r') {
        // 忽略 — \n 收尾
      } else {
        field += ch
      }
    }
  }
  // 收尾
  if (field.length || cur.length) {
    cur.push(field)
    lines.push(cur)
  }
  if (!lines.length) return { columns: [], rows: [], totalRows: 0 }
  // 第一行是表头
  const columns = lines[0].map((c) => (c || '').trim() || '?')
  const dataRows = lines.slice(1).filter((r) => r.some((c) => (c || '').length))
  const totalRows = dataRows.length
  // 转成对象数组；空值等略除空尾
  const rows = dataRows.map((r) => {
    const obj = {}
    for (let i = 0; i < columns.length; i++) {
      obj[columns[i]] = r[i] ?? ''
    }
    return obj
  })
  return { columns, rows, totalRows }
}

async function loadCsvPreviews() {
  const wanted = csvFiles.value
  // 删掉已不存在的 file
  const wantedNames = new Set(wanted.map((f) => f.name))
  for (let i = csvPreviews.length - 1; i >= 0; i--) {
    if (!wantedNames.has(csvPreviews[i].name)) csvPreviews.splice(i, 1)
  }
  // 为新出现的 file 各起一个占位 entry
  const existing = new Set(csvPreviews.map((c) => c.name))
  for (const f of wanted) {
    if (!existing.has(f.name)) csvPreviews.push(buildCsvPreviewEntry(f))
  }
  // 并发 fetch
  const tasks = csvPreviews
    .filter((c) => c.loading && !c.error && c.columns.length === 0 && c.rows.length === 0)
    .map(async (entry) => {
      try {
        const resp = await fetch(entry.downloadUrl)
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
        const text = await resp.text()
        const { columns, rows, totalRows } = parseCsvText(text)
        entry.columns = columns
        entry.totalRows = totalRows
        entry.truncated = totalRows > HEAD_ROWS + TAIL_ROWS
        entry.rows = entry.truncated
          ? [...rows.slice(0, HEAD_ROWS), ...rows.slice(-TAIL_ROWS)]
          : rows
        entry.loading = false
      } catch (err) {
        entry.error = `CSV 预览加载失败：${err?.message || err}`
        entry.loading = false
      }
    })
  await Promise.all(tasks)
}

// ----------------------------------------------------------------
// 图例辅助
// ----------------------------------------------------------------
// viridis 色标（与后端 preview_service._render_continuous 的 plt.get_cmap("viridis") 对齐）。
// 11 个采样点对应 0%, 10%, 20%, ... 100%。前端硬编码避免每次都从 /info 多带数据。
const VIRIDIS_STOPS = [
  '#440154', '#482878', '#3E4989', '#31688E', '#26828E',
  '#1F9E89', '#35B779', '#6DCD59', '#B4DE2C', '#FDE725', '#FDE725',
]
const viridisGradient = computed(() => {
  const stops = VIRIDIS_STOPS.map((c, i) => `${c} ${(i / (VIRIDIS_STOPS.length - 1)) * 100}%`)
  return {
    background: `linear-gradient(to right, ${stops.join(', ')})`,
  }
})

function typeLabelOf(t) {
  if (t === 'continuous') return '连续'
  if (t === 'binary') return '二值'
  if (t === 'categorical') return '分类'
  return t || '?'
}

// 图例标题：根据文件名启发式推断。拼接组用所有分片名一起判断。
function legendTitleFor(group) {
  const text = (group.names || [group.name]).join(' ').toLowerCase()
  if (text.includes('yield') || text.includes('产量')) return '产量等级'
  if (text.includes('ndvi') || text.includes('长势') || text.includes('growth')) return '长势等级'
  if (text.includes('disease') || text.includes('病虫')) return '病虫害等级'
  if (text.includes('class')) return '分类结果'
  if (text.includes('final')) return '最终结果'
  if (text.includes('raw')) return '原始数据'
  return '数值分布'
}

function binaryCountFor(info, cls) {
  if (!info || !info.stats) return 0
  const c = Number(cls)
  if (c === 1 || c === 255) return info.stats.count_1 ?? 0
  if (c === 0) return info.stats.count_0 ?? 0
  return 0
}

// categorical 类目标签：优先读 info.class_metadata（业务语义，如
// "10=甘蔗 / 20=水稻"），缺则回退到 `类别 N` 占位文案。
// class_metadata 是 JSON 对象，键在传输过程中会被强制转成字符串，
// 这里同时匹配 number 和 string 形态。
function categoricalLabelFor(info, cls) {
  if (!info) return `类别 ${cls}`
  const meta = info.class_metadata
  if (meta && typeof meta === 'object') {
    const entry = meta[cls] ?? meta[String(cls)]
    if (entry && typeof entry === 'object' && typeof entry.label === 'string' && entry.label.trim()) {
      return `${entry.label}（${cls}）`
    }
  }
  return `类别 ${cls}`
}

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
  rasterGroups.value.length === 0 &&
  imageFiles.value.length === 0 &&
  otherFiles.value.length === 0 &&
  csvPreviews.value.length === 0
)

// 兜底文案要区分"跑完了但没结果"和"跑挂了" —— 之前只有 message 的失败任务
// 会走 hasMessage 分支显示消息，现在消息挪到任务信息卡，这里必须自己说清楚。
const emptyHint = computed(() =>
  props.result?.status === 'failed'
    ? '任务失败，没有返回结构化结果。'
    : '任务已完成，但未返回任何结构化结果。'
)

// ----------------------------------------------------------------
// 格式化
// ----------------------------------------------------------------
function formatCardValue(v) {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'number') {
    // 指标卡里很多值是整数（如「目标年份」=2025）；fmtNum 会把 |v| >= 1000
    // 渲染成科学计数法（"2.025e+3"），对整数卡片来说不友好。整数卡片直接
    // 走千分位本地化，不进入指数分支。浮点 / 大于 / 远小于 0.01 等连续值
    // 仍由 fmtNum 处理，行为不变。
    if (Number.isInteger(v)) return v.toLocaleString('en-US')
    return fmtNum(v)
  }
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
  if (v === null || v === undefined) return '—'
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000 || (Math.abs(v) < 0.01 && v !== 0)) return v.toExponential(3)
  if (Number.isInteger(v)) return v.toLocaleString('en-US')
  return v.toFixed(4)
}

function fmtPx(n) {
  if (typeof n !== 'number') return '0 px'
  return `${n.toLocaleString('en-US')} px`
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

// 可大图预览的条目：每个栅格组（拼接组展示的是拼好的整图）+ 算法直接产出的图像
const ALL_PREVIEW_FILES = computed(() => [...rasterGroups.value, ...imageFiles.value])

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

// 当 result/files 变更时重新 fetch info
watch(
  () => rasterGroups.value.map((g) => g.key).join('|'),
  () => { loadRasterInfos() },
  { immediate: true }
)

// 当 csv 文件列表变化时重新解析预览。
watch(
  () => csvFiles.value.map((f) => f.name).join('|'),
  () => { loadCsvPreviews() },
  { immediate: true }
)

watch(
  () => props.result?.files,
  () => {
    // imgError 的 key：栅格组用 group key，算法图像用文件名
    const keys = new Set([
      ...rasterGroups.value.map((g) => g.key),
      ...imageFiles.value.map((f) => f.name),
    ])
    for (const key of Object.keys(imgError)) {
      if (!keys.has(key)) delete imgError[key]
    }
  },
  { deep: true }
)

onMounted(loadRasterInfos)
onMounted(loadCsvPreviews)
</script>

<style scoped>
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

/* CSV 表格预览块：每个 CSV 一个 .csv-preview 卡片，含文件名 + 下载链接 +
   解析后的表格（限 50+5 行）。 */
.csv-preview {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: #fafafa;
  margin-bottom: 12px;
  overflow: hidden;
}
.csv-preview__header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  background: #fff;
  border-bottom: 1px solid var(--color-border);
  font-size: 13px;
}
.csv-preview__header .file-name {
  flex: 1;
  min-width: 0;
  word-break: break-all;
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
/* 多分片拼接组：标题上的「N 个分片」标记 */
.raster-card__shards {
  color: #1e40af;
  background: #dbeafe;
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 12px;
  white-space: nowrap;
}
/* 分片列表：逐个列出可下载的分片 TIF */
.raster-card__shard-list {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 12px;
  background: #fff;
  border-bottom: 1px solid var(--color-border);
  font-size: 12px;
}
.raster-card__shard-list-title {
  color: var(--color-muted);
}
.raster-card__shard {
  color: var(--color-primary);
  word-break: break-all;
}
.raster-card__actions {
  display: inline-flex;
  gap: 6px;
  margin-left: auto;
}

/* 缩略图 + 图例 横向布局：
   - 桌面：缩略图左（自适应宽度），图例右（固定 220px）
   - 窄屏：纵向堆叠
   - 缩略图和图例都按自身内容决定高度，让父容器高度 = max(缩略图, 图例) */
.raster-card__body {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  padding: 12px;
  background: #fff;
  align-items: stretch;
}
.raster-card__img-wrap {
  flex: 1 1 320px;
  min-width: 0;
  display: block;
  padding: 0;
  margin: 0;
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  cursor: zoom-in;
  text-align: center;
  align-self: flex-start;
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

/* 右侧图例 */
.raster-card__legend {
  flex: 0 0 220px;
  min-width: 200px;
  font-size: 12px;
  color: var(--color-text);
}
.legend {
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: #f8fafc;
  padding: 10px 12px;
}
.legend__title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-weight: 600;
  font-size: 12px;
}
.legend__title-text {
  color: var(--color-text);
}
.legend__type-badge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
}
.legend__type-badge--continuous { background: #ede9fe; color: #5b21b6; }
.legend__type-badge--binary      { background: #f1f5f9; color: #334155; }
.legend__type-badge--categorical { background: #dbeafe; color: #1e40af; }
.legend__loading {
  font-size: 12px;
  color: var(--color-muted);
  padding: 10px 12px;
  border: 1px dashed var(--color-border);
  border-radius: 4px;
  text-align: center;
}

/* 连续型：色带 + 刻度 + 文字标注 */
.legend-continuous__bar {
  height: 12px;
  border-radius: 3px;
  border: 1px solid var(--color-border);
}
.legend-continuous__ticks {
  display: flex;
  justify-content: space-between;
  font-variant-numeric: tabular-nums;
  color: var(--color-muted);
  margin-top: 4px;
  font-size: 11px;
}
.legend-continuous__labels {
  display: flex;
  justify-content: space-between;
  font-weight: 600;
  margin-top: 2px;
  color: var(--color-text);
}
.legend__hint {
  margin-top: 8px;
  font-size: 11px;
  color: var(--color-muted);
  line-height: 1.4;
}

/* 二值 / 分类：色块 + 文字 */
.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
}
.legend-item:first-of-type { margin-top: 0; }
.legend-item__swatch {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 1px solid var(--color-border);
  border-radius: 2px;
  flex-shrink: 0;
}
.legend-item__label {
  flex: 1;
  min-width: 0;
}
.legend-item__count {
  color: var(--color-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* 像元统计表（在缩略图下方，仅 continuous 显示） */
.raster-card__stats {
  border-top: 1px solid var(--color-border);
  padding: 12px;
  background: #fafafa;
}
.raster-card__stats-heading {
  margin: 0 0 6px 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-muted);
}
.result-viewer__stats {
  border-collapse: collapse;
  font-size: 12px;
}
.result-viewer__stats th {
  text-align: left;
  padding: 2px 12px 2px 0;
  color: var(--color-muted);
  font-weight: 500;
}
.result-viewer__stats td {
  padding: 2px 0;
  font-variant-numeric: tabular-nums;
}

/* ------------------------------------------------------------------
 * Lightbox (仿 el-image 大图预览)
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
