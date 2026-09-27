<template>
  <div>
    <div class="card">
      <h2 class="card__title">
        {{ algo?.name || id }}
        <span
          v-if="algo"
          class="badge"
          :class="algo.type === 'LOCAL' ? 'badge--local' : 'badge--gee'"
          style="margin-left: 8px;"
        >
          {{ algo.type }}
        </span>
      </h2>
      <p>{{ algo?.description }}</p>
    </div>

    <div class="card">
      <h2 class="card__title">运行参数</h2>
      <p v-if="!algo">加载中…</p>
      <p v-else-if="!algo.params_schema?.length" class="list__empty">
        此算法无需参数，可直接提交。
      </p>
      <form v-else @submit.prevent="onSubmit">
        <!-- 基本参数：包含 file 上传、history 选择等复杂控件 -->
        <template v-for="field in basicFields" :key="field.key">
          <!-- 醒目提醒卡片：仅当 schema 中 type === 'notice' 时渲染；不会参与参数提交 -->
          <div
            v-if="field.type === 'notice'"
            class="form-notice"
            :class="`form-notice--${field.notice_type || 'info'}`"
          >
            <div class="form-notice__icon">{{ field.notice_icon || 'ℹ️' }}</div>
            <div class="form-notice__body">
              <strong v-if="field.label" class="form-notice__title">{{ field.label }}</strong>
              <p class="form-notice__text">{{ noticeTextFor(field) }}</p>
            </div>
          </div>

          <div v-else class="form-row">
            <label>
              {{ field.label }}
              <span v-if="field.required">*</span>
            </label>

            <!-- File / directory upload field -->
          <template v-if="field.type === 'file'">
            <!-- Directory picker: native OS folder selection (Chromium/Edge) -->
            <template v-if="field.kind === 'directory' && field.directory_picker">
              <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                <label class="button" style="cursor: pointer; display: inline-block;">
                  选择本地目录…
                  <input
                    type="file"
                    webkitdirectory
                    directory
                    multiple
                    style="display: none;"
                    @change="onDirPick(field, $event)"
                  />
                </label>
                <span style="font-size: 12px; color: var(--color-muted);">
                  或上传 zip：
                </span>
                <input
                  type="file"
                  :accept="field.accept || '*/*'"
                  style="display: inline-block; max-width: 240px;"
                  @change="onZipPick(field, $event)"
                />
              </div>
              <div v-if="field.group_by" style="font-size: 12px; color: var(--color-muted); margin-top: 6px;">
                将按参数
                <code>{{ field.group_by }}</code>
                （值：
                <select
                  class="input"
                  style="display: inline-block; width: auto; padding: 2px 6px;"
                  :value="groupValues[field.key] || ''"
                  @change="(e) => onGroupValueChange(field, e.target.value)"
                >
                  <option value="" disabled>— 选择分组值 —</option>
                  <option v-for="g in uniqueGroupValues" :key="g" :value="g">{{ g }}</option>
                </select>
                ）
                分目录存储
              </div>
            </template>
            <!-- Zip-only picker: 强制要求 .zip，确保 SHP 同伴文件齐备 -->
            <template v-else-if="field.kind === 'zip'">
              <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                <label class="button" style="cursor: pointer; display: inline-block;">
                  上传 .zip…
                  <input
                    type="file"
                    :accept="field.accept || '.zip'"
                    style="display: none;"
                    @change="onZipPick(field, $event)"
                  />
                </label>
                <span style="font-size: 12px; color: var(--color-muted);">
                  （Shapefile 必须含 .shp + .shx + .dbf，请全部打包上传）
                </span>
              </div>
            </template>
            <!-- Single-file picker (kind === 'file') -->
            <template v-else>
              <input
                type="file"
                class="input"
                :accept="field.accept || '*/*'"
                @change="onFilePick(field, $event)"
              />
            </template>

            <!-- Status row: just-uploaded -->
            <div v-if="uploads[field.key]" style="font-size: 12px; color: var(--color-muted); margin-top: 4px;">
              ✓ 已上传 {{ uploads[field.key].name }}
              <span v-if="uploads[field.key].kind === 'directory' || field.kind === 'directory'">（目录）</span>
              <span> · {{ formatSize(uploads[field.key].size_bytes) }}</span>
            </div>
            <div v-else-if="uploadErrors[field.key]" style="color: var(--color-danger); font-size: 12px;">
              {{ uploadErrors[field.key] }}
            </div>

            <!-- History dropdown: pick from previously uploaded project data -->
            <div
              v-if="field.slot && historySelections[field.key] && historySelections[field.key].length"
              style="margin-top: 8px; display: flex; gap: 8px; align-items: center;"
            >
              <span style="font-size: 12px; color: var(--color-muted);">或使用已上传历史：</span>
              <select
                class="input"
                style="max-width: 360px;"
                :value="selectedHistoryId[field.key] || ''"
                @change="(e) => onHistoryPick(field, e.target.value)"
              >
                <option value="" disabled>— 选择历史文件 —</option>
                <option v-for="h in historySelections[field.key]" :key="h.file_id" :value="h.file_id">
                  {{ h.label }} ({{ formatSize(h.size_bytes) }})
                </option>
              </select>
              <router-link
                v-if="field.slot"
                :to="`/datasets?algorithm=${encodeURIComponent(id)}&slot=${encodeURIComponent(field.slot)}`"
                style="font-size: 12px;"
              >
                管理 →
              </router-link>
            </div>

            </template>

          <!-- Number field -->
          <input
            v-else-if="field.type === 'number'"
            class="input"
            type="number"
            step="any"
            inputmode="decimal"
            v-model.number="params[field.key]"
            :placeholder="field.default ?? ''"
          />

          <!-- Date field -->
          <input
            v-else-if="field.type === 'date'"
            class="input"
            type="date"
            v-model="params[field.key]"
            :placeholder="field.default ?? ''"
          />

          <!-- Text / textarea -->
          <input
            v-else-if="field.type !== 'textarea'"
            class="input"
            type="text"
            v-model="params[field.key]"
            :placeholder="field.default ?? ''"
          />
          <textarea
            v-else
            class="textarea"
            v-model="params[field.key]"
            :placeholder="field.default ?? ''"
          />

          <p v-if="field.hint" class="form-row__hint">{{ field.hint }}</p>

          <!-- Saved GEE Assets picker: a checkbox + list of Assets the
               user has previously saved (auto-saved by the backend after
               a successful local-CSV → Asset upload). "使用" fills the
               text input above, "删除记录" only deletes the local pointer
               — the real Asset on GEE stays untouched. -->
          <div
            v-if="field.saved_picker"
            style="margin-top: 8px;"
            class="saved-gee-picker"
          >
            <label
              style="
                display: inline-flex;
                gap: 6px;
                align-items: center;
                cursor: pointer;
                font-size: 12px;
                color: var(--color-muted);
                user-select: none;
              "
            >
              <input
                type="checkbox"
                :checked="!!showSavedGeeAssets[field.key]"
                @change="(e) => onToggleSavedGeeAssets(field, e.target.checked)"
              />
              <span>{{ field.saved_picker.checkbox_label || '查看已保存 GEE Assets' }}</span>
            </label>

            <div
              v-if="showSavedGeeAssets[field.key]"
              style="margin-top: 8px;"
            >
              <p
                v-if="savedGeeLoading[field.key]"
                style="font-size: 12px; color: var(--color-muted); margin: 0;"
              >
                加载中…
              </p>
              <p
                v-else-if="savedGeeError[field.key]"
                style="font-size: 12px; color: var(--color-danger); margin: 0;"
              >
                {{ savedGeeError[field.key] }}
              </p>
              <p
                v-else-if="!savedGeeAssets[field.key] || !savedGeeAssets[field.key].length"
                style="font-size: 12px; color: var(--color-muted); margin: 0;"
              >
                还没有保存过的 GEE Asset。先上传一次本地 CSV，或在上方文本框直接填入 Asset ID。
              </p>
              <table
                v-else
                style="
                  font-size: 12px;
                  border-collapse: collapse;
                  width: 100%;
                  max-width: 760px;
                  background: #fff;
                "
              >
                <thead>
                  <tr style="text-align: left; color: var(--color-muted); border-bottom: 1px solid var(--color-border);">
                    <th style="padding: 6px 8px; font-weight: 600;">Asset ID</th>
                    <th style="padding: 6px 8px; font-weight: 600;">来源文件</th>
                    <th style="padding: 6px 8px; font-weight: 600;">创建时间</th>
                    <th style="padding: 6px 8px; font-weight: 600;">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(row, idx) in savedGeeAssets[field.key]"
                    :key="row.asset_id"
                    :style="idx % 2 === 1 ? 'background: #f8fafc;' : ''"
                  >
                    <td style="padding: 6px 8px; word-break: break-all; max-width: 360px;">
                      <code style="font-size: 11px;">{{ row.asset_id }}</code>
                    </td>
                    <td style="padding: 6px 8px; color: var(--color-muted);">
                      {{ row.source_filename || '—' }}
                    </td>
                    <td style="padding: 6px 8px; color: var(--color-muted); white-space: nowrap;">
                      {{ formatSavedAt(row.created_at) }}
                    </td>
                    <td style="padding: 6px 8px; white-space: nowrap;">
                      <button
                        type="button"
                        class="button button--small"
                        style="font-size: 12px; padding: 2px 10px; margin-right: 4px;"
                        @click="onUseSavedGeeAsset(field, row.asset_id)"
                      >使用</button>
                      <button
                        type="button"
                        class="button button--small button--ghost"
                        style="font-size: 12px; padding: 2px 10px;"
                        @click="onRemoveSavedGeeAsset(field, row.asset_id)"
                      >删除记录</button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          </div>
        </template>

        <!-- 高级参数：默认折叠（高级用户主动展开调整；默认行为与原硬编码一致） -->
        <details v-if="hasAdvanced" class="advanced-section">
          <summary class="advanced-section__summary">其他参数</summary>
          <div class="advanced-section__body">
            <template v-for="field in advancedFields" :key="field.key">
              <div
                v-if="field.type === 'notice'"
                class="form-notice"
                :class="`form-notice--${field.notice_type || 'info'}`"
              >
                <div class="form-notice__icon">{{ field.notice_icon || 'ℹ️' }}</div>
                <div class="form-notice__body">
                  <strong v-if="field.label" class="form-notice__title">{{ field.label }}</strong>
                  <p class="form-notice__text">{{ field.hint }}</p>
                </div>
              </div>
              <div v-else class="form-row">
                <label>
                  {{ field.label }}
                  <span v-if="field.required">*</span>
                </label>

              <!-- 高级参数仅支持 number / date / text / textarea；文件上传保留在基本参数区 -->
              <input
                v-if="field.type === 'number'"
                class="input"
                type="number"
                step="any"
                inputmode="decimal"
                v-model.number="params[field.key]"
                :placeholder="field.default ?? ''"
              />
              <input
                v-else-if="field.type === 'date'"
                class="input"
                type="date"
                v-model="params[field.key]"
                :placeholder="field.default ?? ''"
              />
              <input
                v-else-if="field.type !== 'textarea'"
                class="input"
                type="text"
                v-model="params[field.key]"
                :placeholder="field.default ?? ''"
              />
              <textarea
                v-else
                class="textarea"
                v-model="params[field.key]"
                :placeholder="field.default ?? ''"
              />

              <p v-if="field.hint" class="form-row__hint">{{ field.hint }}</p>
              </div>
            </template>
          </div>
        </details>

        <button class="button" :disabled="submitting || hasUploading">
          {{ submitting ? '提交中…' : (hasUploading ? '上传中…' : '提交运行') }}
        </button>
        <p v-if="submitError" style="color: var(--color-danger); margin-top: 12px;">
          {{ submitError }}
        </p>
      </form>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchAlgorithm } from '../api/algorithms'
