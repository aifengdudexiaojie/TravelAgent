<script setup lang="ts">
/**
 * 旅游攻略生成页
 *
 * 状态与 SSE 订阅全部由 stores/plan.ts 持有，本组件只负责渲染：
 *  · 切到别的 Tab 再回来 → 进度仍在继续（组件销毁不影响 store 里的订阅）；
 *  · 刷新页面 → onMounted 里的 resume() 重新挂到后端同一 task_id 上追平进度。
 *
 * 前置条件：必须已登录自己的小红书账号（否则拿不到真实的小红书攻略数据），
 * 顶部「小红书」状态条负责登录与状态展示，未登录时禁止开始规划。
 */
import { computed, onMounted, ref } from 'vue'
import { apiErrorMessage, guideApi } from '@/lib/api'
import { usePlanStore } from '@/stores/plan'
import PlannerForm from '@/components/PlannerForm.vue'
import IntentConfirm from '@/components/IntentConfirm.vue'
import AnalyzeProgress from '@/components/AnalyzeProgress.vue'
import FinalSummary from '@/components/FinalSummary.vue'
import XhsLoginBar from '@/components/XhsLoginBar.vue'

const plan = usePlanStore()
const savingGuide = ref(false)
const saveError = ref('')
const xhsLoggedIn = ref(false)

/** 未登录小红书时禁止开始规划 */
const blockedReason = computed(() => {
  if (xhsLoggedIn.value) return ''
  return '请先点击右上角「登录小红书」扫码登录你自己的账号，然后才能生成攻略'
})

onMounted(() => {
  // 刷新/切页回来：如果后端任务还在跑，重新挂上并追平进度
  void plan.resume()
})

async function saveAsGuide() {
  if (!plan.finalSummary) return
  savingGuide.value = true
  saveError.value = ''
  try {
    const destination = plan.intent?.locations?.[0] || '未知'
    const days = plan.intent?.days ? parseInt(plan.intent.days) : undefined
    const resp = await guideApi.save({
      title: `${destination}旅游攻略`,
      content: plan.finalSummary,
      destination,
      days,
      summary: (plan.finalSummary as any)?.summary || `${destination}旅行攻略`,
    })
    plan.markSaved({ guide_id: resp.data?.guide_id, title: `${destination}旅游攻略` })
  } catch (err: any) {
    saveError.value = '保存失败：' + apiErrorMessage(err, '请稍后重试')
  } finally {
    savingGuide.value = false
  }
}

function resetAll() {
  saveError.value = ''
  plan.resetAll()
}
</script>

