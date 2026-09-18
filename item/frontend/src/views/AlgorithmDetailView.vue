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
        <div
          v-for="field in algo.params_schema"
          :key="field.key"
          class="form-row"
        >
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
        </div>

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

const datasetsStore = useDatasetsStore()

const hasUploading = computed(() =>
  Object.values(uploadingKeys).some(Boolean)
)

// History selections per field key. Only fields with `slot` get a list.
const historySelections = reactive({})

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
    // Refresh history so the dropdown reflects the new entry.
    if (field.slot) await refreshHistory(field)
  } catch (err) {
    uploadErrors[field.key] = err.userMessage || err.message || '上传失败'
  } finally {
    uploadingKeys[field.key] = false
  }
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
      if (f.type === 'file') {
        params[f.key] = ''
      } else {
        params[f.key] = f.default ?? ''
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

async function onSubmit() {
  submitError.value = ''
  const missing = (algo.value?.params_schema || [])
    .filter((f) => f.required && (params[f.key] === '' || params[f.key] == null))
    .map((f) => f.label)
  if (missing.length) {
    submitError.value = `请填写必填字段：${missing.join('、')}`
    return
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