"""最终总结校验（依据 skills/travel-summarizer.md 的输出规范）
================================================================================
用途：LLM 生成的最终总结在入 RAG 前先校验，避免把残缺/串味的 JSON 写进三库。

两级校验（策略：**简单格式问题自动修，复杂问题抛异常且不写 RAG**）：

  第 1 级 JSON 格式校验 parse_summary_json()
     自动修复的简单问题（记入 report["json"]["repaired"]）：
       · 首尾空白 / BOM
       · Markdown 围栏 ```json … ``` 或残留 ```
       · 前缀标签 json: / JSON：
       · JSON 前后多余文字（括号匹配截取最外层对象）
       · 尾随逗号（,} / ,]）
       · 全角引号当键名分隔符（“key”:）
     其余（截断、括号不闭合、语法错误、顶层非对象）→ **抛 SummaryFormatError，不写 RAG**

  第 2 级 结构校验 validate_summary()
     字段缺失 / 类型错误 / 枚举越界 / catalog 与 daily_plan 不一致等 → 记入 report
     strict 模式下抛 SummaryStructureError，不写 RAG

入口：
    parse_summary_json(raw)                       -> (obj, {"source","repaired"})
    validate_summary(data)                        -> report（只校验，不抛）
    prepare_summary_for_ingest(raw, strict=True)  -> record（失败即抛异常 → 不写 RAG）
    validate_and_prepare(...)                     -> (record|None, report)（兼容旧调用，不抛）

校验项对应 skill 的输出质量要求：
  1 完整性   daily_plan 覆盖所有天数，每天四个时间段（早上/中午/下午/晚上）
  2 一致性   spots_catalog / food_catalog 与 daily_plan 一一对应
  3 可溯源   sources 非空
  4 实用性   precautions_summary 五个归类
  5 数字精度 预算字段为数字，remaining = 总预算 − 预估总花费
  6 地理位置 daily_plan 与 spots_catalog 必须带经纬度
  7 无冗余   spots_catalog 内同一景点只出现一次
  8 备选推荐 extra_recommendations 3~8 个、不与 catalog 重复、带 recommend_reason

report 结构：
    {
      "ok": bool,                # 无 error 即 True（warning 不影响入库）
      "errors": [str, ...],      # 结构性问题
      "warnings": [str, ...],    # 一致性与质量提示
      "counts": {...},           # 结构规模统计
      "json": {"source": "dict|text", "repaired": [str, ...]},
    }
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from services.env_bootstrap import load_env

load_env()


# ============================================================
# 异常（复杂问题一律抛出，调用方据此跳过入库）
# ============================================================
class RagValidationError(ValueError):
    """RAG 入库前校验失败的基类。"""


class SummaryFormatError(RagValidationError):
    """JSON 格式无法修复（截断、语法错误、顶层非对象…）→ 不写 RAG。"""


class SummaryStructureError(RagValidationError):
    """结构不符合 skills/travel-summarizer.md → 不写 RAG。"""

    def __init__(self, errors: List[str], report: Optional[dict] = None):
        self.errors = list(errors)
        self.report = report or {}
        super().__init__("结构校验未通过：" + "；".join(self.errors[:5]))


# ============================================================
# 第 1 级：JSON 格式校验与简单修复
# ============================================================
def _outermost_json(text: str) -> Optional[str]:
    """括号匹配截取最外层 {...} / [...]，正确处理字符串与转义；不闭合返回 None。"""
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None
    stack: List[str] = []
    in_str = esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                return text[start:i + 1]
    return None


def clean_json_text(text: str) -> Tuple[str, List[str]]:
    """对 LLM 输出做**保守**格式清理，返回 (清理后文本, 已修复项)。"""
    repairs: List[str] = []
    s = (text or "").strip()

    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff").lstrip()
        repairs.append("去除 BOM")

    if "```" in s:
        m = re.search(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", s, re.DOTALL)
        if m:
            s = m.group(1).strip()
            repairs.append("剥离 ```json 代码围栏")
        else:
            s = s.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
            repairs.append("剥离残留 ``` 标记")

    m = re.match(r"^(?:json|JSON)\s*[:：]\s*", s)
    if m and s[m.end():].lstrip().startswith(("{", "[")):
        s = s[m.end():].lstrip()
        repairs.append("去除 'json:' 前缀标签")

    sliced = _outermost_json(s)
    if sliced is not None and sliced != s:
        s = sliced.strip()
        repairs.append("截取最外层 JSON（丢弃前后多余文字）")

    fixed = re.sub(r",(\s*[}\]])", r"\1", s)
    if fixed != s:
        s = fixed
        repairs.append("移除尾随逗号")

    fixed = re.sub(r'([{,]\s*)[“”]([^“”\n]{1,40})[“”](\s*:)', r'\1"\2"\3', s)
    if fixed != s:
        s = fixed
        repairs.append("全角引号转半角（键名）")

    return s, repairs


def parse_summary_json(raw: Any) -> Tuple[dict, Dict[str, Any]]:
    """把最终总结解析成 dict：简单格式问题自动修，复杂问题抛 SummaryFormatError。"""
    if isinstance(raw, dict):
        return raw, {"source": "dict", "repaired": []}
    if not isinstance(raw, str):
        raise SummaryFormatError(f"总结类型异常：{type(raw).__name__}（应为 dict 或 JSON 字符串）")

    text, repairs = clean_json_text(raw)
    if not text:
        raise SummaryFormatError("总结内容为空")

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        sliced = _outermost_json(text)
        obj = None
        if sliced and sliced != text:
            try:
                obj = json.loads(sliced)
                repairs.append("二次截取最外层 JSON")
            except json.JSONDecodeError:
                obj = None
        if obj is None:
            raise SummaryFormatError(
                f"JSON 无法解析（{exc.msg}，位置 {exc.pos}）；常见原因是输出被截断或含非法字符。"
                f"片段：{text[:160]}…") from exc

    if not isinstance(obj, dict):
        raise SummaryFormatError(f"JSON 顶层不是对象，而是 {type(obj).__name__}")
    return obj, {"source": "text", "repaired": repairs}

# ---- 枚举（来自 skill 定义） ----
PERIODS = ("早上", "中午", "下午", "晚上")
SPOT_TYPES = ("scenic_spot", "activity", "food", "accommodation")
PLAN_TYPES = ("free", "scenic_spot", "food", "activity", "accommodation", "travel")
FOOD_CATEGORIES = ("main_dish", "snack", "drink")
BUDGET_STATUS = ("within_budget", "over_budget", "exact")
PACE = ("relaxed", "normal", "intense")
PRECAUTION_KEYS = ("tickets", "transport", "timing", "cost", "other")

REQUIRED_TOP_KEYS = ("meta", "daily_plan", "spots_catalog", "food_catalog",
                     "precautions_summary", "budget_breakdown")


# ============================================================
# 小工具
# ============================================================
def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _has_geo(coords: Any) -> bool:
    return (isinstance(coords, dict)
            and _is_num(coords.get("lng")) and _is_num(coords.get("lat")))


def _as_int(v: Any) -> Optional[int]:
    if _is_num(v):
        return int(v)
    if isinstance(v, str):
        digits = "".join(ch for ch in v if ch.isdigit())
        return int(digits) if digits else None
    return None


def _names(items: Any) -> List[str]:
    if not isinstance(items, list):
        return []
    return [str(i.get("name")).strip() for i in items
            if isinstance(i, dict) and str(i.get("name") or "").strip()]


# ============================================================
# 主校验
# ============================================================
def validate_summary(data: Any, json_meta: Optional[dict] = None) -> Dict[str, Any]:
    """校验最终总结结构。返回 {ok, errors, warnings, counts, json}。"""
    errors: List[str] = []
    warnings: List[str] = []
    json_info = json_meta or {"source": "dict", "repaired": []}

    if not isinstance(data, dict):
        return {"ok": False, "errors": [f"顶层不是对象，而是 {type(data).__name__}"],
                "warnings": [], "counts": {}, "json": json_info}

    # 完全不合法（如超时回传的 {"error": ..., "locations_summary": [...]}）
    if "error" in data and "daily_plan" not in data:
        errors.append(f"数据为错误回传，非有效总结：{str(data.get('error'))[:120]}")

    for key in REQUIRED_TOP_KEYS:
        if key not in data:
            errors.append(f"缺少顶层字段：{key}")

    meta = data.get("meta") or {}
    daily_plan = data.get("daily_plan") or {}
    spots = data.get("spots_catalog") or []
    foods = data.get("food_catalog") or []
    extras = data.get("extra_recommendations") or []
    precautions = data.get("precautions_summary") or {}
    budget = data.get("budget_breakdown") or {}

    # ---------- ① meta ----------
    if not isinstance(meta, dict):
        errors.append("meta 不是对象")
    else:
        dests = meta.get("destinations")
        if not (isinstance(dests, list) and dests):
            errors.append("meta.destinations 缺失或为空（无法确定目的地）")
        if meta.get("pace") and meta["pace"] not in PACE:
            warnings.append(f"meta.pace='{meta['pace']}' 不在 {PACE} 内")
        mb = meta.get("budget") or {}
        if mb and mb.get("status") and mb["status"] not in BUDGET_STATUS:
            warnings.append(f"meta.budget.status='{mb['status']}' 不在 {BUDGET_STATUS} 内")
        if _as_int(meta.get("total_days")) is None:
            warnings.append("meta.total_days 无法解析为天数")

    # ---------- ② daily_plan：天数覆盖 + 四时段 ----------
    if not isinstance(daily_plan, dict) or not daily_plan:
        errors.append("daily_plan 缺失或为空")
    else:
        expect_days = _as_int(meta.get("total_days")) if isinstance(meta, dict) else None
        if expect_days and len(daily_plan) != expect_days:
            warnings.append(f"daily_plan 有 {len(daily_plan)} 天，与 meta.total_days={expect_days} 不一致")
        for day_name, day in daily_plan.items():
            if not isinstance(day, dict):
                errors.append(f"daily_plan.{day_name} 不是对象")
                continue
            periods = day.get("periods")
            if not isinstance(periods, list) or not periods:
                errors.append(f"daily_plan.{day_name}.periods 缺失或为空")
                continue
            got_periods = [p.get("period") for p in periods if isinstance(p, dict)]
            missing_periods = [p for p in PERIODS if p not in got_periods]
            if missing_periods:
                warnings.append(f"daily_plan.{day_name} 缺时间段：{'、'.join(missing_periods)}")
            for idx, p in enumerate(periods):
                if not isinstance(p, dict):
                    errors.append(f"daily_plan.{day_name}.periods[{idx}] 不是对象")
                    continue
                if p.get("period") not in PERIODS:
                    warnings.append(f"daily_plan.{day_name}.periods[{idx}].period='{p.get('period')}' 越界")
                if p.get("type") and p["type"] not in PLAN_TYPES:
                    warnings.append(f"daily_plan.{day_name}.periods[{idx}].type='{p['type']}' 越界")
                detail = p.get("detail") or {}
                name = str(detail.get("name") or "").strip()
                if name and not _has_geo(detail.get("coordinates")):
                    warnings.append(f"daily_plan.{day_name}『{name}』缺经纬度（skill 要求带坐标）")
            if not _is_num(day.get("day_cost")) and day.get("day_cost") not in (None, ""):
                warnings.append(f"daily_plan.{day_name}.day_cost 非数字：{day.get('day_cost')!r}")

    # ---------- ③ spots_catalog ----------
    if not isinstance(spots, list) or not spots:
        errors.append("spots_catalog 缺失或为空")
    else:
        for idx, s in enumerate(spots):
            if not isinstance(s, dict):
                errors.append(f"spots_catalog[{idx}] 不是对象")
                continue
            name = str(s.get("name") or "").strip()
            tag = name or f"[{idx}]"
            if not name:
                errors.append(f"spots_catalog[{idx}] 缺 name")
            if s.get("type") and s["type"] not in SPOT_TYPES:
                warnings.append(f"spots_catalog『{tag}』type='{s['type']}' 越界")
            if not _has_geo(s.get("coordinates")):
                warnings.append(f"spots_catalog『{tag}』缺经纬度")
            if not str(s.get("summary") or "").strip():
                warnings.append(f"spots_catalog『{tag}』缺 summary")
            if not isinstance(s.get("sources"), list) or not s.get("sources"):
                warnings.append(f"spots_catalog『{tag}』sources 为空（skill 要求可溯源）")
        dup = [n for n in set(_names(spots)) if _names(spots).count(n) > 1]
        if dup:
            errors.append(f"spots_catalog 内重复条目：{'、'.join(dup)}（skill 要求同一景点只出现一次）")

    # ---------- ④ food_catalog ----------
    if not isinstance(foods, list) or not foods:
        warnings.append("food_catalog 为空（skill 建议每地至少保留 1 个推荐美食）")
    else:
        for idx, f in enumerate(foods):
            if not isinstance(f, dict):
                errors.append(f"food_catalog[{idx}] 不是对象")
                continue
            name = str(f.get("name") or "").strip() or f"[{idx}]"
            if f.get("category") and f["category"] not in FOOD_CATEGORIES:
                warnings.append(f"food_catalog『{name}』category='{f['category']}' 越界")
            if not isinstance(f.get("sources"), list) or not f.get("sources"):
                warnings.append(f"food_catalog『{name}』sources 为空")

    # ---------- ⑤ extra_recommendations ----------
    if not isinstance(extras, list):
        errors.append("extra_recommendations 不是数组")
    else:
        if extras and not (3 <= len(extras) <= 8):
            warnings.append(f"extra_recommendations 有 {len(extras)} 个，skill 要求 3~8 个")
        catalog_names = set(_names(spots)) | set(_names(foods))
        for idx, e in enumerate(extras):
            if not isinstance(e, dict):
                errors.append(f"extra_recommendations[{idx}] 不是对象")
                continue
            name = str(e.get("name") or "").strip() or f"[{idx}]"
            if not str(e.get("recommend_reason") or "").strip():
                warnings.append(f"extra_recommendations『{name}』缺 recommend_reason")
            if name in catalog_names:
                warnings.append(f"extra_recommendations『{name}』与 catalog 重复（skill 要求不重复）")

    # ---------- ⑥ precautions_summary ----------
    if not isinstance(precautions, dict) or not precautions:
        warnings.append("precautions_summary 缺失或为空")
    else:
        for key in PRECAUTION_KEYS:
            if key not in precautions:
                warnings.append(f"precautions_summary 缺归类：{key}")

    # ---------- ⑦ budget_breakdown ----------
    if not isinstance(budget, dict) or not budget:
        warnings.append("budget_breakdown 缺失或为空")
    else:
        parts = ("tickets", "food", "transport", "accommodation", "other")
        for key in parts + ("total", "remaining"):
            if key in budget and budget[key] not in (None, "") and not _is_num(budget[key]):
                warnings.append(f"budget_breakdown.{key} 非数字：{budget[key]!r}（skill 要求数字类型）")

        # skill 要求：预算余额 = 总预算 − 预估总花费
        meta_budget = (meta.get("budget") or {}) if isinstance(meta, dict) else {}
        total_budget = meta_budget.get("total")
        estimated = meta_budget.get("estimated")
        if not _is_num(estimated):
            estimated = budget.get("total") if _is_num(budget.get("total")) else sum(
                budget.get(k, 0) for k in parts if _is_num(budget.get(k)))
        if _is_num(budget.get("remaining")) and _is_num(total_budget) and _is_num(estimated):
            expect = total_budget - estimated
            if abs(expect - budget["remaining"]) > 1:
                warnings.append(f"budget_breakdown.remaining={budget['remaining']} 不等于 "
                                f"总预算-预估花费（{total_budget}-{estimated}={expect}）")

    # ---------- ⑧ 一致性：catalog 是否出现在 daily_plan ----------
    plan_text = _flatten_plan_text(daily_plan)
    if plan_text:
        for n in _names(spots)[:20]:
            if n and n not in plan_text:
                warnings.append(f"spots_catalog『{n}』未出现在 daily_plan 中（skill 要求一一对应）")

    counts = {
        "days": len(daily_plan) if isinstance(daily_plan, dict) else 0,
        "spots": len(spots) if isinstance(spots, list) else 0,
        "foods": len(foods) if isinstance(foods, list) else 0,
        "extras": len(extras) if isinstance(extras, list) else 0,
        "plan_events": len(plan_text.split()) if plan_text else 0,
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "counts": counts, "json": json_info}


def _flatten_plan_text(daily_plan: Any) -> str:
    """把 daily_plan 的文本内容拉平成一个字符串，用于"是否被提及"的一致性检查。"""
    chunks: List[str] = []
    if isinstance(daily_plan, dict):
        for day in daily_plan.values():
            if not isinstance(day, dict):
                continue
            for p in day.get("periods") or []:
                if isinstance(p, dict):
                    chunks.append(str(p.get("content") or ""))
                    detail = p.get("detail") or {}
                    chunks.append(str(detail.get("name") or ""))
                    chunks.append(str(detail.get("summary") or ""))
    return " ".join(c for c in chunks if c)


# ============================================================
# 转成入库记录
# ============================================================
def summary_to_record(summary: dict, *, user_id: Optional[str] = None,
                      username: Optional[str] = None, nickname: Optional[str] = None,
                      title: Optional[str] = None, is_public: bool = False,
                      tags: Optional[List[str]] = None) -> dict:
    """把最终总结转成 services.rag.ingest 需要的记录结构（content 为完整总结 JSON）。"""
    from services.rag.transform import _format_meta

    meta = summary.get("meta") or {}
    dests = meta.get("destinations") or []
    if isinstance(dests, list):
        destination = "、".join(str(d) for d in dests if str(d).strip())
    else:
        destination = str(dests or "").strip()

    daily_plan = summary.get("daily_plan") or {}
    days = _as_int(meta.get("total_days")) or (len(daily_plan) if isinstance(daily_plan, dict) else None)
    if not destination:
        raise ValueError("总结缺少 meta.destinations，无法确定目的地")

    if not title:
        title = f"{destination}{days or ''}天旅游攻略"

    # 标签：目的地 + 节奏 + 预算档位，便于 ES 关键词召回
    auto_tags: List[str] = [str(d) for d in (dests if isinstance(dests, list) else [dests]) if d]
    if meta.get("pace"):
        auto_tags.append(str(meta["pace"]))
    auto_tags += [str(t) for t in (tags or [])]

    return {
        "user_id": user_id,
        "username": username,
        "nickname": nickname,
        "title": title,
        "destination": destination,
        "days": days,
        "summary": _format_meta(meta) or "",      # 展示/ES 用的自然语言摘要
        "tags": list(dict.fromkeys(auto_tags)),
        "is_public": is_public,
        "content": summary,                        # 完整总结 JSON → PG content_json
    }


def prepare_summary_for_ingest(raw: Any, *, strict: bool = True,
                               **owner: Any) -> Tuple[Optional[dict], Dict[str, Any]]:
    """解析 → 结构校验 → 转入库记录（入库前唯一推荐入口）。

    策略：
      · **简单格式问题自动修复**：``` 围栏、`json:` 前缀、前后多余文字、尾随逗号、全角引号键名
        （修复项记入 report["json"]["repaired"]，不阻断入库）
      · **复杂问题一律抛异常且不写 RAG**：
          JSON 截断 / 无法解析 / 顶层非对象        → SummaryFormatError
          结构校验有 error（strict=True，默认）    → SummaryStructureError
    """
    obj, json_meta = parse_summary_json(raw)          # 复杂格式问题在此抛出
    report = validate_summary(obj, json_meta)
    if not report["ok"]:
        if strict:
            raise SummaryStructureError(report["errors"], report)
        return None, report

    try:
        record = summary_to_record(obj, **owner)
    except Exception as exc:
        report["errors"].append(f"转换为入库记录失败：{exc}")
        report["ok"] = False
        if strict:
            raise SummaryStructureError(report["errors"], report) from exc
        return None, report
    return record, report


def validate_and_prepare(summary: Any, *, require_ok: bool = True,
                         **owner: Any) -> Tuple[Optional[dict], Dict[str, Any]]:
    """兼容旧调用：**不抛异常**，返回 (record|None, report)。

    require_ok=True 时结构有 error 就不产出 record（调用方据此跳过入库）；
    需要"异常语义"请直接用 prepare_summary_for_ingest(..., strict=True)。
    """
    try:
        return prepare_summary_for_ingest(summary, strict=require_ok, **owner)
    except RagValidationError as exc:
        report = dict(getattr(exc, "report", None) or {})
        report["ok"] = False
        report.setdefault("errors", [])
        report.setdefault("warnings", [])
        report.setdefault("counts", {})
        report.setdefault("json", {"source": "unknown", "repaired": []})
        if str(exc) not in report["errors"]:
            report["errors"].append(str(exc))
        return None, report