<template>
  <div class="space-y-4">
    <!-- 顶部：标题 + 小红书登录状态 + 实时进度 -->
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h2 class="text-lg font-bold text-gray-800">🗺️ 生成旅游攻略</h2>
      <div class="flex flex-wrap items-center gap-2">
        <XhsLoginBar @update:logged-in="xhsLoggedIn = $event" />
        <div
          v-if="plan.progressText"
          class="flex items-center gap-2 text-xs px-3 py-1 rounded-full border"
          :class="plan.stage === 'done'
            ? 'bg-green-50 border-green-200 text-green-700'
            : 'bg-blue-50 border-blue-200 text-blue-700'"
        >
          <span
            v-if="plan.busy"
            class="inline-block w-3 h-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"
          ></span>
          {{ plan.progressText }}
        </div>
      </div>
    </div>

    <!-- ① 输入阶段 -->
    <div v-if="plan.stage === 'input'" class="bg-white/80 backdrop-blur-sm rounded-xl shadow-lg p-6">
      <p class="text-sm text-gray-500 mb-4">
        输入你的旅行需求，例如："去成都玩3天，预算3000，节奏别太赶"
        <span class="text-gray-400">（攻略内容来自小红书真实笔记，需要先登录你自己的账号）</span>
      </p>
      <PlannerForm
        v-model="plan.userQuery"
        :is-planning="plan.isRecognizing"
        :blocked-reason="blockedReason"
        @submit="(text: string) => plan.recognize(text)"
      />
    </div>

    <!-- ② 意图识别中（loading 动画） -->
    <div
      v-else-if="plan.stage === 'recognizing'"
      class="bg-white/80 backdrop-blur-sm rounded-xl shadow-lg p-10 flex flex-col items-center"
    >
      <div class="relative w-16 h-16 mb-5">
        <span class="absolute inset-0 rounded-full border-4 border-blue-100"></span>
        <span class="absolute inset-0 rounded-full border-4 border-blue-600 border-t-transparent animate-spin"></span>
        <span class="absolute inset-0 flex items-center justify-center text-2xl">🧭</span>
      </div>
      <p class="text-base font-medium text-gray-800">正在识别你的旅行意图…</p>
      <p class="mt-1 text-xs text-gray-500 max-w-md text-center truncate">{{ plan.userQuery }}</p>
      <div class="flex gap-1 mt-4">
        <span class="w-2 h-2 bg-blue-400 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
        <span class="w-2 h-2 bg-blue-400 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
        <span class="w-2 h-2 bg-blue-400 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
      </div>
    </div>

    <!-- ③ 意图确认 / 改完重新识别 -->
    <IntentConfirm
      v-else-if="plan.stage === 'intent_confirm' && plan.intent"
      v-model:query="plan.userQuery"
      :intent="plan.intent"
      :is-recognizing="plan.isRecognizing"
      @confirm="plan.analyze()"
      @redo="plan.recognize()"
    />

    <!-- ④ 分析中 -->
    <div v-else-if="plan.stage === 'analyzing'" class="bg-white/80 backdrop-blur-sm rounded-xl shadow-lg p-6">
      <div class="flex items-center justify-between mb-1">
        <h2 class="text-lg font-bold text-gray-800 flex items-center gap-2">
          <span v-if="plan.isAnalyzing" class="inline-block w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></span>
          ⏳ 正在分析…
        </h2>
        <button
          @click="plan.cancelAnalyze()"
          class="text-xs px-3 py-1.5 border border-gray-300 text-gray-600 rounded-lg hover:bg-gray-50 transition"
        >
          停止分析
        </button>
      </div>
      <p class="text-xs text-blue-600 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2 mb-4">
        💡 可以切到「聊天 / 分享 / 我的」页面，分析会继续在后台跑，切回来进度不会丢。
      </p>
      <AnalyzeProgress :events="plan.events" :is-analyzing="plan.isAnalyzing" />
    </div>

    <!-- ⑤ 结果 -->
    <template v-else-if="plan.stage === 'done' && plan.finalSummary">
      <div class="bg-white/80 backdrop-blur-sm rounded-xl shadow-lg p-6">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-lg font-bold text-gray-800">✅ 攻略生成完成</h2>
          <div class="flex gap-2">
            <button
              @click="saveAsGuide"
              :disabled="savingGuide || !!plan.savedGuide"
              class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm transition disabled:opacity-50"
            >
              {{ plan.savedGuide ? '✓ 已保存' : (savingGuide ? '保存中…' : '💾 保存攻略') }}
            </button>
            <button @click="resetAll" class="px-4 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg text-sm transition">
              新建攻略
            </button>
          </div>
        </div>

        <!-- RAG 入库状态（后端自动把总结写入知识库） -->
        <p
          v-if="plan.saveState === 'saving'"
          class="text-xs text-blue-700 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2 flex items-center gap-2"
        >
          <span class="inline-block w-3 h-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></span>
          正在写入知识库（PG / ES / Qdrant）…
        </p>
        <p
          v-else-if="plan.saveState === 'saved'"
          class="text-xs text-green-700 bg-green-50 border border-green-100 rounded-lg px-3 py-2"
        >
          📚 已自动写入知识库
          <template v-if="plan.saveReport?.ingested?.length">
            （攻略 id：{{ plan.saveReport.ingested[0]?.guide_id }}，分块 {{ plan.saveReport.ingested[0]?.chunks }} 个）
          </template>
        </p>
        <p
          v-else-if="plan.saveState === 'failed'"
          class="text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2"
        >
          ⚠️ 未写入知识库（{{ plan.validationError?.kind === 'structure' ? '结构校验未通过' : (plan.validationError?.message || '入库失败') }}），但攻略本身可正常查看和保存
        </p>
      </div>

      <FinalSummary :summary="plan.finalSummary" @restart="resetAll" />
    </template>

    <!-- 提示 / 错误 -->
    <div v-if="plan.notice" class="p-3 bg-blue-50 border border-blue-200 rounded-lg text-blue-700 text-sm">
      {{ plan.notice }}
    </div>
    <div v-if="plan.error" class="p-4 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
      {{ plan.error }}
    </div>
    <div v-if="saveError" class="p-4 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
      {{ saveError }}
    </div>
  </div>
</template>
