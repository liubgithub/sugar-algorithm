<template>
  <div>
    <div class="card">
      <h2 class="card__title">任务详情</h2>
      <p v-if="error" style="color: var(--color-danger);">{{ error }}</p>
      <p v-else-if="!job">加载中…</p>
      <div v-else>
        <p>
          <strong>任务名:</strong>
          <span v-if="job.name">{{ job.name }}</span>
          <span v-else style="color: var(--color-muted);">{{ defaultName(job) }}</span>
          <button
            class="button button--ghost"
            type="button"
            style="padding: 2px 10px; font-size: 12px; margin-left: 8px;"
            @click="onEditName"
          >编辑</button>
        </p>
        <p>
          <strong>Job ID:</strong> <code>{{ job.job_id }}</code>
        </p>
        <p><strong>算法:</strong> {{ job.algorithm }}</p>
        <p>
          <strong>状态:</strong>
          <span class="badge" :class="badgeClass(job.status)">{{ statusLabel(job.status) }}</span>
        </p>
        <div class="progress" style="margin: 8px 0;">
          <div class="progress__bar" :style="{ width: job.progress + '%' }"></div>
        </div>
        <p><strong>进度:</strong> {{ job.progress }}%</p>
        <p><strong>消息:</strong> {{ job.message || '—' }}</p>

        <div class="job-meta">
          <div>
            <strong>创建:</strong>
            <span>{{ fmtTimeFull(job.created_at) || '—' }}</span>
          </div>
          <div>
            <strong>开始:</strong>
            <span>{{ fmtTimeFull(job.started_at) || '—' }}</span>
          </div>
          <div>
            <strong>完成:</strong>
            <span>{{ fmtTimeFull(job.finished_at) || '—' }}</span>
          </div>
        </div>

        <p style="display: flex; align-items: center; gap: 8px;">
          <strong>备注:</strong>
          <span v-if="job.note">{{ job.note }}</span>
          <span v-else style="color: var(--color-muted);">—</span>
          <button
            class="button button--ghost"
            type="button"
            style="padding: 2px 10px; font-size: 12px;"
            @click="onEditNote"
          >编辑</button>
        </p>
      </div>
    </div>

    <div class="card" v-if="job && hasResult">
      <h2 class="card__title">运行结果</h2>
      <ResultRenderer
        :job-id="jobId"
        :algorithm="job.algorithm"
        :result="job.result || {}"
      />
    </div>

    <!-- 运行日志：默认折叠，避免把后台调试噪声灌给用户。失败排查时可展开。 -->
    <div class="card" v-if="job">
      <details>
        <summary>查看运行日志</summary>
        <p
          v-if="job.status === 'running' || job.status === 'queued' || job.status === 'submitted'"
          style="color: var(--color-muted); margin-top: 8px;"
        >
          算法运行中，日志会持续追加…
        </p>
        <pre class="job-log">{{ logText || '暂无日志输出。' }}</pre>
      </details>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { fetchJob, fetchJobLog, patchJob } from '../api/jobs'
import ResultRenderer from '../components/ResultRenderer.vue'

const route = useRoute()
const jobId = route.params.jobId
const job = ref(null)
const error = ref('')
const logText = ref('')
const logOffset = ref(0)
let timer = null

const STATUS_LABELS = {
  queued: '等待中',
  running: '运行中',
  submitted: '等待中',
  failed: '失败',
  completed: '完成',
  cancelled: '已取消',
}

const hasResult = computed(() => {
  const r = job.value?.result
  if (!r) return false
  return Object.keys(r).length > 0
})

function statusLabel(status) {
  return STATUS_LABELS[status] || status
}

function badgeClass(status) {
  if (status === 'running') return 'badge--running badge--pulse'
  if (status === 'queued' || status === 'submitted') return 'badge--running'
  if (status === 'completed') return 'badge--ok'
  if (status === 'failed' || status === 'cancelled') return 'badge--danger'
  return ''
}

function defaultName(j) {
  const t = j.created_at || ''
  const ymd = t.slice(0, 10)
  const hm = t.slice(11, 16)
  return `${j.algorithm} - ${ymd}${hm ? ' ' + hm : ''}`
}

function fmtTimeFull(s) {
  return s || ''
}

async function onEditNote() {
  if (!job.value) return
  const next = window.prompt('编辑备注（最多 500 字）', job.value.note || '')
  if (next === null) return
  try {
    job.value = await patchJob(jobId, { note: next.trim() })
  } catch (err) {
    window.alert('保存备注失败：' + (err.userMessage || err.message))
  }
}

async function onEditName() {
  if (!job.value) return
  const initial = job.value.name || defaultName(job.value)
  const next = window.prompt('任务名（最多 120 字）', initial)
  if (next === null) return
  try {
    job.value = await patchJob(jobId, { name: next.trim() })
  } catch (err) {
    window.alert('保存任务名失败：' + (err.userMessage || err.message))
  }
}

async function refreshLog() {
  // 增量拉取算法 stdout；日志面板尽力而为，失败不影响任务状态展示。
  try {
    const data = await fetchJobLog(jobId, logOffset.value)
    if (data.text) {
      logText.value += data.text
      logOffset.value = data.size
    }
  } catch (_) {
    /* 忽略日志接口错误 */
  }
}

async function load() {
  try {
    job.value = await fetchJob(jobId)
    await refreshLog()
  } catch (err) {
    error.value = err.userMessage || err.message
  }
}

onMounted(() => {
  load()
  timer = setInterval(() => {
    if (job.value && ['completed', 'failed', 'cancelled'].includes(job.value.status)) {
      clearInterval(timer)
      refreshLog() // 任务结束前再拉一次，收尾剩余日志
      return
    }
    load()
  }, 1500)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.job-meta {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  margin: 8px 0;
  font-size: 13px;
  color: var(--color-muted);
}
.job-meta strong {
  color: var(--color-text);
  margin-right: 4px;
}
.button--ghost {
  background: transparent;
  color: var(--color-primary);
  border: 1px solid var(--color-border);
  padding: 4px 10px;
  font-size: 12px;
}
.button--ghost:hover { background: rgba(37, 99, 235, 0.06); }
</style>