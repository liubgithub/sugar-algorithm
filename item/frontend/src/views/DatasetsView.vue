<template>
  <div>
    <div class="card">
      <h2 class="card__title">我的数据</h2>
      <p style="color: var(--color-muted); font-size: 13px;">
        这里列出所有已上传到项目目录里的参数文件，按算法 + 用途（slot）组织。
        可直接复用运行，也可在算法详情页字段下方下拉选用。
      </p>
      <div style="margin: 12px 0; display: flex; gap: 8px; align-items: center;">
        <label style="font-size: 13px;">筛选算法：</label>
        <select class="input" style="max-width: 280px;" v-model="filterAlgo">
          <option value="">全部</option>
          <option v-for="a in store.summary.algorithms" :key="a.id" :value="a.id">
            {{ a.name }} ({{ a.id }})
          </option>
        </select>
        <button class="button" style="background: var(--color-muted);" @click="store.loadAll()">
          刷新
        </button>
      </div>
    </div>

    <div v-if="store.loading" class="card">加载中…</div>
    <div v-else-if="store.error" class="card" style="color: var(--color-danger);">
      {{ store.error }}
    </div>
    <div v-else-if="!filteredAlgos.length" class="card list__empty">
      暂无已上传的数据。
    </div>

    <div v-for="a in filteredAlgos" :key="a.id" class="card">
      <h3 class="card__title">
        {{ a.name }}
        <span style="font-weight: normal; font-size: 12px; color: var(--color-muted);">
          ({{ a.id }})
        </span>
      </h3>
      <p v-if="!a.slots.length" style="color: var(--color-muted); font-size: 13px;">
        该算法没有文件类型的参数。
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>用途（slot）</th>
            <th>条目数</th>
            <th>总大小</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="slot in a.slots" :key="slot.slot">
            <tr>
              <td><code>{{ slot.slot }}</code></td>
              <td>
                <span v-if="!slot.group_values?.length">{{ slot.file_count }}</span>
                <span v-else>
                  共 {{ slot.file_count }} 个，分布于 {{ slot.group_values.length }} 个分组
                </span>
              </td>
              <td>{{ formatSize(slot.size_bytes) }}</td>
              <td>
                <button
                  v-if="slot.file_count"
                  class="button"
                  style="background: var(--color-muted); padding: 4px 10px; font-size: 12px;"
                  @click="toggleExpand(`${a.id}/${slot.slot}`)"
                >
                  {{ expanded[`${a.id}/${slot.slot}`] ? '收起' : '展开' }}
                </button>
              </td>
            </tr>
            <tr v-if="expanded[`${a.id}/${slot.slot}`]">
              <td colspan="4" style="background: #fafbff; padding: 0;">
                <div style="padding: 8px 16px;">
                  <SlotFiles
                    :algorithm-id="a.id"
                    :slot="slot.slot"
                    :has-group-values="(slot.group_values?.length || 0) > 0"
                    @use="useInForm"
                    @deleted="onDeleted"
                  />
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useDatasetsStore } from '../stores/datasets'
import SlotFiles from '../components/SlotFiles.vue'

const store = useDatasetsStore()
const router = useRouter()
const filterAlgo = ref('')
const expanded = reactive({})

const filteredAlgos = computed(() => {
  if (!filterAlgo.value) return store.summary.algorithms || []
  return (store.summary.algorithms || []).filter((a) => a.id === filterAlgo.value)
})

function toggleExpand(key) {
  expanded[key] = !expanded[key]
}

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

function useInForm({ algorithmId, slot, path, fieldKey }) {
  router.push({
    path: `/algorithms/${algorithmId}`,
    query: { prefill: path, field: fieldKey || slot }
  })
}

function onDeleted() {
  store.loadAll()
}

onMounted(() => {
  store.loadAll()
})
</script>