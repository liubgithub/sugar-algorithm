import http from './index'

export function createJob(algorithm, params, name) {
  return http.post('/jobs', { algorithm, params, name }).then((r) => r.data)
}

export function fetchJob(jobId) {
  return http.get(`/jobs/${jobId}`).then((r) => r.data)
}

export function fetchJobs() {
  return http.get('/jobs').then((r) => r.data.jobs || [])
}

// Tail the algorithm's captured stdout; offset is the byte position we
// have already received so only the new text comes back each poll.
export function fetchJobLog(jobId, offset = 0) {
  return http.get(`/jobs/${jobId}/log`, { params: { offset } }).then((r) => r.data)
}

// Edit a job's mutable fields (currently only `note`).
export function patchJob(jobId, body) {
  return http.patch(`/jobs/${jobId}`, body).then((r) => r.data)
}

// Delete a job and its working directory. Allowed even while running.
export function deleteJob(jobId) {
  return http.delete(`/jobs/${jobId}`).then((r) => r.data)
}

// Cancel a queued job. Throws when the job is missing or in a non-queued state.
export function cancelJob(jobId) {
  return http.post(`/jobs/${jobId}/cancel`).then((r) => r.data)
}

// Re-pull the result of a failed / cancelled job.
//
// Typical case: the GEE task itself succeeded but the *finalize* step
// (wait for asset + download the GeoTIFF) died — usually a flaky proxy.
// The backend re-dispatches the finalize hook; already-downloaded tiles
// are resumed rather than re-fetched.
export function retryJob(jobId) {
  return http.post(`/jobs/${jobId}/retry`).then((r) => r.data)
}