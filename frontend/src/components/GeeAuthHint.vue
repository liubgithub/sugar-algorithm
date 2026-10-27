<template>
  <div class="gee-auth-hint">
    <pre class="gee-auth-hint__msg">{{ message }}</pre>
    <div class="gee-auth-hint__actions">
      <button class="button button--ghost" type="button" @click="onCopy">
        复制命令
      </button>
      <span v-if="copyFeedback" class="gee-auth-hint__copy-feedback">已复制</span>
    </div>
  </div>
</template>

<script setup>
// GEE 认证不匹配时的可执行提示块。
//
// 触发源有两种，展示位置不同但文案同源（后端 `GeeAuthRequired` 的 str()，
// 见 backend/app/services/gee_auth.py）：
//
//   1. 任务详情页——后端 run() 捕获 GeeAuthRequired 后把 str() 写进
//      job.message；
//   2. 算法表单页——提交前的 Asset 可达性校验（POST
//      /api/algorithms/{id}/saved-gee-assets/verify）返回
//      `code: "gee_auth_required"`，前端把它塞进 submitError。
//
// 两条路径都指向同一件事：用户需要在本机终端跑
// `earthengine authenticate --force` 重新绑定 GEE 项目，所以这里只负责
// 把文案原样保留换行渲染 + 提供一键复制命令按钮。识别规则见
// ../utils/geeAuth.js。
import { onBeforeUnmount, ref } from 'vue'
import { GEE_AUTH_COMMAND_LINE } from '../utils/geeAuth'

defineProps({
  message: { type: String, required: true },
})

const copyFeedback = ref(false)
let copyFeedbackTimer = null

onBeforeUnmount(() => {
  if (copyFeedbackTimer) clearTimeout(copyFeedbackTimer)
})

async function onCopy() {
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
</script>

<style scoped>
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
.gee-auth-hint__msg {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-danger);
}
.gee-auth-hint__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.gee-auth-hint__copy-feedback {
  font-size: 12px;
  color: var(--color-muted);
}

/* .button 来自全局 style.css；--ghost 变体目前各视图各自 scoped 定义，
   这里随组件带一份，保证在任意宿主视图下都能正确显示。 */
.button--ghost {
  background: transparent;
  color: var(--color-primary);
  border: 1px solid var(--color-border);
  padding: 2px 10px;
  font-size: 12px;
}
.button--ghost:hover { background: rgba(37, 99, 235, 0.06); }
</style>