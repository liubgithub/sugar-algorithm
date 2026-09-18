<template>
  <div class="layout">
    <nav class="layout__nav">
      <span class="layout__title">甘蔗遥感算法平台</span>
      <router-link to="/">首页</router-link>
      <router-link to="/algorithms">算法</router-link>
      <router-link to="/datasets">我的数据</router-link>
      <router-link to="/jobs">任务</router-link>
    </nav>
    <main class="layout__main">
      <router-view />
    </main>
  </div>
</template>

<script setup>
</script>