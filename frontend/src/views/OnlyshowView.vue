<template>
  <div class="onlyshow">
    <div v-for="(block, index) in blocks" :key="block.id" class="card block">
      <div class="block__header">
        <input
          v-model="block.title"
          class="block__title-input"
          placeholder="展示图像"
          @input="saveBlocks"
        />
        <div
          v-if="(block.files && block.files[0] && block.files[0].imageDataUrl)"
          class="block__mode-toggle"
          role="tablist"
          aria-label="展示方式"
        >
          <button
            type="button"
            class="toggle-btn"
            :class="{ 'toggle-btn--active': (block.mode || 'continuous') === 'continuous' }"
            :disabled="block.uploading"
            role="tab"
            :aria-selected="(block.mode || 'continuous') === 'continuous'"
            @click="switchMode(block, 'continuous')"
          >连续值</button>
          <button
            type="button"
            class="toggle-btn"
            :class="{ 'toggle-btn--active': block.mode === 'unique' }"
            :disabled="block.uploading"
            role="tab"
            :aria-selected="block.mode === 'unique'"
            @click="switchMode(block, 'unique')"
          >唯一值</button>
        </div>
        <button
          v-if="blocks.length > 1"
          type="button"
          class="button button--ghost block__remove"
          @click="removeBlock(index)"
        >删除</button>
      </div>

      <textarea
        v-model="block.description"
        class="block__description"
        rows="3"
        placeholder="添加说明…"
        @input="saveBlocks"
      ></textarea>

      <div class="block__body">
        <!-- 左侧：合成预览图 + slot 上传列表 -->
        <div class="block__image-area">
          <div v-if="block.uploading" class="block__placeholder block__placeholder--loading">
            正在上传并重采样…
          </div>
          <img
            v-else-if="block.files[0] && block.files[0].imageDataUrl"
            :src="block.files[0].imageDataUrl"
            :alt="block.title"
            class="block__image"
          />
          <div v-else class="block__placeholder">尚未上传图像</div>

          <div class="block__slots">
            <div
              v-for="(slot, slotIndex) in block.files"
              :key="slotIndex"
              class="block__slot"
            >
              <label class="block__upload">
                <input
                  type="file"
                  accept=".tif,.tiff"
                  class="block__file"
                  :disabled="block.uploading"
                  @change="onSlotUpload($event, index, slotIndex)"
                />
                <span class="button" :class="{ 'button--disabled': block.uploading }">
                  {{ slot.imageDataUrl || slot.lastFileName ? '更新' : '上传' }}{{ slotIndex === 0 ? '第一张' : '第二张' }}
                </span>
              </label>
              <p v-if="slot.lastFileName" class="block__filename">
                <code>{{ slot.lastFileName }}</code>
              </p>
            </div>
            <button
              v-if="block.files.length < 2"
              type="button"
              class="button button--ghost block__add-slot"
              @click="addSecondSlot(block)"
            >+ 添加第二张图（拼接）</button>
            <button
              v-else
              type="button"
              class="button button--ghost block__add-slot"
              @click="removeSecondSlot(block)"
            >移除第二张图</button>
          </div>
          <p
            v-if="block.files.length > 1"
            class="block__hint"
          >已上传 {{ block.files.length }} 张图，按 extent 自动拼接为一张合成图。</p>
        </div>

        <!-- 右侧：色带 + 像元统计 -->
        <div class="block__legend">
          <template v-if="block.files && block.files[0] && block.files[0].stats">
            <div
              v-if="hasContinuousStats(block.files[0].stats)"
              class="legend"
            >
              <div class="legend__title legend__title--onlyshow">数值分布</div>

              <div class="legend-continuous">
                <div class="legend-continuous__bar" :style="barStyle"></div>
                <div class="legend-continuous__ticks">
                  <span>{{ fmtNum(block.files[0].stats.min) }}</span>
                  <span>{{ fmtNum(block.files[0].stats.mean) }}</span>
                  <span>{{ fmtNum(block.files[0].stats.max) }}</span>
                </div>
                <div class="legend-continuous__labels">
                  <span>低</span>
                  <span>中</span>
                  <span>高</span>
                </div>
                <div class="legend__hint">
                  {{ block.mode === 'unique' && block.files[0].type === 'continuous'
                      ? '唯一值过多（>32）已自动回退为连续模式'
                      : '颜色由深到浅表示数值由低到高' }}
                </div>
              </div>

              <table class="block__stats">
                <tbody>
                  <tr><th>min</th><td>{{ fmtNum(block.files[0].stats.min) }}</td></tr>
                  <tr><th>max</th><td>{{ fmtNum(block.files[0].stats.max) }}</td></tr>
                  <tr><th>mean</th><td>{{ fmtNum(block.files[0].stats.mean) }}</td></tr>
                  <tr><th>std</th><td>{{ fmtNum(block.files[0].stats.std) }}</td></tr>
                </tbody>
              </table>
            </div>
            <div
              v-else-if="block.files[0].type === 'unique' && uniqueLegend(block).length > 0"
              class="legend"
            >
              <div class="legend__title legend__title--onlyshow">唯一值图例</div>
              <div class="legend__uv-row">
                <label class="legend__uv-label">手动指定唯一值</label>
                <input
                  type="text"
                  class="legend__uv-input"
                  :value="(block.uniqueValues || []).join(', ')"
                  :placeholder="`如：10, 20, 30, 40, 50（留空 = 全部）`"
                  @change="onUniqueValuesChange(block, $event.target.value)"
                />
                <button
                  type="button"
                  class="legend__uv-clear"
                  v-if="(block.uniqueValues || []).length > 0"
                  @click="onUniqueValuesChange(block, '')"
                >清除</button>
              </div>
              <ul class="legend__list">
                <li
                  v-for="item in uniqueLegend(block)"
                  :key="item.key"
                  class="legend__list-item"
                >
                  <label
                    class="legend__swatch legend__swatch--btn"
                    :style="{ background: item.color }"
                    :title="`点击修改颜色（当前值 ${item.label}，已选 ${item.color}）`"
                  >
                    <input
                      type="color"
                      class="legend__color-input"
                      :value="normalizeColor(item.color)"
                      @input="onSwatchColorInput(block, item, $event.target.value)"
                    />
                  </label>
                  <span class="legend__value">{{ item.label }}</span>
                  <input
                    type="text"
                    class="legend__note-input"
                    :value="classNote(block, item)"
                    placeholder="备注（如：甘蔗）"
                    @input="setClassNote(block, item, $event.target.value)"
                  />
                  <span class="legend__count">{{ item.count }}</span>
                </li>
              </ul>
              <p class="legend__hint">点击色块选色；在右侧输入框里添加类别备注（如"甘蔗""水稻"），持久化到本地。</p>
            </div>
            <div v-else class="legend">
              <div class="legend__title legend__title--onlyshow">分类图例</div>
              <p class="legend__sub">{{ slotTypeLabel(block.files[0]) }}</p>
              <p class="legend__hint">栅格类型：{{ slotTypeLabel(block.files[0]) }}</p>
            </div>
          </template>
          <div v-else class="legend__empty">上传后将显示色带</div>
        </div>
      </div>
    </div>

    <button type="button" class="button button--add" @click="addBlock">
      + 添加展示块
    </button>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'

