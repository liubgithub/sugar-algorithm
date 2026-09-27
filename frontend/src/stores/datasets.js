import { defineStore } from 'pinia'
import {
  fetchDatasets,
  fetchAlgorithmDatasets,
  deleteDatasetFile
} from '../api/datasets'

// Pinia store that caches the dataset tree, so multiple pages (AlgorithmDetail
// dropdown + DatasetsView) can share the same in-memory list and so that
// deletes immediately invalidate the cache.
export const useDatasetsStore = defineStore('datasets', {
  state: () => ({
    summary: { algorithms: [] },
    byAlgorithm: {},      // algoId -> { slot -> { groups: [{ group_value, files }] } }
    loading: false,
    error: null
  }),
  actions: {
    async loadAll() {
      this.loading = true
      this.error = null
      try {
        this.summary = await fetchDatasets()
      } catch (err) {
        this.error = err.userMessage || err.message
      } finally {
        this.loading = false
      }
    },
    async loadAlgo(algorithmId) {
      try {
        const data = await fetchAlgorithmDatasets(algorithmId)
        this.byAlgorithm[algorithmId] = data
        return data
      } catch (err) {
        this.error = err.userMessage || err.message
        throw err
      }
    },
    async remove(algorithmId, slot, fileId) {
      await deleteDatasetFile(algorithmId, slot, fileId)
      // Refetch affected views.
      if (this.byAlgorithm[algorithmId]) {
        await this.loadAlgo(algorithmId)
      }
      await this.loadAll()
    },
    // Convenience selector: flat list of selectable entries for a given slot,
    // each with the absolute path the algorithm expects.
    flatSelections(algorithmId, slot) {
      const algo = this.byAlgorithm[algorithmId]
      if (!algo) return []
      const slotEntry = (algo.slots || []).find((s) => s.slot === slot)
      if (!slotEntry) return []
      const out = []
      for (const group of slotEntry.groups || []) {
        for (const file of group.files || []) {
          out.push({
            file_id: file.file_id,
            name: file.name,
            kind: file.kind,
            path: file.path,
            size_bytes: file.size_bytes,
            group_value: group.group_value || null,
            label: `${group.group_value ? group.group_value + ' / ' : ''}${file.name}`
          })
        }
      }
      return out
    }
  }
})