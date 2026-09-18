<script setup>
import { reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { uploadFile } from '../api'

const props = defineProps({
  algo: { type: Object, required: true },
  dataFiles: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
})
const emit = defineEmits(['submit', 'uploaded'])

const form = reactive({})
const uploading = reactive({}) // 每个文件参数的上传中状态
const uploadFolders = reactive({}) // 每个文件参数的上传目标子目录

// 切换算法时重置表单（参数声明了 default 时预填默认值）
watch(
  () => props.algo?.id,
  () => {
    Object.keys(form).forEach((key) => delete form[key])
    for (const p of props.algo?.params || []) {
      form[p.name] = p.default ?? ''
    }
  },
  { immediate: true },
)

const FILE_TYPES = ['csv', 'tif', 'xlsx', 'json', 'shp']

function isFileParam(param) {
  return FILE_TYPES.includes(param.type)
}

function optionsFor(type) {
  // shp 边界以 zip 压缩包上传，选择列表同时显示 shp 与 zip 文件
  if (type === 'shp') return props.dataFiles.filter((f) => f.type === 'shp' || f.type === 'zip')
  return props.dataFiles.filter((f) => f.type === type)
}

async function handleUpload({ file, onSuccess, onError }, p) {
  uploading[p.name] = true
  try {
    const res = await uploadFile(file, (uploadFolders[p.name] || '').trim())
    form[p.name] = res.name // 上传成功后直接作为该参数的取值
    ElMessage.success(`已上传：${res.name}（${res.size_mb} MB）`)
    emit('uploaded', res) // 通知父组件刷新文件列表
    onSuccess(res)
  } catch (e) {
    ElMessage.error(e.message)
    onError(e)
  } finally {
    uploading[p.name] = false
  }
}

function handleSubmit() {
  const missing = (props.algo.params || []).find((p) => p.required && !form[p.name])
  if (missing) {
    ElMessage.warning(`请选择参数：${missing.label}`)
    return
  }
  const inputs = {}
  for (const p of props.algo.params) {
    const v = form[p.name]
    if (v === '' || v === null || v === undefined) continue
    // 数字参数转字符串提交，与后端 RunRequest.inputs (dict[str, str]) 对齐
    inputs[p.name] = typeof v === 'number' ? String(v) : v
  }
  emit('submit', inputs)
}
</script>

<template>
  <el-form label-width="200px" @submit.prevent>
    <el-form-item
      v-for="p in algo.params"
      :key="p.name"
      :label="p.label"
      :required="p.required"
    >
      <!-- 月份类参数：月份选择器 -->
      <el-date-picker
        v-if="p.type === 'month'"
        v-model="form[p.name]"
        type="month"
        value-format="YYYY-MM"
        format="YYYY 年 MM 月"
        placeholder="请选择月份"
        style="width: 220px"
      />

      <!-- 数字类参数：数值输入框 -->
      <el-input-number
        v-else-if="p.type === 'number'"
        v-model="form[p.name]"
        :controls="false"
        placeholder="请输入数值"
        style="width: 220px"
      />

      <!-- 文本类参数：单行输入框 -->
      <el-input
        v-else-if="p.type === 'text'"
        v-model="form[p.name]"
        :placeholder="p.required ? '请输入' : '可选，不填则使用默认值'"
        clearable
        style="width: 340px"
      />

      <!-- 文件类参数：从服务器选择 或 从本地上传 -->
      <div v-else class="file-row">
        <el-select
          v-model="form[p.name]"
          :placeholder="p.required ? '请选择文件' : '可选，不选择则跳过'"
          clearable
          filterable
          style="width: 340px"
        >
          <el-option
            v-for="f in optionsFor(p.type)"
            :key="f.name"
            :label="`${f.name}（${f.size_mb} MB）`"
            :value="f.name"
          />
        </el-select>
        <el-upload
          :show-file-list="false"
          :http-request="(opt) => handleUpload(opt, p)"
          :disabled="uploading[p.name]"
        >
          <el-button size="small" type="primary" plain :loading="uploading[p.name]">
            {{ uploading[p.name] ? '上传中…' : '上传文件' }}
          </el-button>
        </el-upload>
        <el-input
          v-model="uploadFolders[p.name]"
          size="small"
          placeholder="子目录（可选，如 月度影像_2026）"
          style="width: 210px"
          clearable
        />
      </div>

      <span v-if="isFileParam(p)" class="type-hint">
        {{ p.type }} 文件{{ p.required ? '' : '（可选）' }}
      </span>
    </el-form-item>

    <el-form-item>
      <el-button
        type="primary"
        size="large"
        :loading="loading"
        @click="handleSubmit"
      >
        {{ loading ? '运行中…（真实算法约需 1-5 分钟）' : '🚀 运行算法' }}
      </el-button>
    </el-form-item>
  </el-form>
</template>

<style scoped>
.file-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.type-hint {
  margin-left: 12px;
  color: #909399;
  font-size: 12px;
}
</style>
