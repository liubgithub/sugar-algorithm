import { defineStore } from 'pinia'
import { cancelJob, createJob, deleteJob, fetchJob, fetchJobs, patchJob } from '../api/jobs'

export const useJobsStore = defineStore('jobs', {
  state: () => ({
    list: [],
    current: null,
    loading: false,
    error: null
  }),
  actions: {
    async loadList() {
      this.loading = true
      this.error = null
      try {
        this.list = await fetchJobs()
      } catch (err) {
        this.error = err.userMessage || err.message
      } finally {
        this.loading = false
      }
    },
    async loadOne(jobId) {
      this.current = await fetchJob(jobId)
      return this.current
    },
    async submit(algorithm, params, name) {
      return createJob(algorithm, params, name)
    },
    // 更新任务名：调 PATCH，同步刷新 list / current。
    async setName(jobId, name) {
      const updated = await patchJob(jobId, { name })
      const idx = this.list.findIndex((j) => j.job_id === jobId)
      if (idx >= 0) this.list[idx] = { ...this.list[idx], ...updated }
      if (this.current?.job_id === jobId) this.current = { ...this.current, ...updated }
      return updated
    },
    // 更新备注：调 PATCH，同步刷新 list / current 中对应条目。
    async patchNote(jobId, note) {
      const updated = await patchJob(jobId, { note })
      const idx = this.list.findIndex((j) => j.job_id === jobId)
      if (idx >= 0) this.list[idx] = { ...this.list[idx], ...updated }
      if (this.current?.job_id === jobId) this.current = { ...this.current, ...updated }
      return updated
    },
    // 取消任务：调 POST /cancel，同步刷新 list / current。
    async cancelJobAction(jobId) {
      const updated = await cancelJob(jobId)
      const idx = this.list.findIndex((j) => j.job_id === jobId)
      if (idx >= 0) this.list[idx] = { ...this.list[idx], ...updated, status: updated.status }
      if (this.current?.job_id === jobId) this.current = { ...this.current, ...updated, status: updated.status }
      return updated
    },
    // 删除任务：从 list 移除；调用方负责导航回 /jobs（如当前正看该任务）。
    async removeJob(jobId) {
      await deleteJob(jobId)
      this.list = this.list.filter((j) => j.job_id !== jobId)
      if (this.current?.job_id === jobId) this.current = null
    }
  }
})