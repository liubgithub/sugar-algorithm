<template>
  <div class="card">
    <h2 class="card__title">任务列表</h2>
    <p v-if="store.loading">加载中…</p>
    <p v-else-if="store.error" style="color: var(--color-danger);">{{ store.error }}</p>
    <p v-else-if="!store.list.length" class="list__empty">暂无任务。</p>
    <table v-else class="jobs-table">
      <thead>
        <tr>
          <th>Job ID</th>
          <th>任务名</th>
          <th>算法</th>
          <th>状态</th>
          <th>进度</th>
          <th>创建</th>
          <th>开始</th>
          <th>完成</th>
          <th>消息</th>
          <th>备注</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="job in store.list"
          :key="job.job_id"
          :class="{ 'row--running': isRunning(job) }"
        >
          <td><code>{{ job.job_id.slice(0, 8) }}…</code></td>
          <td :title="job.name || defaultName(job)">
            <span v-if="job.name">{{ job.name }}</span>
            <span v-else style="color: var(--color-muted);">{{ defaultName(job) }}</span>
          </td>
          <td>{{ job.algorithm }}</td>
          <td>
            <span class="badge" :class="badgeClass(job)">
              {{ statusLabel(job.status) }}
            </span>
          </td>
          <td>{{ job.progress }}%</td>
          <td>{{ fmtTime(job.created_at) }}</td>
          <td>{{ fmtTime(job.started_at) }}</td>
          <td>{{ fmtTime(job.finished_at) }}</td>
          <td :title="job.message">{{ truncate(job.message, 30) }}</td>
          <td>
            <span v-if="job.note" :title="job.note">{{ truncate(job.note, 30) }}</span>
            <span v-else style="color: var(--color-muted);">—</span>
          </td>
          <td class="job-row__actions">
            <router-link :to="`/jobs/${job.job_id}`">详情</router-link>
            <button
              class="button button--ghost"
              type="button"
              @click="onEdit(job)"
            >编辑</button>
            <button
              v-if="job.status === 'queued'"
              class="button button--warn"
              type="button"
              @click="onCancel(job)"
            >取消</button>
            <button
              class="button button--danger"
              type="button"
              @click="onDelete(job)"
            >删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <button class="button" style="margin-top: 12px;" @click="store.loadList()">刷新</button>
  </div>
</template>

<script setup>
import { onMounted } from 'vue'
import { useJobsStore } from '../stores/jobs'

const store = useJobsStore()

const RUNNING_STATUSES = new Set(['running', 'queued', 'submitted'])

const STATUS_LABELS = {
  queued: '等待中',
  running: '运行中',
  submitted: '等待中',
  failed: '失败',
  completed: '完成',
  cancelled: '已取消',
}

function isRunning(job) {
  return RUNNING_STATUSES.has(job.status)
}

function badgeClass(job) {
  if (job.status === 'running') return 'badge--running badge--pulse'
  if (job.status === 'queued' || job.status === 'submitted') return 'badge--running'
  if (job.status === 'completed') return 'badge--ok'
  if (job.status === 'failed' || job.status === 'cancelled') return 'badge--danger'
  return ''
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status
}

function defaultName(job) {
  const t = job.created_at || ''
  // 形如 "2026-09-17T08:11:00Z" → "2026-09-17 08:11"
  const ymd = t.slice(0, 10)
  const hm = t.slice(11, 16)
  return `${job.algorithm} - ${ymd}${hm ? ' ' + hm : ''}`
}

function fmtTime(s) {
  if (!s) return '—'
  // 把 ISO 字符串截成 "MM-DD HH:mm"，日期部分太挤
  const date = s.slice(5, 10)
  const time = s.slice(11, 16)
  return `${date} ${time}`
}

function truncate(s, n) {
  if (!s) return ''
  return s.length > n ? s.slice(0, n) + '…' : s
}

async function onEdit(job) {
  // 二选一：编辑任务名 / 备注；两次 prompt 串起来，避免弹三层。
  const which = window.prompt(
    '编辑类型：输入 name 编辑任务名，输入 note 编辑备注',
    'name'
  )
  if (which === null) return
  const key = which.trim().toLowerCase()
  if (key !== 'name' && key !== 'note') {
    window.alert('只接受 name 或 note。')
    return
  }
  const current = key === 'name' ? (job.name || defaultName(job)) : (job.note || '')
  const next = window.prompt(
    key === 'name' ? '任务名（最多 120 字）' : '备注（最多 500 字）',
    current
  )
  if (next === null) return
  try {
    if (key === 'name') {
      await store.setName(job.job_id, next.trim())
    } else {
      await store.patchNote(job.job_id, next.trim())
    }
  } catch (err) {
    window.alert('保存失败：' + (err.userMessage || err.message))
  }
}

async function onCancel(job) {
  const ok = window.confirm(`确定取消任务 ${defaultName(job)}？此操作不可撤销。`)
  if (!ok) return
  try {
    await store.cancelJobAction(job.job_id)
  } catch (err) {
    const code = err.response?.status
    const detail = err.response?.data?.detail
    if (code === 409 && detail) {
      window.alert(`任务当前状态为「${statusLabel(detail.split("'")[1] || detail)}」，无法取消。`)
    } else {
      window.alert('取消失败：' + (err.userMessage || err.message))
    }
  }
}

async function onDelete(job) {
  const ok = window.confirm(`确定删除任务 ${defaultName(job)}？该任务的工作目录也会一并删除。`)
  if (!ok) return
  try {
    await store.removeJob(job.job_id)
  } catch (err) {
    window.alert('删除失败：' + (err.userMessage || err.message))
  }
}

onMounted(() => {
  store.loadList()
})
</script>

<style scoped>
.jobs-table {
  table-layout: auto;
  width: 100%;
  font-size: 13px;
}
.jobs-table th,
.jobs-table td {
  padding: 6px 8px;
  white-space: nowrap;
  vertical-align: middle;
}
.job-row__actions {
  display: flex;
  gap: 6px;
  align-items: center;
  white-space: nowrap;
}
.button--ghost {
  background: transparent;
  color: var(--color-primary);
  border: 1px solid var(--color-border);
  padding: 4px 10px;
  font-size: 12px;
}
.button--ghost:hover { background: rgba(37, 99, 235, 0.06); }
.button--danger {
  background: var(--color-danger);
  color: #fff;
  padding: 4px 10px;
  font-size: 12px;
}
.button--warn {
  background: #f59e0b;
  color: #fff;
  border: 1px solid #d97706;
  padding: 4px 10px;
  font-size: 12px;
}
</style>