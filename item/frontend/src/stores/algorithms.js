import { defineStore } from 'pinia'
import { fetchAlgorithms, fetchAlgorithm } from '../api/algorithms'

export const useAlgorithmsStore = defineStore('algorithms', {
  state: () => ({
    list: [],
    loading: false,
    error: null
  }),
  actions: {
    async loadList() {
      this.loading = true
      this.error = null
      try {
        this.list = await fetchAlgorithms()
      } catch (err) {
        this.error = err.userMessage || err.message
      } finally {
        this.loading = false
      }
    },
    async loadOne(id) {
      return fetchAlgorithm(id)
    }
  }
})