const STORAGE_KEY = 'onlyshow-blocks-v1'

// 缓存最近上传的原始 File 对象，按 (blockId, slotIndex) 索引。File 不能 JSON
// 序列化、不进 localStorage，所以放在外面。刷新页面后丢失，再次切换 mode /
// 添加第二张图时需要重新上传对应 slot（其他 slot 的缓存仍在）。
const fileCache = new Map() // key: `${blockId}#${slotIndex}` -> File

// IndexedDB file cache —— File 对象本身不能 JSON 序列化（localStorage 放不下），
// 所以用 IndexedDB 存 blob。刷新页面后 loadBlocks 从这里恢复 fileCache，
// 这样改色块 / 切 mode 都能直接 triggerRender，不用每次重新上传图。
const DB_NAME = 'onlyshow_files_v1'
const STORE_NAME = 'file_cache'
let _dbPromise = null
function openDB() {
  if (_dbPromise) return _dbPromise
  _dbPromise = new Promise((resolve, reject) => {
    if (typeof indexedDB === 'undefined') { reject(new Error('no IndexedDB')); return }
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME)
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
  return _dbPromise
}
async function dbSet(key, file) {
  try {
    const db = await openDB()
    await new Promise((res, rej) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).put(file, key)
      tx.oncomplete = res
      tx.onerror = () => rej(tx.error)
    })
    db.close()
  } catch (e) { console.warn('[onlyshow] IDB put failed:', e) }
}
async function dbGet(key) {
  try {
    const db = await openDB()
    const v = await new Promise((res, rej) => {
      const tx = db.transaction(STORE_NAME, 'readonly')
      const req = tx.objectStore(STORE_NAME).get(key)
      req.onsuccess = () => res(req.result || null)
      req.onerror = () => rej(req.error)
    })
    db.close()
    return v
  } catch (e) { console.warn('[onlyshow] IDB get failed:', e); return null }
}
async function dbDelete(key) {
  try {
    const db = await openDB()
    await new Promise((res, rej) => {
      const tx = db.transaction(STORE_NAME, 'readwrite')
      tx.objectStore(STORE_NAME).delete(key)
      tx.oncomplete = res
      tx.onerror = () => rej(tx.error)
    })
    db.close()
  } catch (e) { /* 静默：删除失败不影响主流程 */ }
}

function cacheKey(blockId, slotIndex) {
  return `${blockId}#${slotIndex}`
}

// 唯一值图例：色块改色。input 直接嵌在色块 label 里 —— 点击 label 会把 click
// 转发到 <input type="color">，打开系统选色器。比「全局隐藏 input + .click()」
// 可靠（Chrome / Edge 在 input 完全离屏时不响应程序 click）。
function normalizeColor(c) {
  // <input type="color"> 只接受 #rrggbb 小写形式。如果带 alpha / 大写 / 其它
  // 形式，要么截断到 6 位，要么兜底成灰色，避免 value 被忽略。
  if (typeof c !== 'string') return '#888888'
  const m = c.match(/^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$/)
  if (m) return '#' + m[1].toLowerCase()
  return '#888888'
}

