"""攻略分析任务：把"确认意图后"的耗时分析从 HTTP 请求里解耦出来
================================================================================

背景（这就是"切到别的页面，进度就停了并重置"的根因）：

    原实现里 `summary_from_notes()` 的分析过程是在 `/api/summary/stream` 这个
    SSE **请求内部**跑的：客户端一断开（切页导致组件卸载后主动 abort、刷新页面、
    关标签页），FastAPI 就会 aclose 掉生成器 → 分析被取消；即使没取消，进度事件
    也只推给当前这一个连接，前端组件一销毁，看到的进度自然"归零"。

本模块把分析变成一个**后台任务**，进度事件按 task_id 缓冲，任何连接都可以：

    · 订阅（subscribe）：先重放已有事件，再跟随新事件，直到任务结束；
    · 重放：新连接从 from_index=0 开始就能拿到完整进度（前端刷新/切页后追平）；
    · 快照（snapshot）：GET /api/summary/task/{task_id} 一次性拿到当前状态。

同一 task_id 只会真正分析一次（重复确认、多端订阅都只是挂到同一个任务上）。

对外接口：
    start_summary_task(task_id, owner) -> SummaryTask      幂等创建并启动
    get_task(task_id)                  -> SummaryTask|None
    subscribe_task(task_id, from_index) -> AsyncIterator[{"type","data"}]
    snapshot_task(task_id)             -> dict
    save_summary_to_rag(summary, owner) -> dict            校验 + 写入 RAG
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger("services.summary_task")

# 订阅空闲时的心跳间隔（秒）：避免长时间无事件被中间代理判定为死连接
HEARTBEAT_SECONDS = float(os.getenv("SUMMARY_SSE_HEARTBEAT", "15"))
# 任务缓冲保留上限与过期时间
MAX_TASKS = int(os.getenv("SUMMARY_MAX_TASKS", "50"))
TASK_TTL_SECONDS = float(os.getenv("SUMMARY_TASK_TTL", str(6 * 3600)))
# 单个任务最多缓冲多少事件（超出丢弃最早的进度事件，done/saved 等终态事件必留）
MAX_EVENTS = int(os.getenv("SUMMARY_MAX_EVENTS", "2000"))

TERMINAL_STATUSES = {"done", "error", "cancelled"}


def _env_flag(name: str, default: bool = True) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


# ================================================================
# 最终总结 → 结构校验 → 写入 RAG
# ================================================================
def save_summary_to_rag(summary: dict, owner: Optional[Dict[str, Any]] = None) -> dict:
    """校验最终总结 → 写入 RAG（PG 真相源 + ES + Qdrant 分块向量）。

    同步阻塞函数（含 embedding 网络调用），请在线程中执行（asyncio.to_thread）。

    校验策略（services/rag/validate.py）：
      · 简单格式问题（``` 围栏、`json:` 前缀、前后多余文字、尾随逗号…）自动修复后继续；
      · 复杂问题（JSON 截断/无法解析、结构不符合 skill 规范）→ 抛 RagValidationError，不写 RAG。

    返回：{"validation": report, "ingested": [{guide_id, chunks}], "skipped": None}
    """
    from services.rag.ingest import IngestOptions, ingest_guides
    from services.rag.validate import prepare_summary_for_ingest

    owner = owner or {}
    strict = _env_flag("RAG_VALIDATE_STRICT", True)
    record, report = prepare_summary_for_ingest(
        summary,
        strict=strict,
        user_id=owner.get("user_id"),
        username=owner.get("username"),
        nickname=owner.get("nickname"),
        is_public=bool(owner.get("is_public")),
    )

    out: Dict[str, Any] = {"validation": report, "ingested": None, "skipped": None}
    if record is None:                       # strict=False 且校验未通过
        out["skipped"] = "结构校验未通过，已跳过入库"
        return out

    result = ingest_guides([record], options=IngestOptions(
        user_id=owner.get("user_id"),
        username=owner.get("username"),
        nickname=owner.get("nickname"),
        verbose=False,
    ))
    out["ingested"] = result.get("ingested") or []
    return out


def auto_ingest_enabled(override: Optional[bool]) -> bool:
    if override is not None:
        return bool(override)
    return _env_flag("RAG_AUTO_INGEST", True)


# ================================================================
# 任务对象
# ================================================================
@dataclass
class SummaryTask:
    """一次"确认意图 → 分析 → 写 RAG"的后台任务，带可重放的事件缓冲。"""

    task_id: int
    owner: Dict[str, Any] = field(default_factory=dict)
    owner_user_id: str = ""                  # 归属用户：接口鉴权 + 多用户 MCP 实例都靠它
    status: str = "running"                  # running | done | error | cancelled
    events: List[Dict[str, Any]] = field(default_factory=list)
    final_summary: Any = None
    ingest_report: Any = None
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    runner: Optional[asyncio.Task] = None
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    # ---------------- 事件写入 ----------------
    async def emit(self, event_type: str, data: Any) -> None:
        async with self._cond:
            if len(self.events) >= MAX_EVENTS:
                # 缓冲上限：丢掉最早的"纯进度"事件，避免长任务把内存撑爆
                self.events = self.events[len(self.events) // 4:]
            self.events.append({"type": event_type, "data": data})
            self._cond.notify_all()

    async def close(self, status: str, error: str = "") -> None:
        async with self._cond:
            self.status = status
            self.error = error
            self.finished_at = time.time()
            self._cond.notify_all()
        logger.info(
            "分析任务结束 task_id=%s status=%s 事件数=%d 耗时=%.1fs",
            self.task_id, status, len(self.events),
            self.finished_at - self.started_at,
        )

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

    # ---------------- 订阅 ----------------
    async def subscribe(self, from_index: int = 0) -> AsyncIterator[Dict[str, Any]]:
        """先重放 events[from_index:]，再跟随新事件，直到任务结束。

        空闲 HEARTBEAT_SECONDS 会 yield 一条 {"type": "ping"}，让 SSE 连接保持活跃。
        """
        index = max(0, from_index)
        while True:
            async with self._cond:
                while index >= len(self.events) and not self.finished:
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        break
                batch = self.events[index:]
                index = len(self.events)
                finished = self.finished

            if not batch and not finished:
                yield {"type": "ping", "data": {"ts": int(time.time() * 1000)}}
                continue
            for event in batch:
                yield event
            if finished and index >= len(self.events):
                return

    # ---------------- 快照 ----------------
    def snapshot(self, include_events: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "task_id": self.task_id,
            "status": self.status,
            "index": len(self.events),
            "final_summary": self.final_summary,
            "ingest_report": self.ingest_report,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
        }
        if include_events:
            data["events"] = list(self.events)
        return data


# ================================================================
# 任务注册表
# ================================================================
_TASKS: Dict[int, SummaryTask] = {}


def _purge_stale() -> None:
    """清理过期/超额的历史任务（内存兜底）。"""
    now = time.time()
    for task_id, task in list(_TASKS.items()):
        if task.finished and now - (task.finished_at or task.started_at) > TASK_TTL_SECONDS:
            _TASKS.pop(task_id, None)
    if len(_TASKS) > MAX_TASKS:
        ordered = sorted(_TASKS.values(), key=lambda t: t.started_at)
        for task in ordered[: len(_TASKS) - MAX_TASKS]:
            if task.finished:
                _TASKS.pop(task.task_id, None)


def get_task(task_id: int) -> Optional[SummaryTask]:
    return _TASKS.get(int(task_id))


def _run_analysis(task: SummaryTask) -> asyncio.Task:
    """启动真正的分析协程（与任何 HTTP 连接无关）。"""
    return asyncio.create_task(_run_summary_task(task), name=f"summary-task-{task.task_id}")


async def _run_summary_task(task: SummaryTask) -> None:
    """分析 → 推送进度事件 → 写 RAG → 关闭任务。异常一律转成 error 事件。"""
    from functions.summary_from_notes import summary_from_notes
    from services.rag.validate import RagValidationError, SummaryFormatError
    from utils.redis_storage import RedisMemory

    # 多用户模式下，用该任务归属用户自己的小红书 MCP 实例（一人一实例、cookies 隔离）
    from services.xhs_manager import mcp_url_for
    mcp_url = mcp_url_for(task.owner_user_id)
    logger.info("分析任务开始 task_id=%s user=%s mcp=%s", task.task_id, task.owner_user_id, mcp_url)
    try:
        redis = RedisMemory()
        async for event in summary_from_notes(task.task_id, redis, mcp_url=mcp_url):
            etype = event.get("type")
            data = event.get("data")
            if etype == "done":
                task.final_summary = data
            await task.emit(etype, data)
            if etype == "error":
                await task.close("error", str((data or {}).get("message", "")))
                return

        if task.final_summary is not None and auto_ingest_enabled(task.owner.get("save_to_rag")):
            await task.emit("saving", {"message": "正在写入 RAG（PG / ES / Qdrant）…"})
            try:
                # 入库含 embedding 网络调用且是同步实现 → 放线程，避免阻塞事件循环
                report = await asyncio.to_thread(save_summary_to_rag, task.final_summary, task.owner)
                task.ingest_report = report
                await task.emit("saved", report)
            except RagValidationError as exc:
                kind = "format" if isinstance(exc, SummaryFormatError) else "structure"
                logger.warning("RAG 校验未通过，未写入 RAG（%s）: %s", kind, exc)
                await task.emit("validation_error", {
                    "kind": kind,
                    "message": str(exc),
                    "errors": getattr(exc, "errors", []),
                    "report": getattr(exc, "report", {}),
                })
            except Exception as exc:                      # noqa: BLE001 入库失败不影响总结
                logger.warning("RAG 入库失败: %s", exc)
                await task.emit("save_error", {"message": str(exc)})

        await task.close("done")
    except asyncio.CancelledError:
        await task.close("cancelled", "任务被取消")
        raise
    except Exception as exc:                              # noqa: BLE001 任何异常都要告知前端
        logger.exception("分析任务失败: %s", exc)
        await task.emit("error", {"message": str(exc)})
        await task.close("error", str(exc))


def start_summary_task(task_id: int, owner: Optional[Dict[str, Any]] = None,
                       owner_user_id: str = "") -> SummaryTask:
    """幂等启动任务：已存在（跑着或已结束）则直接返回，不会重复分析。

    owner_user_id 必须由**鉴权后的当前用户**传入：它既用于接口归属校验（防止
    越权读取别人的任务），也用于分配该用户自己的小红书 MCP 实例。
    """
    task_id = int(task_id)
    existing = _TASKS.get(task_id)
    if existing is not None:
        if owner:
            # 补齐/更新归属信息（重复确认时可能带上了用户信息）
            for key, value in owner.items():
                if value is not None:
                    existing.owner[key] = value
        if owner_user_id and not existing.owner_user_id:
            existing.owner_user_id = owner_user_id
        return existing

    _purge_stale()
    task = SummaryTask(task_id=task_id, owner=dict(owner or {}), owner_user_id=owner_user_id or "")
    _TASKS[task_id] = task
    task.runner = _run_analysis(task)
    return task


def assert_task_owner(task: "SummaryTask", user_id: Optional[str]) -> None:
    """校验任务归属；不属于该用户则抛 PermissionError（路由转 403）。

    没有归属用户（老任务/匿名创建）时按"无主"处理：只允许同一个 user_id 明确为空
    的调用者访问，避免任何人凭 task_id 猜到就能读。
    """
    if not task.owner_user_id:
        return
    if not user_id or str(user_id) != str(task.owner_user_id):
        raise PermissionError("无权访问该分析任务")


async def subscribe_task(task_id: int, from_index: int = 0,
                         user_id: Optional[str] = None) -> AsyncIterator[Dict[str, Any]]:
    """按 task_id 订阅进度；任务不存在抛 KeyError（路由转 404），越权抛 PermissionError（转 403）。"""
    task = _TASKS.get(int(task_id))
    if task is None:
        raise KeyError(task_id)
    assert_task_owner(task, user_id)
    async for event in task.subscribe(from_index=from_index):
        yield event


def snapshot_task(task_id: int, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    task = _TASKS.get(int(task_id))
    if task is None:
        return None
    assert_task_owner(task, user_id)
    return task.snapshot()


async def cancel_task(task_id: int, user_id: Optional[str] = None) -> bool:
    """取消正在跑的任务（前端"停止分析"用）。"""
    task = _TASKS.get(int(task_id))
    if task is None or task.finished or task.runner is None:
        return False
    assert_task_owner(task, user_id)
    task.runner.cancel()
    return True
