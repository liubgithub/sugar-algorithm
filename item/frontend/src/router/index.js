import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'home',
    component: () => import('../views/HomeView.vue')
  },
  {
    path: '/algorithms',
    name: 'algorithms',
    component: () => import('../views/AlgorithmsView.vue')
  },
  {
    path: '/algorithms/:id',
    name: 'algorithm-detail',
    component: () => import('../views/AlgorithmDetailView.vue'),
    props: true
  },
  {
    path: '/datasets',
    name: 'datasets',
    component: () => import('../views/DatasetsView.vue')
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: () => import('../views/JobsView.vue')
  },
  {
    path: '/jobs/:jobId',
    name: 'job-detail',
    component: () => import('../views/JobDetailView.vue'),
    props: true
  }
]

const router = createRouter({
  // Hash 模式：路由放在 # 后面，跳转不依赖服务器对路径的回退支持，
  // 无论是 vite 开发服务器、后端静态托管还是直接打开 dist 都能正常导航。
  history: createWebHashHistory(),
  routes
})

export default router