// 在 classMetadata 里找到 valueKey 对应的 idx（classMetadata 用 idx 当 key，
// 不是 value —— 这是后端 _render_unique 的约定）。
function findClassMetaIdxByValueKey(slot, valueKey) {
  const meta = (slot && slot.classMetadata) || {}
  for (const k of Object.keys(meta)) {
    const m = meta[k] || {}
    const v = m.value
    if (v !== undefined && v !== null && String(parseInt(v, 10)) === valueKey) {
      return k
    }
  }
  return null
}

// 把 "#rrggbb" / "#rrggbbaa" 解析成 [r, g, b, a]。无法解析就返回 null。
function parseColor(s) {
  if (typeof s !== 'string') return null
  const m = s.match(/^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$/)
  if (!m) return null
  return [
    parseInt(m[1].slice(0, 2), 16),
    parseInt(m[1].slice(2, 4), 16),
    parseInt(m[1].slice(4, 6), 16),
    m[2] ? parseInt(m[2], 16) : 255,
  ]
}

// 把 #rrggbb(aa) 序列化成 RGBA 字节 key，用于 Map 查找。
function rgbaKey(r, g, b, a) {
  return `${r},${g},${b},${a}`
}

// 不依赖后端 / fileCache / IDB：用 Canvas 在前端重打图。
// 原理：原 imageDataUrl (PNG) 里的每个像素 RGBA 对应 classMetadata 里某个 idx 的
// color；按 idx 找到新 color 替换即可。透明像素（alpha=0，nodata）不动。
// 这样即使 fileCache 空（刷新页面后），改色块也能立刻看到图变化。
function repaintImageWithCustomColors(imageDataUrl, classMetadata, customColors) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onerror = () => reject(new Error('image decode failed'))
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = img.naturalWidth
      canvas.height = img.naturalHeight
      const ctx = canvas.getContext('2d')
      if (!ctx) { reject(new Error('canvas 2d ctx unavailable')); return }
      ctx.drawImage(img, 0, 0)
      const imgData = ctx.getImageData(0, 0, canvas.width, canvas.height)
      const px = imgData.data
      // 构建原色 → idx 映射 + idx → 新色映射
      const colorToIdx = new Map()
      const idxToNewColor = new Map()
      for (const k of Object.keys(classMetadata || {})) {
        const m = classMetadata[k] || {}
        const idx = parseInt(k, 10)
        const cOld = parseColor(m.color)
        if (!cOld || !Number.isInteger(idx)) continue
        colorToIdx.set(rgbaKey(cOld[0], cOld[1], cOld[2], cOld[3]), idx)
        const v = m.value
        const vk = v !== undefined && v !== null ? String(parseInt(v, 10)) : null
        const newColor = (vk && customColors && customColors[vk]) || m.color
        const cNew = parseColor(newColor) || cOld
        idxToNewColor.set(idx, cNew)
      }
      // 遍历像素替换
      for (let i = 0; i < px.length; i += 4) {
        if (px[i + 3] === 0) continue  // nodata 透明，跳过
        const k = rgbaKey(px[i], px[i + 1], px[i + 2], px[i + 3])
        const idx = colorToIdx.get(k)
        if (idx === undefined) continue
        const c = idxToNewColor.get(idx)
        if (!c) continue
        px[i] = c[0]; px[i + 1] = c[1]; px[i + 2] = c[2]; px[i + 3] = c[3]
      }
      ctx.putImageData(imgData, 0, 0)
      resolve(canvas.toDataURL('image/png'))
    }
    img.src = imageDataUrl
  })
}

function onSwatchColorInput(block, item, newColor) {
  if (!block || !item) return
  const next = { ...(block.customColors || {}) }
  next[item.valueKey] = newColor
  block.customColors = next

  // 立刻同步更新图例色块颜色（响应式 → 用户立刻看到图例变）。
  const slot = (block.files || [])[0]
  if (slot && slot.classMetadata) {
    const idx = findClassMetaIdxByValueKey(slot, item.valueKey)
    if (idx !== null) {
      slot.classMetadata = {
        ...slot.classMetadata,
        [idx]: { ...slot.classMetadata[idx], color: newColor },
      }
    }
  }

  saveBlocks()

  // 策略 1：用 Canvas 在前端重打图（不依赖 fileCache / IDB / 后端）。
  // 即使刷新页面后 fileCache 空也能立即更新图。失败 / 不适用时降级到策略 2。
  if (slot && slot.imageDataUrl && slot.classMetadata) {
    repaintImageWithCustomColors(slot.imageDataUrl, slot.classMetadata, block.customColors)
      .then((newUrl) => {
        slot.imageDataUrl = newUrl
        saveBlocks()
      })
      .catch((err) => {
        console.warn('[onlyshow] canvas repaint failed:', err)
      })
  }

  // 策略 2：通知后端（如果 fileCache 还在），让权威响应覆盖前端的近似结果。
  // 这一步对前端重打图其实是冗余的，但保留它意味着：用户后续再加图 / 切 mode
  // 时用的是后端权威颜色（避免前端色板漂移）。
  if (fileCache.has(cacheKey(block.id, 0)) || fileCache.has(cacheKey(block.id, 1))) {
    triggerRender(block)
  }
}

