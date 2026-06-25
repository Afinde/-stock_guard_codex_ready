import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', meta: { public: true }, component: () => import('./views/LoginView.vue') },
    { path: '/', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
    { path: '/recommendations', name: 'recommendations', component: () => import('./views/RecommendationsView.vue') },
    { path: '/signals', name: 'signals', component: () => import('./views/SignalsView.vue') },
    { path: '/market', name: 'market', component: () => import('./views/MarketView.vue') },
    { path: '/stocks/:symbol?', name: 'stocks', component: () => import('./views/StockView.vue') },
    { path: '/backtests/:id?', name: 'backtests', component: () => import('./views/BacktestsView.vue') },
    { path: '/paper', name: 'paper', component: () => import('./views/PaperView.vue') },
    { path: '/system', name: 'system', component: () => import('./views/SystemView.vue') },
    { path: '/admin/users', name: 'admin-users', meta: { admin: true }, component: () => import('./views/AdminUsersView.vue') }
  ]
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.loaded) {
    try {
      await auth.loadMe()
    } catch {
      auth.user = null
    }
  }
  if (!to.meta.public && !auth.user) return { path: '/login', query: { redirect: to.fullPath } }
  if (to.meta.admin && !auth.isAdmin) return '/'
  if (to.path === '/login' && auth.user) return '/'
  return true
})

export default router
