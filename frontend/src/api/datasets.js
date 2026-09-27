import http from './index'

// Datasets API: browse and delete previously uploaded parameter files
// organized by algorithm id and parameter slot.

export function fetchDatasets() {
  return http.get('/datasets').then((r) => r.data)
}

export function fetchAlgorithmDatasets(algorithmId) {
  return http.get(`/datasets/${encodeURIComponent(algorithmId)}`).then((r) => r.data)
}

export function deleteDatasetFile(algorithmId, slot, fileId) {
  return http
    .delete(`/datasets/${encodeURIComponent(algorithmId)}/${encodeURIComponent(slot)}/${encodeURIComponent(fileId)}`)
    .then((r) => r.data)
}