import {
  fetchSavedGeeAssets,
  deleteSavedGeeAsset,
  verifyGeeAsset
} from '../api/gee'
import { createJob } from '../api/jobs'
import { uploadAsset } from '../api/uploads'
import { useDatasetsStore } from '../stores/datasets'

const route = useRoute()
const router = useRouter()
const id = route.params.id

const algo = ref(null)
const params = reactive({})
const uploads = reactive({})        // field key -> { name, kind, path, size_bytes }
const uploadErrors = reactive({})  // field key -> error string
const uploadingKeys = reactive({}) // field key -> bool
const groupValues = reactive({})   // field key -> current group_value text
const selectedHistoryId = reactive({}) // field key -> picked history file_id
const submitting = ref(false)
const submitError = ref('')

// ----------------------------------------------------------------------
// Saved GEE Assets — locally-persisted Asset pointers the user can
// re-use with one click. The backend writes a row here automatically
// after a successful local-CSV → Asset upload; the front-end only needs
// to list / use / delete.
// ----------------------------------------------------------------------
const savedGeeAssets = reactive({})   // field.key -> [asset, ...]
const savedGeeLoading = reactive({})  // field.key -> bool
const savedGeeError = reactive({})    // field.key -> error string
const showSavedGeeAssets = reactive({}) // field.key -> bool (checkbox state)

