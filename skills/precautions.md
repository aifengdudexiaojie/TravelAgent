---
name: precautions
description: 旅游注意事项/避坑信息的通用处理规则和分类体系，供所有旅游 Agent 共享引用
metadata:
  type: reference
---

# 注意事项处理规则 (Precautions Common Rules)

## 定义

"注意事项"指帖子中提到的**负面体验、避坑指南、提醒警告**。是所有旅游 Agent 在处理帖子内容时必须遵守的共同规则。

## 提取规则

### 必须提取为注意事项的内容
- ❌ **价格欺诈/隐形消费**：如"门口卖80里面只要50"
- ❌ **时间陷阱**：如"说是2小时逛完其实要走半天"
- ❌ **交通不便**：如"没有直达公交，打车很贵"
- ❌ **体验落差**：如"照片看着很美实际很一般"
- ❌ **排队/拥挤**：如"周末人超级多，排队1小时"
- ❌ **季节不宜**：如"冬天去没什么看的"
- ❌ **安全提醒**：如"路很滑要小心""山上蚊虫多"
- ❌ **饮食注意**：如"辣度很高不能吃辣慎入""海鲜不新鲜"

### 不应作为注意事项
- ✅ 客观事实描述（"门票80元"→ 这是消费信息，不是注意事项）
- ✅ 个人偏好（"我不喜欢这种风格"→ 除非是普遍性的体验落差）
- ✅ 正面推荐（"非常值得去"→ 这是总结内容，不是注意事项）

## 分类体系（9类枚举）

| 类别 | 标签 | 示例 |
|------|------|------|
| 门票相关 | `tickets` | 票价虚高、有更便宜的购票渠道 |
| 交通相关 | `transport` | 停车难、公交不便、打车贵 |
| 时间相关 | `timing` | 排队久、营业时间限制、季节不对 |
| 消费相关 | `cost` | 景区内消费贵、隐性收费 |
| 安全相关 | `safety` | 路滑、蚊虫、天气危险 |
| 饮食相关 | `food` | 口味辣、不卫生 |
| 拥挤相关 | `crowd` | 人多排队、最佳时段 |
| 导向相关 | `navigation` | 容易迷路、导航不准 |
| 其他 | `other` | 通用提醒 |

## 置信度标记

| 标记 | 含义 |
|------|------|
| `direct_quote` | 原文明确说了，可直接引用 |
| `inferred` | 从上下文中推断出的潜在问题 |
| `repeated` | 被多个帖子反复提及，需重点关注 |

## 严重性分级

| 等级 | 含义 |
|------|------|
| `high` | 可能导致行程失败/安全问题/重大经济损失 |
| `medium` | 影响体验但可克服 |
| `low` | 轻微提醒 |

## 优先级

当同一景点在不同帖子中存在矛盾信息时：
1. **多数一致** > 单一说法
2. **近期帖子**（按时间戳）> 早期帖子
3. **有具体细节** > 笼统说法
4. `repeated` 标记注意事项 > `direct_quote` > `inferred`

## 与各 Skill 的引用关系

```
precautions.md (本文件 — 通用规则定义)
├──→ travel-precautions-extractor.md   使用 9类分类 + 严重性分级 + 置信度
├──→ travel-post-filter.md             使用最基本的 precautions 字段提取
├──→ travel-content-extractor.md       编排层间接使用（通过调用子Agent）
├──→ travel-summarizer.md              使用分类体系做 precautions_summary 汇总
└──→ travel-planner.md                 注意事项影响取舍决策 (severity=high 的优先处理)
```

- [travel-precautions-extractor.md](./travel-precautions-extractor.md) — **本规则的直接使用者**，每个注意事项使用 9 类分类 + severity 分级
- [travel-post-filter.md](./travel-post-filter.md) — 初步提取时使用基本规则
- [travel-content-extractor.md](./travel-content-extractor.md) — 编排层，编排 4 个子 Agent，间接使用本规则
- [travel-summarizer.md](./travel-summarizer.md) — 最终总结时使用分类体系做 precautions_summary
- [travel-planner.md](./travel-planner.md) — 取舍决策时参考 severity=high 的注意事项
