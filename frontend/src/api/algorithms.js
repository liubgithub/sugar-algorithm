import http from './index'

export function fetchAlgorithms() {
  return http.get('/algorithms').then((r) => r.data.algorithms || [])
}

export function fetchAlgorithm(id) {
  return http.get(`/algorithms/${id}`).then((r) => r.data)
}