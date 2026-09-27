<template>
  <div>
    <div class="card">
      <h2 class="card__title">平台概览</h2>
      <p>
        甘蔗遥感算法展示平台： 前端采用 Vue 3 开发，后端采用 FastAPI，使用 SQLite 和本地文件目录进行数据及结果存储。
      </p>
      <p>
        本地与web均可运行算法：
      </p>
        <ul>
        <li>广西26持续估产1</li>
        <li>广西26持续估产2（在1的基础上优化更新）</li>
        <li>广西甘蔗分类--获取阈值</li>
        <li>广西甘蔗分类--特征提取</li>
        
      </ul>
      <p>
        目前展示算法结果：
      </p>
        <ul>
        <li>广西25一次性估产（只展示结果）</li>
        <li>广西甘蔗分类--区域分类（本地运行成功，web暂未运行成功）</li>
      </ul>
       <p>
        目前本地与web均未跑通算法：
      </p>
        <ul>
        <li>巴西持续估产</li>
      </ul>
    </div>

    <div class="card">
      <h2 class="card__title">快速开始</h2>
      <ol>
        <li>进入 <router-link to="/algorithms">算法页面</router-link> 查看实际可运行算法</li>
        <li>进入 <router-link to="/datasets">我的数据页面</router-link> 查看已上传或使用的数据</li>
        <li>进入 <router-link to="/jobs">任务页面</router-link> 查看算法运行的状态与结果</li>
        <li>进入 <router-link to="/onlyshow">展示页面</router-link> 查看目前展示算法结果</li>
      </ol>
    </div>
  </div>
</template>

<script setup>
</script>