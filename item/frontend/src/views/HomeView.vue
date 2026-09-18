<template>
  <div>
    <div class="card">
      <h2 class="card__title">平台概览</h2>
      <p>
        甘蔗遥感算法 Web 平台 MVP（第一阶段）。后端 FastAPI、前端 Vue 3 +
        Vite，使用本地 SQLite + 文件目录存储。
      </p>
      <p>
        当前阶段已实现：算法注册表、任务创建/查询、Demo 算法端到端验证。
      </p>
    </div>

    <div class="card">
      <h2 class="card__title">快速开始</h2>
      <ol>
        <li>进入 <router-link to="/algorithms">算法</router-link> 页面查看已注册算法。</li>
        <li>选择 <strong>Demo 算法</strong>，填写参数后提交运行。</li>
        <li>进入 <router-link to="/jobs">任务</router-link> 查看任务状态与结果。</li>
      </ol>
    </div>
  </div>
</template>

<script setup>
</script>