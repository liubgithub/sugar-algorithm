<script setup>
import { computed } from 'vue'

const props = defineProps({
  algorithms: { type: Array, default: () => [] },
  modelValue: { type: String, default: null },
})
const emit = defineEmits(['update:modelValue'])

const selectedAlgo = computed(
  () => props.algorithms.find((a) => a.id === props.modelValue) || null,
)

function onChange(value) {
  emit('update:modelValue', value)
}

const TAG_TYPES = { csv: 'success', tif: 'warning', xlsx: 'danger', month: 'info' }
function tagType(type) {
  return TAG_TYPES[type] || 'info'
}
</script>

<template>
  <div>
    <div class="selector-row">
      <span class="label">选择算法</span>
      <el-select
        :model-value="modelValue"
        placeholder="请选择要运行的算法"
        style="width: 320px"
        @change="onChange"
      >
        <el-option
          v-for="algo in algorithms"
          :key="algo.id"
          :label="algo.name"
          :value="algo.id"
        />
      </el-select>
    </div>

    <el-alert
      v-if="selectedAlgo"
      type="info"
      :closable="false"
      show-icon
      class="desc"
    >
      <template #title>{{ selectedAlgo.description }}</template>
      需要 {{ selectedAlgo.params.length }} 个参数：
      <el-tag
        v-for="p in selectedAlgo.params"
        :key="p.name"
        size="small"
        class="param-tag"
        :type="tagType(p.type)"
      >
        {{ p.label }}（{{ p.type === 'month' ? '月份' : p.type + ' 文件' }}{{ p.required ? '' : '，可选' }}）
      </el-tag>
    </el-alert>
  </div>
</template>

<style scoped>
.selector-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.label {
  font-weight: 600;
  flex-shrink: 0;
}
.desc {
  margin-top: 14px;
}
.param-tag {
  margin: 2px 4px 2px 0;
}
</style>
