"""统一的 .env 加载入口
================================================================================
问题背景：多个模块（embedding / guide_ingest / es_client …）在**导入时**就读取
环境变量。若此时 .env 尚未加载，则 .env 里的配置会被静默忽略，只剩默认值。

因此：任何在模块级读取环境变量的模块，都必须在读取**之前**调用 load_env()。

特性
----
- 幂等：多次调用只加载一次。
- 定位稳定：优先加载项目根目录的 .env（相对本文件上两级），与当前工作目录无关。
- 不覆盖已有环境变量（override=False），便于容器/CI 用真实环境变量覆盖 .env。
- python-dotenv 缺失时静默跳过（不阻断纯环境变量部署）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_LOADED = False
_ENV_PATH: Optional[Path] = None

# 项目根目录：services/env_bootstrap.py → services/ → 项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def setup_console() -> None:
    """让 Windows 控制台能安全输出中文与 ✅/❌ 等符号。

    背景：中文 Windows 控制台默认 GBK(cp936)，无法编码 ✅/❌，直接 print 会抛
    UnicodeEncodeError 并中断脚本。这里把 stdout/stderr 的 errors 降级为 "replace"
    （保留原编码，避免 UTF-8 字节在 GBK 控制台显示为乱码），使符号变成 "?" 而不崩溃。
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            enc = (stream.encoding or "").lower()
            if "utf" in enc:
                # 已经是 UTF-8（如 Windows Terminal / -X utf8）：仅兜底 replace
                reconfigure(errors="replace")
            else:
                # GBK 等窄编码：保留编码（中文正常显示），不可编码字符降级为 ?
                reconfigure(errors="replace")
        except Exception:
            pass


def load_env() -> None:
    """加载项目根目录的 .env（幂等），并顺手修正控制台编码行为。"""
    global _LOADED, _ENV_PATH
    if _LOADED:
        return
    _LOADED = True
    setup_console()
    try:
        from dotenv import load_dotenv
    except ImportError:                     # 纯环境变量部署场景
        return

    candidate = PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=False)
        _ENV_PATH = candidate
    else:
        # 回退：按 python-dotenv 默认策略搜索（当前目录及上级）
        load_dotenv(override=False)


def env_file_path() -> Optional[Path]:
    """返回实际加载的 .env 路径；未找到返回 None（可据此提示用户 cp .env.example .env）。"""
    load_env()
    return _ENV_PATH


def env_file_hint() -> str:
    """给日志/报错用的一行说明。"""
    path = env_file_path()
    if path:
        return str(path)
    return f"未找到 {PROJECT_ROOT / '.env'}（可执行 cp .env.example .env 后填入密钥）"