// 唯一值图例：类别备注（"10 → 甘蔗"）。key 用 valueKey（即 str(int(value))），
// 与 custom_colors 的 key 对齐，便于一处管理。
function classNote(block, item) {
  if (!block || !item) return ''
  const notes = block.classNotes || {}
  return notes[item.valueKey] || ''
}
function setClassNote(block, item, text) {
  if (!block || !item) return
  const notes = { ...(block.classNotes || {}) }
  const t = (text || '').trim()
  if (t) {
    notes[item.valueKey] = t
  } else {
    delete notes[item.valueKey]
  }
  block.classNotes = notes
  saveBlocks()
  // 备注是展示用，不需要重新渲染图。
}

// 任一状态变化（mode / customColors / 第二张图增删）后重新调后端。
// 用 AbortController 取消上一次未完成的请求 —— 关键：色块选色时 <input
// type="color"> 的 @input 会被持续触发（用户拖 RGB 滑块时连续触发），如果
// 用 uploading flag 拦截后续，最后发出的请求用的颜色是用户「第一次拖动」
// 的，而不是最终选的颜色。改成 abort 后，每次都让最后一次生效。
const inflightControllers = new WeakMap()
async function triggerRender(block) {
  const ordered = []
  for (let i = 0; i < (block.files || []).length; i++) {
    const f = fileCache.get(cacheKey(block.id, i))
    if (f) ordered.push(f)
  }
  if (ordered.length === 0) return
  // 取消上一次未完成的请求
  const prev = inflightControllers.get(block)
  if (prev) prev.abort()
  const ctrl = new AbortController()
  inflightControllers.set(block, ctrl)
  block.uploading = true
  try {
    const form = new FormData()
    for (const f of ordered) form.append('files', f, f.name)
    form.append('mode', block.mode || 'continuous')
    if (block.customColors && Object.keys(block.customColors).length > 0) {
      form.append('custom_colors', JSON.stringify(block.customColors))
    }
    // 用户手动指定要渲染的 unique 值集合；空数组 → 后端按数组实际 unique 值渲染。
    if (block.mode === 'unique' && Array.isArray(block.uniqueValues) && block.uniqueValues.length > 0) {
      form.append('unique_values', JSON.stringify(block.uniqueValues))
    }
    const resp = await fetch('/api/preview/tif', { method: 'POST', body: form, signal: ctrl.signal })
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try { const e = await resp.json(); if (e && e.detail) detail = e.detail } catch {}
      throw new Error(detail)
    }
    const data = await resp.json()
    if (!data.preview_png_base64) throw new Error('后端未返回预览图像')
    // 如果这次请求已被新的请求取消，就不要写回（避免旧响应覆盖新状态）。
    if (ctrl.signal.aborted) return
    // 把后端返回的图 / stats 写回 block.files[0]（合成图只显示在 slot 0 位置）；
    // slot 1 的 imageDataUrl / stats 仍保留自己的，但 imageDataUrl 会被合成图覆盖。
    block.files[0].imageDataUrl = `data:image/png;base64,${data.preview_png_base64}`
    block.files[0].type = data.type || ''
    if (hasContinuousStats(data.stats)) {
      block.files[0].stats = {
        min: data.stats.min, max: data.stats.max,
        mean: data.stats.mean, std: data.stats.std,
      }
    } else {
      block.files[0].stats = data.stats || null
    }
    block.files[0].classes = data.classes || null
    block.files[0].classMetadata = data.class_metadata || null
    saveBlocks()
  } catch (err) {
    if (err && err.name === 'AbortError') return
    console.error(err)
    window.alert('TIF 渲染失败：' + (err && err.message ? err.message : err))
  } finally {
    if (inflightControllers.get(block) === ctrl) {
      inflightControllers.delete(block)
      block.uploading = false
    }
  }
}

// 与 frontend/src/components/ResultRenderer.vue:477 保持一致（11 个采样点）。
const VIRIDIS_RGB = [
  [0x44, 0x01, 0x54], [0x48, 0x28, 0x78], [0x3e, 0x49, 0x89], [0x31, 0x68, 0x8e], [0x26, 0x82, 0x8e],
  [0x1f, 0x9e, 0x89], [0x35, 0xb7, 0x79], [0x6d, 0xcd, 0x59], [0xb4, 0xde, 0x2c], [0xfd, 0xe7, 0x25],
  [0xfd, 0xe7, 0x25],
]

const barStyle = {
  background: `linear-gradient(to right, ${VIRIDIS_RGB
    .map((c, i) => `rgb(${c[0]},${c[1]},${c[2]}) ${(i / (VIRIDIS_RGB.length - 1)) * 100}%`)
    .join(', ')})`,
}

