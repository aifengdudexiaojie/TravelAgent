<script setup lang="ts">
/**
 * 意图确认卡片
 *
 * 关键点（对应"意图识别后重新输入，并没有重新识别"）：
 *  · 这里直接提供**可编辑的输入框**，改完点「🔄 重新识别」会用新内容重新识别；
 *  · 重新识别期间按钮进入 loading 状态（转圈），并禁用，避免重复提交；
 *  · 重新识别会清空旧意图（在 store.recognize 里做），不会再显示上一次的结果。
 */
import type { IntentContent } from '@/types'

withDefaults(
  defineProps<{
    intent: IntentContent
    query: string
    isRecognizing?: boolean
  }>(),
  { isRecognizing: false },
)

const emit = defineEmits<{
  confirm: []
  redo: []
  'update:query': [value: string]
}>()
</script>

<template>
  <div class="p-4 bg-gradient-to-r from-indigo-50 to-blue-50 border border-indigo-100 rounded-xl shadow-sm">
    <div class="flex items-center gap-2 mb-3">
      <span class="text-lg">🎯</span>
      <h3 class="font-semibold text-gray-800">我已识别您的旅行意图</h3>
    </div>

    <!-- 意图信息 -->
    <div class="flex flex-wrap gap-2 mb-4">
      <span class="bg-white px-3 py-1 rounded-full text-sm shadow-sm border border-indigo-100">
        📍 {{ intent.locations?.join('、') || '未知' }}
      </span>
      <span class="bg-white px-3 py-1 rounded-full text-sm shadow-sm border border-indigo-100">
        ⏱️ {{ intent.days || '天数未知' }}
      </span>
      <span class="bg-white px-3 py-1 rounded-full text-sm shadow-sm border border-indigo-100">
        📅 {{ intent.start_date || '灵活' }} ~ {{ intent.end_date || '灵活' }}
      </span>
      <span v-if="intent.people_count" class="bg-white px-3 py-1 rounded-full text-sm shadow-sm border border-indigo-100">
        👥 {{ intent.people_count }} 人
      </span>
      <span class="bg-white px-3 py-1 rounded-full text-sm shadow-sm border border-indigo-100">
        🏖️ {{ intent.pace }}
      </span>
      <span class="bg-white px-3 py-1 rounded-full text-sm shadow-sm border border-indigo-100">
        💰 {{ intent.budget_level }}
        <template v-if="intent.budget_amount">（{{ intent.budget_amount }}元{{ intent.budget_amount_per_person ? '/人' : '' }}）</template>
      </span>
    </div>

    <p v-if="intent.others" class="mb-4 text-xs text-gray-500 bg-white/60 rounded-lg p-2">
      📝 备注：{{ intent.others }}
    </p>

    <!-- 需求可编辑：改完直接重新识别 -->
    <label class="block text-xs text-gray-500 mb-1">需求有出入？直接改，然后重新识别：</label>
    <textarea
      :value="query"
      @input="emit('update:query', ($event.target as HTMLTextAreaElement).value)"
      :disabled="isRecognizing"
      rows="2"
      class="w-full mb-3 px-3 py-2 text-sm bg-white border border-indigo-100 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-300 resize-none disabled:opacity-60"
      placeholder="例如：去成都玩4天，2个人，预算5000，节奏轻松一点"
    ></textarea>

    <!-- 操作按钮 -->
    <div class="flex gap-3">
      <button
        @click="emit('confirm')"
        :disabled="isRecognizing"
        class="flex-1 py-2.5 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
      >
        ✅ 确认，开始分析
      </button>
      <button
        @click="emit('redo')"
        :disabled="isRecognizing || !query.trim()"
        class="flex-1 py-2.5 border border-gray-300 text-gray-700 text-sm font-medium rounded-lg hover:bg-gray-50 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
      >
        <span
          v-if="isRecognizing"
          class="inline-block w-4 h-4 border-2 border-gray-500 border-t-transparent rounded-full animate-spin"
        ></span>
        <span>{{ isRecognizing ? '重新识别中…' : '🔄 重新识别' }}</span>
      </button>
    </div>
  </div>
</template>
