// Unified Axios wrapper for the backend API.
import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 30000
})

http.interceptors.response.use(
  (resp) => resp,
  (err) => {
    const message =
      err?.response?.data?.detail ||
      err?.message ||
      '请求失败'
    err.userMessage = message
    return Promise.reject(err)
  }
)

export default http