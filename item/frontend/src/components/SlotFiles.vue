<template>
  <div>
    <p v-if="loading">加载中…</p>
    <p v-else-if="error" style="color: var(--color-danger);">{{ error }}</p>
    <p v-else-if="!groups.length" style="color: var(--color-muted);">该 slot 暂无文件</p>
    <div v-else>
      <div
        v-for="group in groups"
        :key="group.group_value || '_flat'"
        style="margin-bottom: 12px;"
      >
        <div
          v-if="group.group_value"
          style="font-size: 12px; color: var(--color-muted); margin-bottom: 4px;"
        >
          分组: <code>{{ group.group_value }}</code>
        </div>
        <table v-if="group.files.length" style="width: 100%;">
          <thead>
            <tr>
              <th style="width: 40%;">名称</th>
              <th>类型</th>
              <th>大小</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="file in group.files" :key="file.file_id">
              <td>
                <code style="font-size: 12px;">{{ file.name }}</code>
                <div style="font-size: 11px; color: var(--color-muted);">{{ file.path }}</div>
              </td>
              <td>{{ file.kind === 'directory' ? '目录' : '文件' }}</td>
              <td>{{ formatSize(file.size_bytes) }}</td>
              <td>
                <button
                  class="button"
                  style="background: var(--color-primary); padding: 4px 10px; font-size: 12px; margin-right: 6px;"
                  @click="$emit('use', { algorithmId, slot, path: file.path, fieldKey: pickFieldKey() })"
                >
                  用于算法
                </button>
                <button
                  class="button"
                  style="background: var(--color-danger); padding: 4px 10px; font-size: 12px;"
                  @click="onDelete(file)"
                >
                  删除
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useDatasetsStore } from '../stores/datasets'

const props = defineProps({
  algorithmId: { type: String, required: true },
  slot: { type: String, required: true },
  hasGroupValues: { type: Boolean, default: false }
})

const emit = defineEmits(['use', 'deleted'])

const store = useDatasetsStore()
const loading = ref(false)
const error = ref('')

const groups = computed(() => {
  const algo = store.byAlgorithm[props.algorithmId]
  if (!algo) return []
  const slotEntry = (algo.slots || []).find((s) => s.slot === props.slot)
  if (!slotEntry) return []
  return (slotEntry.groups || []).filter((g) => g.files && g.files.length)
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

const SLOT_TO_FIELD = {
  excel: 'excel_path',
  rasters: 'raster_dir',
  mask: 'cane_mask_path',
  threshold_json: 'threshold_json_path'
}

function pickFieldKey() {
  return SLOT_TO_FIELD[props.slot] || props.slot
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    await store.loadAlgo(props.algorithmId)
  } catch (err) {
    error.value = err.userMessage || err.message
  } finally {
    loading.value = false
  }
}

async function onDelete(file) {
  if (!confirm(`确定删除 ${file.name}？`)) return
  try {
    await store.remove(props.algorithmId, props.slot, file.file_id)
    emit('deleted')
  } catch (err) {
    error.value = err.userMessage || err.message
  }
}

onMounted(refresh)
</script>