const HEADER_TITLE = '广西2025一次性估产数据'
const HEADER_DESCRIPTION = '展示已有数据。每块独立编辑标题与上传 TIF 图像；上传后将自动生成色带与统计信息，下次打开仍将保留上次上传结果。'

const blocks = reactive([])

function makeId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
}

function emptySlot() {
  return {
    imageDataUrl: '',
    lastFileName: '',
    stats: null,
    type: '',
    classes: null,
    classMetadata: null,
  }
}

function makeBlock(isHeader = false) {
  return {
    id: makeId(),
    title: isHeader ? HEADER_TITLE : '展示图像',
    description: isHeader ? HEADER_DESCRIPTION : '',
    // 一个 block 最多两个影像 slot；slot 0 必存在，slot 1 可选。
    files: [emptySlot()],
    mode: 'continuous', // 'continuous' | 'unique'
    customColors: {}, // { [valueStr]: '#rrggbb' }
    uniqueValues: [], // [int/float...] — 用户手动指定要渲染的栅格唯一值；空 = 全部
    classNotes: {}, // { [valueStr]: string } — 类别名备注，如 {"10": "甘蔗"}
    uploading: false,
  }
}

async function loadBlocks() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed) && parsed.length > 0) {
        for (const b of parsed) {
          // 兼容旧版缓存：缺字段时补默认；uploading 永远重置成 false。
          // 旧版用 imageDataUrl/lastFileName/stats/type/classes/classMetadata
          // 这些顶层字段；新版统一搬到 files[0]，新增 slot 1 占位。
          const files = []
          if (b.imageDataUrl || b.lastFileName || b.stats || b.type || b.classes || b.classMetadata) {
            files.push({
              imageDataUrl: b.imageDataUrl || '',
              lastFileName: b.lastFileName || '',
              stats: b.stats || null,
              type: b.type || '',
              classes: b.classes || null,
              classMetadata: b.classMetadata || null,
            })
          }
          if (Array.isArray(b.files) && b.files.length > 0) {
            for (const f of b.files) files.push({ ...emptySlot(), ...f })
          }
          if (files.length === 0) files.push(emptySlot())
          blocks.push({
            uploading: false,
            mode: 'continuous',
            customColors: {},
            uniqueValues: [],
            classNotes: {},
            ...b,
            files,
          })
        }
        return
      }
    }
  } catch (err) {
    console.warn('Onlyshow: 读取本地缓存失败', err)
  }
  // 缓存不存在或解析失败：保证页面不空，给一个默认的「标题块」。
  if (blocks.length === 0) blocks.push(makeBlock(true))

  // 从 IndexedDB 恢复原始 file blob（fileCache 在刷新后是空的，
  // 没有这一步的话用户改色 / 切 mode 都无法 triggerRender）。
  // 每个 block 最多 2 个 slot，并行拉；恢复后如发现 customColors 与图上
  // 实际颜色不一致，自动重渲一次，让图追上用户最后一次选的颜色。
  const fetchJobs = []
  for (const b of blocks) {
    fetchJobs.push((async () => {
      const k0 = cacheKey(b.id, 0)
      const k1 = cacheKey(b.id, 1)
      const [f0, f1] = await Promise.all([dbGet(k0), dbGet(k1)])
      if (f0 && !fileCache.has(k0)) fileCache.set(k0, f0)
      if (f1 && !fileCache.has(k1)) fileCache.set(k1, f1)
      // 文件恢复成功 + 用户之前存了 customColors + 但图还没追上 → 重渲。
      // 这样刷新后改色块 / 切 mode 都能立即更新图，不需要重新上传图。
      const hasFile = fileCache.has(k0) || fileCache.has(k1)
      const hasCustom = b.customColors && Object.keys(b.customColors).length > 0
      const hasUV = Array.isArray(b.uniqueValues) && b.uniqueValues.length > 0
      const slot = (b.files || [])[0]
      const cm = (slot && slot.classMetadata) || {}
      let inSync = !hasCustom && !hasUV
      if (!inSync) {
        // 检查每个用户改过的 value，classMetadata 里对应 idx 的 color 是否一致。
        for (const [vk, wantColor] of Object.entries(b.customColors || {})) {
          let matched = false
          for (const k of Object.keys(cm)) {
            const m = cm[k] || {}
            if (m.value !== undefined && m.value !== null && String(parseInt(m.value, 10)) === vk) {
              if (m.color === wantColor) { matched = true }
              break
            }
          }
          if (!matched) { inSync = false; break }
          inSync = true
        }
      }
      if (hasFile && !inSync) {
        // 用最新 customColors 触发重渲，abort 上一次；后端响应回来后图追上。
        triggerRender(b)
      }
    })())
  }
  await Promise.all(fetchJobs)
}

