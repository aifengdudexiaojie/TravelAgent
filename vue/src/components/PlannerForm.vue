<script setup lang="ts">
/**
 * 旅行需求输入框
 *  · v-model 绑定内容（内容保存在 plan store 里，切页/刷新不丢）
 *  · 提交后按钮进入 loading 状态（转圈 + 文案），输入框禁用
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    isPlanning: boolean
    modelValue: string
    placeholder?: string
    /** 非空则表示当前不可用（例如还没登录小红书），文案会显示出来 */
    blockedReason?: string
  }>(),
  { placeholder: '输入你的旅行需求，例如：去成都玩3天，预算3000，节奏别太赶', blockedReason: '' },
)

const emit = defineEmits<{
  submit: [input: string]
  'update:modelValue': [value: string]
}>()

const text = computed({
  get: () => props.modelValue,
  set: (value: string) => emit('update:modelValue', value),
})

function handleSubmit() {
  const value = text.value.trim()
  if (!value || props.isPlanning || props.blockedReason) return
  emit('submit', value)
}
</script>

<template>
  <div class="w-full">
    <form @submit.prevent="handleSubmit" class="space-y-2">
      <div class="flex gap-3 items-stretch">
        <div class="flex-1">
          <input
            v-model="text"
            :placeholder="blockedReason || placeholder"
            :disabled="isPlanning || !!blockedReason"
            class="w-full min-h-11 px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-400 disabled:opacity-60 disabled:bg-gray-50"
          />
        </div>

        <button
          type="submit"
          :disabled="isPlanning || !!blockedReason || !text.trim()"
          class="h-11 px-6 text-sm font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
        >
          <span
            v-if="isPlanning"
            class="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin"
          ></span>
          <span>{{ isPlanning ? '识别中…' : '🚀 开始规划' }}</span>
        </button>
      </div>

      <!-- 前置条件未满足（例如未登录小红书） -->
      <div v-if="blockedReason" class="flex items-center gap-2 text-xs text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
        <span>🚫</span><span>{{ blockedReason }}</span>
      </div>

      <!-- 载入条：提交后立刻出现，避免"点了没反应"的体感 -->
      <div v-if="isPlanning" class="flex items-center gap-2 text-xs text-blue-600">
        <span class="relative flex h-2 w-2">
          <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
          <span class="relative inline-flex rounded-full h-2 w-2 bg-blue-500"></span>
        </span>
        AI 正在理解你的需求，预计几秒钟…
      </div>
    </form>
  </div>
</template>
