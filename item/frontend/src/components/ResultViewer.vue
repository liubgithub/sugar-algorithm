<template>
  <div class="result-viewer">
    <div v-if="loading" class="result-viewer__status">加载预览中…</div>
    <div v-else-if="loadError" class="result-viewer__status" style="color: var(--color-danger);">
      预览加载失败：{{ loadError }}
    </div>
    <template v-else>
      <div class="result-viewer__toolbar">
        <label class="result-viewer__check">
          <input type="checkbox" v-model="layerVisible" />
          显示结果图层
        </label>
        <span class="result-viewer__type">
          <span class="badge" :class="typeBadge">{{ typeLabel }}</span>
          <code style="margin-left: 6px;">{{ filename }}</code>
          <span style="margin-left: 6px; color: var(--color-muted);">
            {{ info.width }}×{{ info.height }}
          </span>
        </span>
        <a
          :href="downloadUrl"
          :download="filename"
          class="button"
          style="padding: 4px 10px; font-size: 12px; margin-left: auto;"
        >
          下载原始 GeoTIFF
        </a>
      </div>

      <div ref="mapEl" class="result-viewer__map">
        <canvas
          v-show="layerVisible && overlayImage"
          ref="canvasEl"
          class="result-viewer__overlay"
        />
      </div>

      <div v-if="info.type === 'categorical' || info.type === 'binary'" class="result-viewer__legend">
        <h4 style="font-size: 12px; margin: 0 0 6px 0;">图例</h4>
        <div class="result-viewer__legend-grid">
          <div v-for="(color, cls) in info.classes" :key="cls" class="result-viewer__legend-item">
            <span class="result-viewer__swatch" :style="{ background: color }" />
            <span>类别 {{ cls }}</span>
            <span v-if="info.type === 'categorical'" style="color: var(--color-muted); margin-left: 4px;">
              ({{ info.stats.class_counts[cls] ?? 0 }} px)
            </span>
            <span v-else-if="cls == 1 || cls == 255" style="color: var(--color-muted); margin-left: 4px;">
              ({{ info.stats.count_1 ?? 0 }} px)
            </span>
            <span v-else style="color: var(--color-muted); margin-left: 4px;">
              ({{ info.stats.count_0 ?? 0 }} px)
            </span>
          </div>
        </div>
      </div>

      <div v-else-if="info.type === 'continuous'" class="result-viewer__legend">
        <h4 style="font-size: 12px; margin: 0 0 6px 0;">统计</h4>
        <table class="result-viewer__stats">
          <tbody>
            <tr><th>min</th><td>{{ formatNum(info.stats.min) }}</td></tr>
            <tr><th>max</th><td>{{ formatNum(info.stats.max) }}</td></tr>
            <tr><th>mean</th><td>{{ formatNum(info.stats.mean) }}</td></tr>
            <tr><th>std</th><td>{{ formatNum(info.stats.std) }}</td></tr>
            <tr><th>p2</th><td>{{ formatNum(info.stats.p2) }}</td></tr>
            <tr><th>p98</th><td>{{ formatNum(info.stats.p98) }}</td></tr>
            <tr v-if="info.stats.nodata_count"><th>nodata</th><td>{{ info.stats.nodata_count }} px</td></tr>
          </tbody>
        </table>
      </div>

      <div v-if="pixelInfo" class="result-viewer__pixel">
        <strong>像元值：</strong>
        <span v-if="pixelInfo.value === null">NoData</span>
        <span v-else>{{ pixelInfo.value }}</span>
        <span style="color: var(--color-muted); margin-left: 8px;">
          ({{ pixelInfo.lon.toFixed(5) }}, {{ pixelInfo.lat.toFixed(5) }})
        </span>
        <button
          class="button"
          style="padding: 2px 8px; font-size: 11px; margin-left: 8px;"
          @click="clearPixel"
        >清除</button>
      </div>
      <div v-else class="result-viewer__hint">点击地图查询像元值。</div>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import 'ol/ol.css'
import Map from 'ol/Map'
import View from 'ol/View'
import TileLayer from 'ol/layer/Tile'
import OSM from 'ol/source/OSM'
import { fromLonLat, toLonLat } from 'ol/proj'
import { getCenter as getExtentCenter } from 'ol/extent'

