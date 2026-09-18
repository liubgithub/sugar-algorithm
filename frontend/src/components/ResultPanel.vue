<script setup>
const props = defineProps({
  result: { type: Object, required: true },
})
const emit = defineEmits(['reset'])

function fmt(value, decimals = 3) {
  if (value === null || value === undefined) return '—'
  return Number(value).toFixed(decimals)
}

function tableColumns(table) {
  return table.columns.map((col, i) => ({ prop: `c${i}`, label: col, minWidth: 120 }))
}

function tableRows(table) {
  return table.rows.map((row) => {
    const obj = {}
    table.columns.forEach((_, i) => {
      obj[`c${i}`] = row[i]
    })
    return obj
  })
}

function encode(url) {
  return encodeURI(url)
}

function download(url) {
  const a = document.createElement('a')
  a.href = encodeURI(url)
  a.download = ''
  document.body.appendChild(a)
  a.click()
  a.remove()
}

function preview(url) {
  window.open(encode(url), '_blank')
}
</script>

<template>
  <div>
    <el-alert type="success" :closable="false" show-icon class="result-banner">
      <template #title>「{{ result.algorithm_name }}」{{ result.message }}</template>
      任务编号：{{ result.run_id }}
    </el-alert>

    <!-- 1. float 指标 -->
    <el-card class="card" shadow="never">
      <template #header><span class="card-title">📊 预测指标</span></template>
      <el-row :gutter="16">
        <el-col v-for="m in result.metrics" :key="m.name" :span="6">
          <div class="metric-card">
            <div class="metric-value">{{ fmt(m.value, m.decimals) }}</div>
            <div class="metric-label">{{ m.name }}<span v-if="m.unit">（{{ m.unit }}）</span></div>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <!-- 2. csv 表格 -->
    <el-card v-for="t in result.tables" :key="t.name" class="card" shadow="never">
      <template #header>
        <span class="card-title">📋 {{ t.name }}</span>
        <el-button
          v-if="t.csv_url"
          link
          type="primary"
          class="download-btn"
          @click="download(t.csv_url)"
        >
          下载 CSV
        </el-button>
      </template>
      <el-table
        :data="tableRows(t)"
        border
        stripe
        size="small"
        max-height="420"
      >
        <el-table-column
          v-for="col in tableColumns(t)"
          :key="col.prop"
          v-bind="col"
          show-overflow-tooltip
        />
      </el-table>
    </el-card>

    <!-- 3. tif 图片 -->
    <el-card v-for="r in result.rasters" :key="r.name" class="card" shadow="never">
      <template #header>
        <span class="card-title">🖼️ {{ r.name }}</span>
        <span class="download-btn">
          <el-button link type="primary" @click="preview(r.png_url)">
            预览原图
          </el-button>
          <el-button link type="primary" @click="download(r.tif_url)">
            下载 TIF
          </el-button>
        </span>
      </template>
      <el-image
        :src="encode(r.png_url)"
        :preview-src-list="[encode(r.png_url)]"
        fit="contain"
        class="raster-img"
        lazy
      >
        <template #error>
          <div class="img-error">图片加载失败</div>
        </template>
      </el-image>
    </el-card>

    <div class="reset-row">
      <el-button type="primary" size="large" plain @click="emit('reset')">
        🔄 再运行一次
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.result-banner {
  max-width: 860px;
  margin: 0 auto 20px;
}
.card {
  max-width: 860px;
  margin: 0 auto 20px;
}
.card-title {
  font-weight: 600;
}
.download-btn {
  float: right;
}
.metric-card {
  background: #f5f7fa;
  border-radius: 8px;
  padding: 16px 12px;
  text-align: center;
  margin-bottom: 12px;
}
.metric-value {
  font-size: 26px;
  font-weight: 700;
  color: #2f6b3c;
}
.metric-label {
  font-size: 12px;
  color: #606266;
  margin-top: 6px;
}
.raster-img {
  width: 100%;
  border-radius: 6px;
}
.img-error {
  height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #909399;
  background: #f5f7fa;
}
.reset-row {
  text-align: center;
  padding-bottom: 30px;
}
</style>