const datasetsStore = useDatasetsStore()

const hasUploading = computed(() =>
  Object.values(uploadingKeys).some(Boolean)
)

// 把 schema 切成「基本参数」和「高级参数（折叠）」两段。
// 后端 crop_threshold 在 schema 上用 `"advanced": true` 标记云量 / 百分位 / 尺度等
// 调参项；UI 折叠起来避免噪音，用户不动时也保留默认值。
// `hide_from_form: true` 的字段只用于算法内部 param key 占位，不渲染 UI
// （例如 crop_features 的 `samples_asset_id` 总是由 GEE picker 直接写）。
const basicFields = computed(() =>
  (algo.value?.params_schema || []).filter(
    (f) => !f.advanced && !f.hide_from_form
  )
)
const advancedFields = computed(() =>
  (algo.value?.params_schema || []).filter(
    (f) => f.advanced && !f.hide_from_form
  )
)
const hasAdvanced = computed(() => advancedFields.value.length > 0)

// History selections per field key. Only fields with `slot` get a list.
const historySelections = reactive({})

// XOR helper: when a field declares `xors_with: [other_key, ...]`, picking
// it should clear the counterpart(s). Returns true if `field` actually has
// any xors_with — used as a quick filter before iterating.
function clearXorPartners(field) {
  if (!field || !Array.isArray(field.xors_with) || !field.xors_with.length) return
  for (const partnerKey of field.xors_with) {
    if (params[partnerKey] !== undefined) params[partnerKey] = ''
    if (uploads[partnerKey] !== undefined) delete uploads[partnerKey]
    if (uploadErrors[partnerKey] !== undefined) delete uploadErrors[partnerKey]
    if (selectedHistoryId[partnerKey] !== undefined) selectedHistoryId[partnerKey] = ''
  }
}