function saveBlocks() {
  // 持久化前清掉 uploading 状态，避免页面刷新后还显示"处理中"
  const lite = blocks.map((b) => ({
    id: b.id,
    title: b.title,
    description: b.description,
    files: (b.files || []).map((f) => ({
      imageDataUrl: f.imageDataUrl || '',
      lastFileName: f.lastFileName || '',
      stats: f.stats || null,
      type: f.type || '',
      classes: f.classes || null,
      classMetadata: f.classMetadata || null,
    })),
    mode: b.mode || 'continuous',
    customColors: b.customColors || {},
    uniqueValues: Array.isArray(b.uniqueValues) ? b.uniqueValues : [],
    classNotes: b.classNotes || {},
  }))
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(lite))
  } catch (err) {
    // localStorage 写满时降级：只持久化元数据，去掉 imageDataUrl（base64 PNG 可能 200KB+）。
    // 用户下次打开仍能看到标题与说明，但需要重新上传。
    console.warn('Onlyshow: 本地缓存写入失败', err)
    const meta = lite.map((b) => ({
      id: b.id,
      title: b.title,
      description: b.description,
      files: b.files.map(() => emptySlot()),
      mode: b.mode || 'continuous',
      customColors: b.customColors || {},
      uniqueValues: Array.isArray(b.uniqueValues) ? b.uniqueValues : [],
      classNotes: b.classNotes || {},
    }))
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(meta))
    } catch (err2) {
      console.warn('Onlyshow: 降级持久化也失败', err2)
    }
  }
}

function addBlock() {
  blocks.push(makeBlock(false))
  saveBlocks()
}

function removeBlock(index) {
  const removed = blocks[index]
  if (removed) {
    // 清空该 block 所有 slot 的文件缓存（内存 + IndexedDB）。
    for (const key of Array.from(fileCache.keys())) {
      if (key.startsWith(`${removed.id}#`)) {
        fileCache.delete(key)
        dbDelete(key)
      }
    }
  }
  blocks.splice(index, 1)
  saveBlocks()
}

function addSecondSlot(block) {
  if (!block || block.files.length >= 2) return
  block.files.push(emptySlot())
  saveBlocks()
}
function removeSecondSlot(block) {
  if (!block || block.files.length < 2) return
  block.files.pop()
  const key = cacheKey(block.id, 1)
  fileCache.delete(key)
  dbDelete(key)
  saveBlocks()
  // 移除后还有 slot 0，触发重新渲染（用 slot 0 单独跑）
  if (fileCache.has(cacheKey(block.id, 0))) triggerRender(block)
}

function hasContinuousStats(stats) {
  return stats
    && Number.isFinite(stats.min)
    && Number.isFinite(stats.max)
    && Number.isFinite(stats.mean)
}

function blockTypeLabel(_block) {
  // 旧版兼容函数，保留以防外部直接 import；当前模板用 slotTypeLabel。
  return '未知'
}

function slotTypeLabel(slot) {
  if (!slot) return '未知'
  if (slot.type === 'continuous') return '连续'
  if (slot.type === 'binary') return '二值'
  if (slot.type === 'categorical') return '分类'
  if (slot.type === 'unique') return '唯一值'
  return slot.type || '未知'
}

function uniqueLegend(block) {
  // 拿当前块主图（slot 0）的 classMetadata，扁平化成 [{ key, color, label, count }]。
  // 后端 unique 模式：classMetadata 的 key 是排序后的索引 0..N-1，value 里
  // 有 color / value / label。categorical 模式：key 是原始类别值。
  // 我们这里再附一个 valueKey 字段 = 原始栅格值（int/float 序列化后），
  // 用于 pickColorFor 与后端 custom_colors 的 key 对齐（custom_colors 按
  // 「值的整数表示」查找，不是按 idx）。
  const slot = (block.files || [])[0] || {}
  const meta = slot.classMetadata || {}
  const counts = (slot.stats && slot.stats.class_counts) || {}
  const entries = []
  for (const key of Object.keys(meta)) {
    const m = meta[key] || {}
    const rawValue = m.value
    // 后端 custom_colors 用 str(int(u)) 匹配；这里也用同样的字符串。
    // valueKey 是送到后端的 lookup key（unique 模式下 = str(int(value))）；
    // key 仅用于 v-for。
    const valueKey = rawValue !== undefined && rawValue !== null
      ? String(parseInt(rawValue, 10))
      : key
    entries.push({
      key,
      valueKey,
      color: typeof m.color === 'string' ? m.color : '#999999',
      label: m.label || `类别 ${key}`,
      count: typeof counts[key] === 'number' ? counts[key] : 0,
    })
  }
  return entries
}

async function onSlotUpload(event, blockIndex, slotIndex) {
  const file = event.target.files && event.target.files[0]
  event.target.value = ''
  if (!file) return
  const block = blocks[blockIndex]
  if (!block) return
  // 确保 slot 存在
  while (block.files.length <= slotIndex) block.files.push(emptySlot())
  block.files[slotIndex].lastFileName = file.name
  const key = cacheKey(block.id, slotIndex)
  fileCache.set(key, file)
  // 同步存到 IndexedDB，让刷新页面后 loadBlocks 能恢复 fileCache。
  await dbSet(key, file)
  await triggerRender(block)
}

