import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 0, // 取消客户端超时：大研究区同步计算/下载可能持续数小时，由后端决定成败
})

// 统一错误处理：把 FastAPI 的 detail（字符串或 422 数组）转成可读中文消息
http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const detail = error.response?.data?.detail
    let message = '请求失败，请确认后端服务已启动'
    if (typeof detail === 'string') {
      message = detail
    } else if (Array.isArray(detail)) {
      message = detail.map((d) => d.msg).join('；')
    } else if (error.message) {
      message = error.message
    }
    return Promise.reject(new Error(message))
  },
)

export const getAlgorithms = () => http.get('/algorithms')
export const getDataFiles = () => http.get('/data')
export const runAlgorithm = (algorithmId, inputs) =>
  http.post('/run', { algorithm_id: algorithmId, inputs })

// 上传文件到后端数据文件夹，folder 为可选子目录（如 月度影像_2026）
export const uploadFile = (file, folder = '') => {
  const formData = new FormData()
  formData.append('file', file)
  if (folder) formData.append('folder', folder)
  return http.post('/upload', formData)
}
