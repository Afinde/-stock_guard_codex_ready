import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
    { path: '/signals', name: 'signals', component: () => import('./views/SignalsView.vue') },
    { path: '/stocks/:symbol?', name: 'stocks', component: () => import('./views/StockView.vue') },
    { path: '/backtests/:id?', name: 'backtests', component: () => import('./views/BacktestsView.vue') },
    { path: '/paper', name: 'paper', component: () => import('./views/PaperView.vue') },
    { path: '/system', name: 'system', component: () => import('./views/SystemView.vue') }
  ]
})