async function switchMode(block, newMode) {
  if (!block || block.uploading) return
  if ((block.mode || 'continuous') === newMode) return
  block.mode = newMode
  saveBlocks()
  // 任意 slot 有缓存的文件就重新渲染
  const has = fileCache.has(cacheKey(block.id, 0)) || fileCache.has(cacheKey(block.id, 1))
  if (has) await triggerRender(block)
}

// 把用户输入的字符串（"10, 20, 30" / "10 20 30" / "10，20"）解析成数字数组。
// 解析失败 / 空字符串 → 返回空数组（让后端按栅格实际 unique 值渲染）。
function parseUniqueValuesInput(text) {
  if (!text || !text.trim()) return []
  // 支持中英文逗号 / 空格 / 分号分隔
  const parts = text.split(/[,，;；\s]+/).filter(Boolean)
  const out = []
  for (const p of parts) {
    const n = Number(p)
    if (Number.isFinite(n)) out.push(n)
  }
  return out
}

// 用户在图例上方「手动指定唯一值」输入框触发：更新 block.uniqueValues 并重新渲染。
async function onUniqueValuesChange(block, text) {
  if (!block || block.uploading) return
  const next = parseUniqueValuesInput(text)
  // 用 JSON 比对，避免数字数组在 reactive 视图里每次都触发 watch
  const same = JSON.stringify(next) === JSON.stringify(block.uniqueValues || [])
  block.uniqueValues = next
  saveBlocks()
  if (!same) {
    const has = fileCache.has(cacheKey(block.id, 0)) || fileCache.has(cacheKey(block.id, 1))
    if (has) await triggerRender(block)
  }
}

function fmtNum(v) {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000 || (Math.abs(v) < 0.01 && v !== 0)) return v.toExponential(3)
  if (Number.isInteger(v)) return v.toLocaleString('en-US')
  return v.toFixed(4)
}

onMounted(() => { loadBlocks() })
</script>

<style scoped>
.onlyshow {
  display: flex;
  flex-direction: column;
}

.block {
  padding: 16px 20px;
}

.block__header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.block__title-input {
  flex: 1;
  min-width: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
  border: 1px solid transparent;
  background: transparent;
  padding: 4px 6px;
  border-radius: 4px;
  font-family: inherit;
}
.block__title-input:focus {
  outline: none;
  border-color: var(--color-border);
  background: #fff;
}
.block__title-input::placeholder {
  color: var(--color-muted);
  font-weight: 500;
}

.block__remove {
  flex-shrink: 0;
}

/* 展示方式切换：连续值 / 唯一值。复用一个 segmented control 样式，
   用 border + active 状态表达选中，避免再引第三方组件库。 */
.block__mode-toggle {
  display: inline-flex;
  flex-shrink: 0;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  overflow: hidden;
  background: #fff;
}
.toggle-btn {
  background: transparent;
  border: 0;
  padding: 4px 10px;
  font-size: 12px;
  line-height: 1.4;
  color: var(--color-muted);
  cursor: pointer;
  font-family: inherit;
  transition: background 0.15s ease, color 0.15s ease;
}
.toggle-btn:hover:not(:disabled) {
  background: rgba(37, 99, 235, 0.06);
  color: var(--color-primary);
}
.toggle-btn--active {
  background: var(--color-primary);
  color: #fff;
}
.toggle-btn--active:hover {
  background: var(--color-primary);
  color: #fff;
}
.toggle-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.toggle-btn + .toggle-btn {
  border-left: 1px solid var(--color-border);
}

/* 说明文字：textarea 编辑即写，rows 拉低些让它在视觉上更接近一段说明而非表单输入。 */
.block__description {
  display: block;
  width: 100%;
  margin: 0 0 12px 0;
  padding: 6px 8px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-muted);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  resize: vertical;
  min-height: 36px;
  box-sizing: border-box;
}
.block__description:hover {
  border-color: var(--color-border);
}
.block__description:focus {
  outline: none;
  border-color: var(--color-border);
  background: #fff;
  color: var(--color-text);
}
.block__description::placeholder {
  color: var(--color-muted);
}

.block__body {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.block__image-area {
  flex: 1 1 320px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.block__placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 200px;
  background: #f5f7fa;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  color: var(--color-muted);
  font-size: 13px;
}
.block__placeholder--loading {
  background: #eef2ff;
  border-color: var(--color-primary);
  color: var(--color-primary);
  font-weight: 500;
}

.block__image {
  display: block;
  width: 100%;
  height: auto;
  max-height: 480px;
  object-fit: contain;
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  image-rendering: pixelated;
}

.block__upload {
  display: inline-block;
  align-self: flex-start;
  cursor: pointer;
}
.block__upload .button {
  display: inline-block;
  pointer-events: none;
}
.block__file {
  display: none;
}
.block__file:disabled + .button,
.button--disabled {
  background: #9ca3af;
  cursor: not-allowed;
}

/* 两个 upload slot 并排：slot 0 / slot 1 各自占一列，加号按钮放第二行 */
.block__slots {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 8px;
}
.block__slot {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 120px;
}
.block__add-slot {
  align-self: center;
  font-size: 12px;
  padding: 4px 10px;
}
.block__hint {
  width: 100%;
  margin: 4px 0 0 0;
  font-size: 11px;
  color: var(--color-muted);
}

/* 隐藏的 color input 已废弃 —— 现在每个色块自带 <input type="color">，
   包在 <label> 里，点击 label 自动转发到 input。 */

.block__filename {
  margin: 0;
  font-size: 12px;
  color: var(--color-muted);
}
.block__filename code {
  color: var(--color-text);
  background: #f3f4f6;
  padding: 1px 6px;
  border-radius: 4px;
}

.block__legend {
  flex: 0 0 220px;
  min-width: 200px;
}

.legend {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: #f8fafc;
  padding: 10px 12px;
}

.legend__title--onlyshow {
  margin-bottom: 8px;
  font-weight: 600;
  font-size: 13px;
  color: var(--color-text);
}

.legend__sub {
  margin: 0 0 6px 0;
  font-size: 12px;
  color: var(--color-text);
}

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
  font-size: 11px;
}

