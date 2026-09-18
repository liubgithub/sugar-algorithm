<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getAlgorithms, getDataFiles, runAlgorithm } from './api'
import AlgorithmSelector from './components/AlgorithmSelector.vue'
import ParamForm from './components/ParamForm.vue'
import ResultPanel from './components/ResultPanel.vue'

const algorithms = ref([])
const dataFiles = ref([])
const selectedAlgoId = ref(null)
const running = ref(false)
const result = ref(null)

const selectedAlgo = computed(
  () => algorithms.value.find((a) => a.id === selectedAlgoId.value) || null,
)

onMounted(async () => {
  try {
    const [algos, files] = await Promise.all([getAlgorithms(), getDataFiles()])
    algorithms.value = algos
    dataFiles.value = files
  } catch (e) {
    ElMessage.error(e.message)
  }
})

// 参数表单里上传了新文件后，刷新服务器文件列表
async function refreshDataFiles() {
  try {
    dataFiles.value = await getDataFiles()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function handleRun(inputs) {
  running.value = true
  try {
    result.value = await runAlgorithm(selectedAlgoId.value, inputs)
    ElMessage.success('运行成功')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    running.value = false
  }
}

function handleReset() {
  result.value = null
}
</script>

<template>
  <el-container class="layout">
    <el-header class="header">
      <h1>🌾 算法运行平台</h1>
      <p>选择算法 → 上传/选择参数文件 → 运行 → 查看与下载结果</p>
    </el-header>

    <el-main>
      <template v-if="!result">
        <el-card class="card" shadow="never">
          <AlgorithmSelector
            v-model="selectedAlgoId"
            :algorithms="algorithms"
          />
        </el-card>

        <el-card v-if="selectedAlgo" class="card" shadow="never">
          <template #header>
            <span class="card-title">参数配置</span>
          </template>
          <ParamForm
            :key="selectedAlgo.id"
            :algo="selectedAlgo"
            :data-files="dataFiles"
            :loading="running"
            @submit="handleRun"
            @uploaded="refreshDataFiles"
          />
        </el-card>
      </template>

      <ResultPanel v-else :result="result" @reset="handleReset" />
    </el-main>
  </el-container>
</template>

<style>
body {
  margin: 0;
  background: #f5f7fa;
  font-family: 'Helvetica Neue', Helvetica, 'PingFang SC', 'Microsoft YaHei',
    Arial, sans-serif;
}
.header {
  background: linear-gradient(120deg, #2f6b3c, #4a9d5f);
  color: #fff;
  display: flex;
  align-items: baseline;
  gap: 16px;
  height: 72px;
  padding: 0 28px;
}
.header h1 {
  font-size: 22px;
  margin: 0;
}
.header p {
  font-size: 13px;
  opacity: 0.85;
  margin: 0;
}
.card {
  max-width: 860px;
  margin: 0 auto 20px;
}
.card-title {
  font-weight: 600;
}
</style>
