# 后端接口说明文档

## 概述

本文档定义了前端 `E:\MyTravelAgent\vue` 与后端服务之间的接口契约。
后端服务需实现以下 API 端点供前端调用。

## 接口定义

### 1. 旅行规划

提交用户旅行需求，返回完整的规划结果。

```
POST /api/travel-plan
Content-Type: application/json
```

**请求体：**

```json
{
  "query": "我想明天去成都玩三天, 请帮我制定一个旅行计划, 预算3000元左右"
}
```

**成功响应 (200)：**

```json
{
  "analysis": {
    "text": "用户旅行意图分析结果:\n🏕️ **目的地**：成都\n⏰ **旅行天数**：3天\n...",
    "json": "{ \"destination\": \"成都\", \"duration\": 3, ... }"
  },
  "weather": {
    "text": "用户旅行期间的天气信息与建议: \n**2025-07-01：**\n🌤️ 天气：阴\n...",
    "json": "[{ \"date\": \"2025-07-01\", \"weather\": \"阴\", ... }]"
  },
  "planner": {
    "text": "**🎯 3天行程规划完成！**\n\n**📅 行程时间：** 2025-07-01 至 2025-07-03\n...",
    "json": "[{ \"date\": \"2025-07-01\", \"plan\": [...], \"generalTips\": \"...\" }]"
  }
}
```

**错误响应 (4xx/5xx)：**

```json
{
  "error": "错误描述信息",
  "code": "ERROR_CODE"
}
```

---

### 2. 健康检查

检查后端服务是否正常。

```
GET /api/health
```

**成功响应 (200)：**

```json
{
  "status": "ok"
}
```

---

### 3. SSE 实时推送（可选）

如果后端需要推送 Agent 执行状态到前端，使用 Server-Sent Events。

```
POST /api/travel-plan/sse
Content-Type: application/json
```

**请求体：** 同上

**响应：** `text/event-stream`

```
data: {"id":"analyzerAgent_xxx","name":"AnalyzerAgent","type":"agent","desc":"开始分析...","content":"xxx","contentType":"text","createdAt":1712345678000}

data: {"id":"weatherAgent_xxx","name":"WeatherAgent","type":"agent","desc":"开始获取天气信息...","content":"xxx","contentType":"text","createdAt":1712345679000}

data: {"id":"plannerAgent_xxx","name":"PlannerAgent","type":"agent","desc":"开始制定行程...","content":"xxx","contentType":"text","createdAt":1712345680000}

data: {"analysis":{...},"weather":{...},"planner":{...}}

data: [DONE]
```

## 前端数据结构

### AgentResults

| 字段 | 类型 | 说明 |
|------|------|------|
| analysis.text | string | 分析结果的可读文本 |
| analysis.json | string | 分析结果的 JSON 字符串（可直接 JSON.parse） |
| weather.text | string | 天气信息的可读文本 |
| weather.json | string | 天气信息的 JSON 字符串 |
| planner.text | string | 规划结果的可读文本 |
| planner.json | string | 规划结果的 JSON 字符串 |

### AgentExecutionEvent

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 唯一标识 |
| name | string | Agent 名称（AnalyzerAgent / WeatherAgent / PlannerAgent / SmartSupervisor 等） |
| type | "supervisor" \| "agent" \| "tool" | 类型 |
| desc | string | 描述信息 |
| content | string | 内容 |
| contentType | "json" \| "text" \| "" | 内容类型 |
| createdAt | number | 时间戳 |

## Agent 行为说明（后端实现参考）

后端需按以下顺序依次执行三个 Agent，每个 Agent 的行为如下：

### 1. AnalyzerAgent（需求分析）
- **系统提示词：** 见原项目 `src/agents/analyzerAgent.ts` 的 `systemPrompt`
- **模型调用：** 调用 Moonshot API (`kimi-k2.6` 模型)
- **功能：** 从用户输入中提取目的地、天数、日期、预算、偏好等信息
- **工具：** `get_current_date`（获取当前日期）
- **输出：** JSON 格式的分析结果

### 2. WeatherAgent（天气查询）
- **系统提示词：** 见原项目 `src/agents/weatherAgent.ts` 的 `systemPrompt`
- **模型调用：** 调用 Moonshot API
- **功能：** 根据分析结果中的目的地和日期查询天气
- **工具：** `get_location_id`（通过和风天气 API 获取 LocationId）、`get_weather_by_location_id`（查询天气）
- **输出：** JSON 数组格式的天气数据

### 3. PlannerAgent（行程规划）
- **系统提示词：** 见原项目 `src/agents/plannerAgent.ts` 的 `systemPrompt`
- **模型调用：** 调用 Moonshot API
- **功能：** 根据分析结果和天气信息生成每日行程
- **输出：** JSON 数组格式的行程计划

## 技术栈建议

- **后端语言：** Node.js (Express / Koa / Fastify) 或 Python (FastAPI / Flask)
- **AI SDK：** `openai` npm 包（Node.js）或 `openai` Python 包
- **天气 API：** 和风天气 (QWeather)
- **通信协议：** REST API + 可选 SSE