.legend__hint {
  margin-top: 8px;
  font-size: 11px;
  color: var(--color-muted);
  line-height: 1.4;
}

/* 唯一值 / 分类图例：颜色块 + 标签 + 像元数。max-height + overflow 让
   unique 值特别多（接近 32）时仍能在固定区域内滚动，不撑爆 legend 卡片。 */
.legend__list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 220px;
  overflow-y: auto;
}
.legend__list-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: 12px;
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}
.legend__swatch {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid rgba(0, 0, 0, 0.12);
}

/* 唯一值图例上方的「手动指定唯一值」输入行 */
.legend__uv-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 4px 0 10px;
  flex-wrap: wrap;
}
.legend__uv-label {
  font-size: 12px;
  color: #555;
  flex-shrink: 0;
}
.legend__uv-input {
  flex: 1;
  min-width: 180px;
  padding: 4px 8px;
  font-size: 12px;
  border: 1px solid #d0d0d0;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.legend__uv-input:focus {
  outline: 2px solid rgba(76, 110, 245, 0.35);
  outline-offset: 1px;
  border-color: rgba(76, 110, 245, 0.6);
}
.legend__uv-clear {
  font-size: 12px;
  padding: 4px 8px;
  border: 1px solid #d0d0d0;
  background: #fafafa;
  border-radius: 4px;
  cursor: pointer;
  color: #555;
}
.legend__uv-clear:hover {
  background: #f0f0f0;
  border-color: #b0b0b0;
}
/* 当色块作为可点击元素渲染时（label 包裹 input[type=color]） */
.legend__swatch--btn {
  position: relative;
  cursor: pointer;
  padding: 0;
  background-clip: padding-box;
  transition: outline 0.15s ease, transform 0.15s ease;
}
.legend__swatch--btn:hover {
  outline: 2px solid var(--color-primary);
  outline-offset: 1px;
}
/* input 覆盖整个色块；opacity:0 但仍可点击 / 可获得焦点 →
   用户在色块区域任意一点点击都能打开系统选色器。 */
.legend__color-input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: pointer;
  opacity: 0;
}
.legend__value {
  flex: 0 0 auto;
  min-width: 36px;
  padding: 0 6px;
  border-radius: 3px;
  background: #f3f4f6;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: var(--color-text);
  text-align: center;
}
.legend__note-input {
  flex: 1;
  min-width: 0;
  padding: 3px 6px;
  font-size: 12px;
  border: 1px solid transparent;
  border-radius: 3px;
  background: transparent;
  color: var(--color-text);
  font-family: inherit;
}
.legend__note-input::placeholder {
  color: #b0b0b0;
}
.legend__note-input:hover {
  border-color: #e0e0e0;
  background: #fafafa;
}
.legend__note-input:focus {
  outline: none;
  border-color: rgba(76, 110, 245, 0.6);
  background: #fff;
  box-shadow: 0 0 0 2px rgba(76, 110, 245, 0.15);
}
.legend__count {
  flex-shrink: 0;
  color: var(--color-muted);
  font-size: 11px;
}

.legend__empty {
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  padding: 24px 12px;
  text-align: center;
  color: var(--color-muted);
  font-size: 13px;
  background: #fafbfc;
}

.block__stats {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: 12px;
}
.block__stats th,
.block__stats td {
  padding: 2px 0;
  text-align: left;
  font-variant-numeric: tabular-nums;
}
.block__stats th {
  color: var(--color-muted);
  font-weight: 500;
  padding-right: 12px;
  width: 60px;
}

.button--add {
  background: transparent;
  border: 1px dashed var(--color-border);
  color: var(--color-primary);
  padding: 12px;
  width: 100%;
  font-size: 14px;
  font-weight: 500;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.button--add:hover {
  background: rgba(37, 99, 235, 0.04);
  border-color: var(--color-primary);
}

/* 复用 ResultRenderer 的 button--ghost 风格，保证"删除"按钮视觉一致。 */
.button--ghost {
  background: transparent;
  color: var(--color-primary);
  border: 1px solid var(--color-border);
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 4px;
  cursor: pointer;
}
.button--ghost:hover {
  background: rgba(37, 99, 235, 0.06);
}
</style>