import {
  fetchJobInfo,
  fetchJobPixelValue,
  jobFileUrl,
  jobPreviewUrl
} from '../api/jobFiles'

const props = defineProps({
  jobId: { type: String, required: true },
  filename: { type: String, required: true }
})

const mapEl = ref(null)
const canvasEl = ref(null)
const info = ref(null)
const loading = ref(true)
const loadError = ref('')
const layerVisible = ref(true)
const pixelInfo = ref(null)
const overlayImage = ref(null)  // HTMLImageElement once loaded

const downloadUrl = computed(() => jobFileUrl(props.jobId, props.filename))

const typeBadge = computed(() => {
  switch (info.value?.type) {
    case 'continuous': return 'badge--local'
    case 'binary': return 'badge--gee'
    case 'categorical': return 'badge--gee'
    default: return ''
  }
})

const typeLabel = computed(() => {
  switch (info.value?.type) {
    case 'continuous': return '连续'
    case 'binary': return '二值'
    case 'categorical': return '分类'
    default: return '?'
  }
})

let map = null

function formatNum(v) {
  if (v === null || v === undefined) return '—'
  if (typeof v !== 'number') return String(v)
  if (Math.abs(v) >= 1000 || (Math.abs(v) < 0.01 && v !== 0)) return v.toExponential(3)
  return v.toFixed(4)
}

/**
 * bounds from /info: [[minLon, minLat], [maxLon, maxLat]] (EPSG:4326).
 * Returns the corresponding extent in EPSG:3857.
 */
function boundsToMercatorExtent(bounds) {
  const sw = fromLonLat([bounds[0][0], bounds[0][1]])
  const ne = fromLonLat([bounds[1][0], bounds[1][1]])
  return [sw[0], sw[1], ne[0], ne[1]]
}

/**
 * Project the raster's geographic extent to canvas pixel coordinates that
 * match the OL viewport. Runs on every postrender so pan/zoom keeps the
 * overlay glued to its geographic footprint.
 */
function positionOverlay() {
  if (!map || !canvasEl.value || !info.value) return
  const extent = boundsToMercatorExtent(info.value.bounds)
  const topLeft = map.getPixelFromCoordinate([extent[0], extent[3]])
  const bottomRight = map.getPixelFromCoordinate([extent[2], extent[1]])
  if (!topLeft || !bottomRight) return
  const canvas = canvasEl.value
  canvas.style.left = `${topLeft[0]}px`
  canvas.style.top = `${topLeft[1]}px`
  canvas.style.width = `${Math.max(1, bottomRight[0] - topLeft[0])}px`
  canvas.style.height = `${Math.max(1, bottomRight[1] - topLeft[1])}px`
  console.debug('[ResultViewer] positioned canvas', {
    topLeft, bottomRight, styleW: canvas.style.width, styleH: canvas.style.height
  })
}

async function loadOverlayImage(url) {
  // Prefer fetch + Blob URL: works regardless of how the dev proxy handles
  // binary content. Fall back to direct Image() for browsers / proxies that
  // handle it transparently.
  try {
    const resp = await fetch(url, { credentials: 'same-origin' })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    if (!blob.type.startsWith('image/')) {
      throw new Error(`unexpected content-type: ${blob.type}`)
    }
    const objectUrl = URL.createObjectURL(blob)
    const img = await new Promise((resolve, reject) => {
      const i = new Image()
      i.onload = () => {
        console.log('[ResultViewer] preview image loaded via blob', { url, w: i.naturalWidth, h: i.naturalHeight })
        resolve(i)
      }
      i.onerror = (e) => reject(new Error('Image decode failed'))
      i.src = objectUrl
    })
    return img
  } catch (err) {
    console.warn('[ResultViewer] fetch-blob path failed, retrying direct Image()', err)
  }

  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => {
      console.log('[ResultViewer] preview image loaded directly', { url, w: img.naturalWidth, h: img.naturalHeight })
      resolve(img)
    }
    img.onerror = (e) => {
      console.error('[ResultViewer] preview image FAILED to load', { url, error: e })
      reject(new Error('preview image failed to load'))
    }
    img.src = url
  })
}

