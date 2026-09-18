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