// Helpers for downloading job result files via the proxied /api base URL.
import http from './index'

export function listJobFiles(jobId) {
  return http.get(`/jobs/${jobId}/files`).then((r) => r.data.files || [])
}

// Build a direct download URL the browser can navigate to.
export function jobFileUrl(jobId, filename) {
  return `/api/jobs/${jobId}/files/${encodeURIComponent(filename)}`
}

// Result preview endpoints (GeoTIFF -> PNG + info + pixel value).
const resultBase = (jobId, filename) =>
  `/api/jobs/${jobId}/result/files/${encodeURIComponent(filename)}`

export function jobPreviewUrl(jobId, filename) {
  return `${resultBase(jobId, filename)}/preview.png`
}

export function jobInfoUrl(jobId, filename) {
  return `${resultBase(jobId, filename)}/info`
}

export function jobPixelValueUrl(jobId, filename, lon, lat, band = 1) {
  const params = new URLSearchParams({ lon: String(lon), lat: String(lat), band: String(band) })
  return `${resultBase(jobId, filename)}/value?${params.toString()}`
}

export function fetchJobInfo(jobId, filename) {
  return http.get(`/jobs/${jobId}/result/files/${encodeURIComponent(filename)}/info`).then((r) => r.data)
}

export function fetchJobPixelValue(jobId, filename, lon, lat, band = 1) {
  const params = { lon, lat, band }
  return http
    .get(`/jobs/${jobId}/result/files/${encodeURIComponent(filename)}/value`, { params })
    .then((r) => r.data)
}

// ---------------------------------------------------------------------------
// Mosaic (multi-file) — 把同一张图切成若干块的结果拼成一张完整图
// ---------------------------------------------------------------------------
//
// 有些算法的产物是多个 .tif 分片，只有拼起来才是完整结果（例如 GEE 把
// 大范围导出拆成多个分片）。这里走的是展示页（OnlyshowView）上传多张 TIF
// 时同一条后端拼接管线：preview_service 的 mosaic 分支。
const mosaicBase = (jobId) => `/api/jobs/${jobId}/result/mosaic`

export function jobMosaicPreviewUrl(jobId, filenames) {
  const q = new URLSearchParams({ files: filenames.join(',') })
  return `${mosaicBase(jobId)}/preview.png?${q.toString()}`
}

export function jobMosaicInfoUrl(jobId, filenames) {
  const q = new URLSearchParams({ files: filenames.join(',') })
  return `${mosaicBase(jobId)}/info?${q.toString()}`
}

export function fetchJobMosaicInfo(jobId, filenames) {
  return http
    .get(`/jobs/${jobId}/result/mosaic/info`, { params: { files: filenames.join(',') } })
    .then((r) => r.data)
}