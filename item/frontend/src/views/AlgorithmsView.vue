<template>
  <div class="card">
    <h2 class="card__title">算法列表</h2>
    <p v-if="store.loading">加载中…</p>
    <p v-else-if="store.error" style="color: var(--color-danger);">
      {{ store.error }}
    </p>
    <p v-else-if="!store.list.length" class="list__empty">暂无算法。</p>
    <table v-else>
      <thead>
        <tr>
          <th>ID</th>
          <th>名称</th>
          <th>类型</th>
          <th>说明</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="algo in store.list" :key="algo.id">
          <td><code>{{ algo.id }}</code></td>
          <td>{{ algo.name }}</td>
          <td>
            <span
              class="badge"
              :class="algo.type === 'LOCAL' ? 'badge--local' : 'badge--gee'"
            >
              {{ algo.type }}
            </span>
          </td>
          <td>{{ algo.description }}</td>
          <td>
            <router-link :to="`/algorithms/${algo.id}`">查看 / 运行</router-link>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { onMounted } from 'vue'
import { useAlgorithmsStore } from '../stores/algorithms'

const store = useAlgorithmsStore()

onMounted(() => {
  store.loadList()
})
</script>