// Collect every distinct group_value we already know about (existing rasters
// dirs) so the user can reuse them when uploading a new bundle.
const uniqueGroupValues = computed(() => {
  const out = new Set()
  for (const k of Object.keys(historySelections)) {
    for (const h of historySelections[k]) {
      if (h.group_value) out.add(h.group_value)
    }
  }
  return Array.from(out).sort()
})

function formatSize(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  let v = bytes
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(1)} ${units[i]}`
}

function getGroupValueFor(field) {
  // Prefer an explicit user selection, otherwise try to derive from current
  // params (e.g. target_month is typed into the form), otherwise fall back to
  // "default".
  return (
    groupValues[field.key] ||
    (field.group_by && params[field.group_by]) ||
    ''
  )
}

async function refreshHistory(field) {
  if (!field.slot) return
  if (!datasetsStore.byAlgorithm[id]?.slots?.find((s) => s.slot === field.slot)) {
    try {
      await datasetsStore.loadAlgo(id)
    } catch (_) {
      // store surfaces the error; just keep the empty list
    }
  }
  historySelections[field.key] = datasetsStore.flatSelections(id, field.slot)
  // If the param already points at a known history path, pre-select it.
  const current = params[field.key]
  if (current) {
    const hit = historySelections[field.key].find((h) => h.path === current)
    if (hit) selectedHistoryId[field.key] = hit.file_id
  }
}

async function doUpload(field, files, label) {
  uploadErrors[field.key] = ''
  uploadingKeys[field.key] = true
  try {
    const resp = await uploadAsset({
      algorithm: id,
      slot: field.slot,
      files,
      groupValue: field.group_by ? getGroupValueFor(field) || undefined : undefined,
      label
    })
    uploads[field.key] = resp
    params[field.key] = resp.path
    selectedHistoryId[field.key] = resp.file_id
    // XOR: picking a local file clears any GEE Asset counterpart and vice
    // versa. The schema declares `xors_with: [...]` on either side.
    clearXorPartners(field)
    // Refresh history so the dropdown reflects the new entry.
    if (field.slot) await refreshHistory(field)
  } catch (err) {
    uploadErrors[field.key] = err.userMessage || err.message || '上传失败'
  } finally {
    uploadingKeys[field.key] = false
  }
}

// ----------------------------------------------------------------------
// Saved GEE Assets UI — checkbox + list of locally-persisted Asset
// pointers. "使用" copies the id into the text input above; "删除记录"
// only removes the local row.
// ----------------------------------------------------------------------

function onToggleSavedGeeAssets(field, checked) {
  showSavedGeeAssets[field.key] = checked
  if (checked) loadSavedGeeAssets(field)
}

async function loadSavedGeeAssets(field) {
  if (!field?.saved_picker) return
  savedGeeLoading[field.key] = true
  savedGeeError[field.key] = ''
  try {
    const list = await fetchSavedGeeAssets(id)
    savedGeeAssets[field.key] = list
  } catch (err) {
    savedGeeError[field.key] = err.userMessage || err.message || '加载已保存 GEE Asset 失败'
    savedGeeAssets[field.key] = []
  } finally {
    savedGeeLoading[field.key] = false
  }
}

// "使用"：把选中行的 asset_id 写回文本框（与 picker 走同一个 target_key），
// 同时清掉本地 CSV / 上传历史，避免运行时同时有两个样本来源。
function onUseSavedGeeAsset(field, assetId) {
  const targetKey = field.saved_picker?.target_key
  if (targetKey) params[targetKey] = assetId
  clearXorPartners(field)
}

// "删除记录"：仅删除本系统表里的指针；绝不删 GEE 上的真实 Asset。
async function onRemoveSavedGeeAsset(field, assetId) {
  if (!window.confirm(`仅删除本系统保存的 GEE Asset 记录：\n\n${assetId}\n\nGEE 上的真实 Asset 不会被删除。是否继续？`)) {
    return
  }
  try {
    await deleteSavedGeeAsset(id, assetId)
    // 立即从本地列表里删掉对应行；后台再拉一次以保持与服务端同步。
    if (savedGeeAssets[field.key]) {
      savedGeeAssets[field.key] = savedGeeAssets[field.key].filter(
        (r) => r.asset_id !== assetId
      )
    }
    await loadSavedGeeAssets(field)
  } catch (err) {
    savedGeeError[field.key] = err.userMessage || err.message || '删除记录失败'
  }
}

// 后端 created_at 是 ISO UTC（"2025-01-23T04:56:12Z"），渲染成更短的本地时间。
function formatSavedAt(iso) {
  if (!iso) return '—'
  // 兼容带不带 Z 后缀；无时区时按 UTC 解析。
  const hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(iso)
  const d = new Date(hasTz ? iso : iso + 'Z')
  if (isNaN(d.getTime())) return iso
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function onFilePick(field, evt) {
  const file = evt.target.files?.[0]
  if (!file) return
  await doUpload(field, [file])
  // Reset the input so re-selecting the same file fires onchange again.
  evt.target.value = ''
}

async function onDirPick(field, evt) {
  const files = Array.from(evt.target.files || [])
  if (!files.length) return
  const label = files[0].webkitRelativePath?.split('/')[0] || ''
  await doUpload(field, files, label)
  evt.target.value = ''
}

async function onZipPick(field, evt) {
  const file = evt.target.files?.[0]
  if (!file) return
  await doUpload(field, [file])
  evt.target.value = ''
}

function onHistoryPick(field, fileId) {
  const hit = (historySelections[field.key] || []).find((h) => h.file_id === fileId)
  if (!hit) return
  selectedHistoryId[field.key] = fileId
  params[field.key] = hit.path
  uploads[field.key] = {
    name: hit.label,
    kind: hit.kind,
    path: hit.path,
    size_bytes: hit.size_bytes
  }
}

function onGroupValueChange(field, value) {
  groupValues[field.key] = value
}

onMounted(async () => {
  try {
    algo.value = await fetchAlgorithm(id)
    for (const f of algo.value.params_schema || []) {
      // notice 字段只是展示给用户的提醒卡片，不参与参数提交。
      if (f.type === 'notice') continue
      // hide_from_form 占位字段仍在 params 里初始化（算法要读），但不渲染 UI。
      if (f.type === 'file') {
        params[f.key] = ''
      } else {
        params[f.key] = f.default ?? ''
      }
    }
    // 初始化 saved-picker 的本地状态：默认收起，等用户勾选再异步取数据。
    for (const f of algo.value.params_schema || []) {
      if (f.saved_picker) {
        showSavedGeeAssets[f.key] = false
        savedGeeAssets[f.key] = []
      }
    }
    // Kick off history loads for any field with a slot.
    for (const f of algo.value.params_schema || []) {
      if (f.type === 'file' && f.slot) {
        refreshHistory(f)
      }
    }
    // Allow ?prefill=path&field=excel_path from the Datasets page to seed params.
    const prefillPath = route.query.prefill
    const prefillField = route.query.field
    if (prefillPath && prefillField) {
      params[prefillField] = String(prefillPath)
    }
  } catch (err) {
    submitError.value = err.userMessage || err.message
  }
})

// 解析「数字数组」字段。兼容以下输入：
//   1. 原生数组（程序化注入时）
//   2. JSON 字符串（"[1, 2, 3]"）
//   3. Python 风格的变量赋值（"min_values = [1, 2, ...]"），可直接从
//      「生成阈值」结果页一键复制过来；尾部多余的逗号会被自动剔除。
//   4. 逗号分隔的宽容格式（"[1, 2, 3]" 或 "1, 2, 3"），兜底用。
// 任一元素无法转为有限数字，抛出带字段名 + 索引的清晰错误信息。
function parseNumberArray(raw, label) {
  if (Array.isArray(raw)) {
    return raw.map((v, i) => {
      const n = Number(v)
      if (!Number.isFinite(n)) {
        throw new Error(`${label} 第 ${i + 1} 个值不是合法数字：${v}`)
      }
      return n
    })
  }
  if (typeof raw !== 'string') {
    throw new Error(`${label} 必须为数组`)
  }
  const trimmed = raw.trim()
  if (!trimmed) {
    throw new Error(`${label} 不能为空`)
  }

  // 抽取最外层 [...] 块（同时兼容 "min_values = [\n  1,\n  2,\n]" 与
  // "[1, 2, 3]"），并剥离 Python 允许的尾部逗号。括号内允许出现任意非
  // 方括号字符，或一层嵌套方括号（应对取值里偶尔出现 list 的极端情况）。
  const bracketMatch = trimmed.match(/\[(?:[^\[\]]|\[[^\[\]]*\])*\]/)
  let body
  if (bracketMatch) {
    body = bracketMatch[0]
  } else {
    // 没找到方括号时：降级到「去括号 + 按逗号切」的老宽容逻辑。
    body = `[${trimmed.replace(/^\[|\]$/g, '')}]`
  }
  // 去掉数组中最后一个元素后的尾随逗号（Python 允许，JSON 不允许）。
  body = body.replace(/,(\s*\])/g, '$1')

  let arr = null
  try {
    arr = JSON.parse(body)
  } catch (_) {
    throw new Error(
      `${label} 格式无法识别，请粘贴『生成阈值』算法输出的完整数组，例如：${label} = [...]`
    )
  }
  if (!Array.isArray(arr)) {
    throw new Error(
      `${label} 格式无法识别，请粘贴『生成阈值』算法输出的完整数组，例如：${label} = [...]`
    )
  }
  return arr.map((v, i) => {
    const n = Number(v)
    if (!Number.isFinite(n)) {
      throw new Error(`${label} 第 ${i + 1} 个值不是合法数字：${v}`)
    }
    return n
  })
}

// 当用户在年份字段输入合法年份时，动态显示基期 / 监测期窗口。
// 后端算法会按相同的规则自动生成日期窗口；前端把结果即时反馈给用户，
// 避免他们误以为年份是给"今天"使用。其它算法不受影响。
function buildHarvestWindowHint(yearNum) {
  if (!Number.isFinite(yearNum)) return ''
  const y = Math.trunc(yearNum)
  return `基期：${y}-10-01 → ${y}-11-30\n监测期：${y}-12-01 → ${y + 1}-04-21`
}

// 渲染 notice 字段的正文：仅对 harvest 算法的「时间窗口（自动）」
// 字段做动态渲染，其它 notice 仍走 schema 中的静态 hint。
function noticeTextFor(field) {
  if (
    id === 'harvest' &&
    field.type === 'notice' &&
    field.key === '__window_notice__'
  ) {
    const yearNum = Number(params.year)
    const derived = buildHarvestWindowHint(yearNum)
    return derived || field.hint || ''
  }
  return field.hint || ''
}

// crop_features 算法的提交前校验。前端能拦下来的错误尽量在前端拦，
// 这样用户不必等服务端往返就能看到清晰的中文错误。
function validateCropFeatures() {
  // GEE 项目 ID
  const geeId = String(params.gee_project_id ?? '').trim()
  if (!geeId) return '请填写 GEE 项目 ID'

  // 年份
  const yearN = Number(params.year)
  if (!Number.isFinite(yearN)) return '年份必须是有效数字'

  // 样本点文件 / GEE 样本点 Asset（二选一：要么本地 CSV，要么复用已有 GEE Asset）
  const hasLocalSample = !!String(params.samples_local_path || '').trim()
  const hasGeeSample = !!String(params.samples_asset_id || '').trim()
  if (!hasLocalSample && !hasGeeSample) {
    return '请上传本地 CSV 样本点文件 或 选择 GEE 上已上传的样本点 Asset（两者必须填一个）'
  }
  if (hasLocalSample && hasGeeSample) {
    return '本地 CSV 与 GEE 样本点 Asset 只能选其一，请清空另一个再提交'
  }

  // min_values / max_values
  let minV
  let maxV
  try {
    minV = parseNumberArray(params.min_values, 'min_values')
  } catch (err) {
    return err.message
  }
  try {
    maxV = parseNumberArray(params.max_values, 'max_values')
  } catch (err) {
    return err.message
  }
  if (minV.length !== 11) {
    return `min_values 应包含 11 个数值（特征顺序：B2、B3、B4、B8、B11、B12、NDVI、EVI、NDWI、VV、VH），当前 ${minV.length} 个`
  }
  if (maxV.length !== 11) {
    return `max_values 应包含 11 个数值（特征顺序：B2、B3、B4、B8、B11、B12、NDVI、EVI、NDWI、VV、VH），当前 ${maxV.length} 个`
  }
  if (minV.length !== maxV.length) {
    return 'min_values 与 max_values 长度不一致'
  }
  const BAND_LABELS = ['B2', 'B3', 'B4', 'B8', 'B11', 'B12', 'NDVI', 'EVI', 'NDWI', 'VV', 'VH']
  for (let i = 0; i < minV.length; i++) {
    if (maxV[i] <= minV[i]) {
      return `第 ${i + 1} 个值（${BAND_LABELS[i]}）：max_values(${maxV[i]}) 必须大于 min_values(${minV[i]})`
    }
  }

  // DEM
  if (params.dem_min === '' || params.dem_min == null) return '请填写 DEM 最小值'
  if (params.dem_max === '' || params.dem_max == null) return '请填写 DEM 最大值'
  const demMin = Number(params.dem_min)
  const demMax = Number(params.dem_max)
  if (!Number.isFinite(demMin)) return `DEM 最小值必须是数字：${params.dem_min}`
  if (!Number.isFinite(demMax)) return `DEM 最大值必须是数字：${params.dem_max}`
  if (demMin >= demMax) {
    return `DEM 最小值 (${demMin}) 必须小于 DEM 最大值 (${demMax})`
  }

  // 云量
  if (params.cloud_percentage !== '' && params.cloud_percentage != null) {
    const cloud = Number(params.cloud_percentage)
    if (!Number.isFinite(cloud) || cloud < 0 || cloud > 100) {
      return `Sentinel-2 最大云量必须在 0~100 之间：${params.cloud_percentage}`
    }
  }

  return null
}

async function onSubmit() {
  submitError.value = ''
  // notice 字段不参与必填检查（它只是展示给用户的提醒），其它必填字段必须填写。
  const missing = (algo.value?.params_schema || [])
    .filter((f) => f.required && f.type !== 'notice' && (params[f.key] === '' || params[f.key] == null))
    .map((f) => f.label)
  if (missing.length) {
    submitError.value = `请填写必填字段：${missing.join('、')}`
    return
  }
  // crop_features 算法专用校验（数组长度、max>min、DEM 范围、云量范围等）。
  if (id === 'crop_features') {
    const validationError = validateCropFeatures()
    if (validationError) {
      submitError.value = validationError
      return
    }
  }
  // 如果用户填写了 GEE Asset ID（且没选本地 CSV / 历史文件），先做一次可达性
  // 校验，把"Asset 不存在/无权限"这种错误立刻反馈给用户，省掉 30 分钟
  // 后端 GEE 任务失败才看到结果的过程。
  const manualAssetId = String(params.samples_asset_id || '').trim()
  if (manualAssetId) {
    try {
      const verifyResult = await verifyGeeAsset(
        id,
        manualAssetId,
        String(params.gee_project_id || '')
      )
      if (verifyResult && verifyResult.ok === false) {
        submitError.value = verifyResult.error || 'GEE Asset 校验失败'
        return
      }
    } catch (_) {
      // 校验接口本身失败时静默放过——后端 run() 还会再做一次兜底。
    }
  }
  submitting.value = true
  try {
    const payload = { ...params }
    for (const k of Object.keys(payload)) {
      if (payload[k] === '' || payload[k] == null) delete payload[k]
    }
    const res = await createJob(id, payload)
    router.push({ name: 'job-detail', params: { jobId: res.job_id } })
  } catch (err) {
    submitError.value = err.userMessage || err.message
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
/* 字段下方的小字提示，与 ResultRenderer/ThresholdResult 保持淡灰色一致 */
.form-row__hint {
  margin: 4px 0 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-muted);
}

/* 醒目提醒卡片：与 ThresholdResult 的 warning-card 视觉风格一致 */
.form-notice {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin: 14px 0;
  padding: 12px 14px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  background: #f8fafc;
}
.form-notice--warning {
  background: #fef3c7;
  border-color: #fcd34d;
}
.form-notice--info {
  background: #eef2ff;
  border-color: #c7d2fe;
}
.form-notice__icon {
  font-size: 20px;
  line-height: 1;
  margin-top: 2px;
  flex-shrink: 0;
}
.form-notice__body {
  flex: 1;
  min-width: 0;
}
.form-notice__title {
  display: block;
  font-size: 14px;
  margin-bottom: 4px;
  color: #92400e;
}
.form-notice--info .form-notice__title {
  color: #1e3a8a;
}
.form-notice__text {
  margin: 0;
  font-size: 13px;
  color: #78350f;
  line-height: 1.55;
}
.form-notice--info .form-notice__text {
  color: #1e293b;
}
.form-notice__text code {
  background: rgba(146, 64, 14, 0.1);
  padding: 1px 4px;
  border-radius: 3px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

/* 高级参数折叠区：浅色描边，跟主表单区隔开；默认折叠 */
.advanced-section {
  margin: 14px 0;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  background: #fafbfc;
}
.advanced-section__summary {
  cursor: pointer;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-muted);
  user-select: none;
  list-style: revert;
}
.advanced-section[open] .advanced-section__summary {
  border-bottom: 1px dashed var(--color-border);
  margin-bottom: 4px;
}
.advanced-section__body {
  padding: 4px 12px 8px 12px;
}
.advanced-section__body .form-row:last-child {
  margin-bottom: 0;
}
</style>