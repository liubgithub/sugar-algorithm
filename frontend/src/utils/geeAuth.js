// GEE 认证不匹配的"识别 + 文案"单一事实来源。
//
// 后端 `GeeAuthRequired`（backend/app/services/gee_auth.py）的 str() 以固定
// 前缀开头，凡是以它开头的文案都意味着"用户需要在本机重新认证 GEE"，前端
// 据此把纯红字升级为带"复制命令"按钮的 GeeAuthHint 组件。
//
// 前端刻意不按 GEE 的英文原始报错做匹配 —— 那段文案 earthengine-api 会随
// 版本变动（例如 "Caller does not have required permission to use project"），
// 而这个中文前缀是本仓库自己写的，稳定得多。

export const GEE_AUTH_HINT_PREFIX = '当前 GEE 认证与请求项目'

// 提示文案里以缩进形式出现的命令，单独抽出来给"复制命令"按钮一键复制。
export const GEE_AUTH_COMMAND_LINE = 'earthengine authenticate --force'

export function isGeeAuthMessage(msg) {
  return String(msg || '').startsWith(GEE_AUTH_HINT_PREFIX)
}