function drawOverlay() {
  if (!canvasEl.value || !overlayImage.value) {
    console.warn('[ResultViewer] drawOverlay skipped', { hasCanvas: !!canvasEl.value, hasImage: !!overlayImage.value })
    return
  }
  const canvas = canvasEl.value
  canvas.width = overlayImage.value.naturalWidth
  canvas.height = overlayImage.value.naturalHeight
  const ctx = canvas.getContext('2d')
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  ctx.drawImage(overlayImage.value, 0, 0)
  console.log('[ResultViewer] drawOverlay done', { w: canvas.width, h: canvas.height })
}

async function load() {
  loading.value = true
  loadError.value = ''
  overlayImage.value = null
  try {
    info.value = await fetchJobInfo(props.jobId, props.filename)
    if (!mapEl.value) return

    // Pre-load the PNG so canvas has pixels to draw before OL's first paint.
    const previewUrl = jobPreviewUrl(props.jobId, props.filename)
    overlayImage.value = await loadOverlayImage(previewUrl)

    if (map) {
      map.setTarget(undefined)
      map = null
    }

    const extent = boundsToMercatorExtent(info.value.bounds)

    map = new Map({
      target: mapEl.value,
      layers: [new TileLayer({ source: new OSM() })],
      view: new View({
        projection: 'EPSG:3857',
        center: getExtentCenter(extent),
        zoom: 12,
        minZoom: 2,
        maxZoom: 18
      })
    })
    map.getView().fit(extent, { padding: [32, 32, 32, 32], maxZoom: 18 })

    // Draw + position once OL has laid out its viewport, then keep it glued.
    await nextTick()
    drawOverlay()
    requestAnimationFrame(positionOverlay)
    map.on('postrender', positionOverlay)

    map.on('singleclick', (evt) => {
      const lonLat = toLonLat(evt.coordinate)
      fetchJobPixelValue(props.jobId, props.filename, lonLat[0], lonLat[1])
        .then((data) => {
          pixelInfo.value = { ...data, lon: lonLat[0], lat: lonLat[1] }
        })
        .catch(() => {
          pixelInfo.value = { value: null, type: info.value.type, lon: lonLat[0], lat: lonLat[1] }
        })
    })
  } catch (err) {
    loadError.value = err?.userMessage || err?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function clearPixel() {
  pixelInfo.value = null
}

watch(layerVisible, () => {
  /* v-show on the canvas handles visibility */
})

watch(() => [props.jobId, props.filename], () => {
  pixelInfo.value = null
  load()
})

onMounted(load)

onBeforeUnmount(() => {
  if (map) {
    map.setTarget(undefined)
    map = null
  }
})
</script>

<style scoped>
.result-viewer {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 12px;
  margin-bottom: 12px;
  background: #fafafa;
}
.result-viewer__toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.result-viewer__check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
}
.result-viewer__type {
  display: inline-flex;
  align-items: center;
  font-size: 13px;
}
.result-viewer__map {
  position: relative;
  width: 100%;
  height: 360px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: #ffffff;
  overflow: hidden;
}

.result-viewer__overlay {
  position: absolute;
  top: 0;
  left: 0;
  /* OpenLayers 的 .ol-viewport 自带 z-index: 10 且被追加在此 canvas
     之后，不显式抬高 z-index 时底图会完全盖住预览图。 */
  z-index: 11;
  pointer-events: none;
  image-rendering: pixelated;
}
.result-viewer__status {
  padding: 16px;
  text-align: center;
  color: var(--color-muted);
}
.result-viewer__legend {
  margin-top: 8px;
  font-size: 12px;
}
.result-viewer__legend-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
}
.result-viewer__legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.result-viewer__swatch {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 1px solid #ccc;
  border-radius: 2px;
}
.result-viewer__stats {
  border-collapse: collapse;
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
.result-viewer__pixel,
.result-viewer__hint {
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: 4px;
  font-size: 12px;
}
.result-viewer__pixel {
  background: #eef6ff;
  border: 1px solid #c8def5;
}
.result-viewer__hint {
  background: #f5f5f5;
  color: var(--color-muted);
}
</style>