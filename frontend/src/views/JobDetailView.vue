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

        <!-- 通用失败详情：当后端把 GEE 任务原始错误（如
             "Unable to export features with null geometry. (Error code: 3)"）
             写到 message 时，把它渲染成醒目的预格式化块，方便用户一眼看清
             失败原因。GeeAuthRequired 走下面的特殊分支（带复制命令按钮）。 -->
        <div
          v-if="job.status === 'failed' && !geeAuthHint && job.message"
          class="gee-error-block"
        >
          <div class="gee-error-block__title">错误详情</div>
          <pre class="gee-error-block__msg">{{ job.message }}</pre>
        </div>

        <!-- GEE 认证不匹配时的快速操作：原始文案是后端 GeeAuthRequired 异常
             的 str()，里头包含 `earthengine authenticate --force` 命令。
             渲染成预格式化块保留换行，并提供"复制命令"按钮方便用户直接
             粘贴到本机终端执行。 -->
        <div
          v-if="job.status === 'failed' && geeAuthHint"
          class="gee-auth-hint"
        >
          <pre class="gee-auth-hint__msg">{{ job.message }}</pre>
          <button
            class="button button--ghost"
            type="button"
            style="padding: 2px 10px; font-size: 12px;"
            @click="onCopyGeeAuthCommand"
          >复制命令</button>
          <span
            v-if="copyFeedback"
            class="gee-auth-hint__copy-feedback"
          >已复制</span>
        </div>

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
      <!-- crop_threshold 算法走专用结果页：阈值结果需要按变量复制到后续代码，
           通用 ResultRenderer 只列指标卡不够直观。 -->
      <ThresholdResult
        v-if="isThresholdAlgorithm"
        :job-id="jobId"
        :algorithm="job.algorithm"
        :result="{ ...(job.result || {}), params: job.params || {} }"
        :status="job.status"
        :gee-project="job.params?.gee_project || ''"
        :job-message="job.message || ''"
      />
      <ResultRenderer
        v-else
        :job-id="jobId"
        :algorithm="job.algorithm"
        :result="displayResult"
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
import ThresholdResult from '../components/ThresholdResult.vue'

// 这两个算法 ID 都属于"GEE 动态阈值计算"类，结果结构一致（band_names + min/max_values + dem_min/max）。
// 后续若新增同类算法，只要往这里加 ID 即可复用 ThresholdResult。
const THRESHOLD_ALGORITHMS = new Set(['crop_threshold'])

// 特征提取（crop_features）算法在前端只展示核心 4 个指标卡，旧的 DB 行
// 里塞过 task_status / sample_asset_id / asset_id / output_csv / gee_task_id
// 等冗余项，这里按白名单过滤一次，让新旧数据渲染一致。
const CROP_FEATURES_ALGORITHM = 'time-series-features'
const CROP_FEATURES_CARD_WHITELIST = new Set([
  'year',
  'feature_count',
  'bands_per_month',
  'months',
])

function filterCropFeaturesCards(result) {
  if (!result || !result.metrics || !Array.isArray(result.metrics.cards)) {
    return result
  }
  const filtered = result.metrics.cards.filter(
    (c) => c && c.key != null && CROP_FEATURES_CARD_WHITELIST.has(c.key)
  )
  // 若白名单过滤后为空（理论上不会发生），退回原数组避免卡片区全空。
  const nextMetrics =
    filtered.length > 0 ? { ...result.metrics, cards: filtered } : result.metrics
  return { ...result, metrics: nextMetrics }
}

const displayResult = computed(() => {
  const base = job.value?.result || {}
  if ((job.value?.algorithm || '') === CROP_FEATURES_ALGORITHM) {
    return filterCropFeaturesCards(base)
  }
  return base
})

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

const isThresholdAlgorithm = computed(() =>
  THRESHOLD_ALGORITHMS.has(job.value?.algorithm || '')
)

// 后端 GeeAuthRequired 异常的固定前缀（见 backend/app/services/gee_auth.py）。
// 命中即说明后端提示用户在本机重新认证。
const GEE_AUTH_HINT_PREFIX = '当前 GEE 认证与请求项目'
const geeAuthHint = computed(() => {
  const msg = job.value?.message || ''
  return msg.startsWith(GEE_AUTH_HINT_PREFIX)
})

const copyFeedback = ref(false)
let copyFeedbackTimer = null

// GeeAuthRequired 的提示文案里包含形如 `    earthengine authenticate --force`
// 的命令（带缩进）。把它解析出来便于"复制命令"按钮一键复制。
const GEE_AUTH_COMMAND_LINE = 'earthengine authenticate --force'

async function onCopyGeeAuthCommand() {
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(GEE_AUTH_COMMAND_LINE)
    } else {
      // 兜底：临时 textarea + execCommand，仅在不支持 Clipboard API 的浏览器触发。
      const ta = document.createElement('textarea')
      ta.value = GEE_AUTH_COMMAND_LINE
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    copyFeedback.value = true
    if (copyFeedbackTimer) clearTimeout(copyFeedbackTimer)
    copyFeedbackTimer = setTimeout(() => {
      copyFeedback.value = false
    }, 1800)
  } catch (err) {
    console.error('复制失败', err)
  }
}

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
  if (copyFeedbackTimer) clearTimeout(copyFeedbackTimer)
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

.gee-auth-hint {
  margin-top: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  background: rgba(220, 38, 38, 0.06);
  border: 1px solid rgba(220, 38, 38, 0.3);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 通用失败错误块：与 gee-auth-hint 视觉一致，但只展示错误文案本身，
   不提供复制命令。用于 GEE 任务原始 error_message（GEE 状态码、CSV
   字段缺失、Asset 不可访问等）。 */
.gee-error-block {
  margin-top: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  background: rgba(220, 38, 38, 0.06);
  border: 1px solid rgba(220, 38, 38, 0.3);
}
.gee-error-block__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-danger);
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.gee-error-block__msg {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-danger);
}
.gee-auth-hint__msg {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-danger);
}
.gee-auth-hint__copy-feedback {
  margin-left: 8px;
  font-size: 12px;
  color: var(--color-muted);
}
</style>