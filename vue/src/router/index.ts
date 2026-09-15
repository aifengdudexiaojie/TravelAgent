import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/LoginView.vue'),
    meta: { requiresGuest: true },
  },
  {
    path: '/',
    component: () => import('@/views/LayoutView.vue'),
    meta: { requiresAuth: true },
    children: [
      { path: '', redirect: '/chat' },
      {
        path: 'chat',
        name: 'Chat',
        component: () => import('@/views/ChatView.vue'),
        meta: { title: '聊天', icon: '💬' },
      },
      {
        path: 'guide',
        name: 'Guide',
        component: () => import('@/views/GuideView.vue'),
        meta: { title: '旅游攻略', icon: '🗺️' },
      },
      {
        path: 'share',
        name: 'Share',
        component: () => import('@/views/ShareView.vue'),
        meta: { title: '旅游分享', icon: '🌍' },
      },
      {
        path: 'profile',
        name: 'Profile',
        component: () => import('@/views/ProfileView.vue'),
        meta: { title: '用户中心', icon: '👤' },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('token')

  if (to.meta.requiresAuth && !token) {
    next('/login')
  } else if (to.meta.requiresGuest && token) {
    next('/')
  } else {
    next()
  }
})

export default router
