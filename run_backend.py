"""启动后端（uvicorn），并把控制台输出同时写入日志文件
================================================================================

为什么需要它：
    `python -m uvicorn main:app` 的输出只存在于那个终端窗口里 —— 用后台任务/服务
    方式启动时就"看不见"了。本脚本把 stdout/stderr（包含 uvicorn 访问日志、项目
    logger、以及代码里所有 print 的进度信息）同时 tee 到：

        logs/backend.log          默认路径，可用环境变量 BACKEND_LOG_FILE 覆盖

于是可以用三种方式查看后端输出：
    1. 浏览器：前端「🖥️ 后端日志」页（走 /api/dev/logs/stream，实时跟随）
    2. 终端：  Get-Content logs\\backend.log -Wait -Tail 50
    3. 直接打开 logs\\backend.log

用法：
    python run_backend.py                 # 默认 127.0.0.1:8088
    BACKEND_PORT=9000 python run_backend.py
    BACKEND_RELOAD=1 python run_backend.py   # 改代码自动重启（开发用）
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent


class _Tee(io.TextIOBase):
    """把写入同时转发到多个流（控制台 + 日志文件）。

    控制台在中文 Windows 下可能是 GBK，遇到 ✅ 这类字符会抛 UnicodeEncodeError，
    这里按 replace 降级重写，保证"日志文件里是完整内容、控制台不崩"。
    """

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]
        self._primary = self._streams[0] if self._streams else None
        self._file = self._streams[-1] if self._streams else None

    # --- 让 setup_console()/logging 等仍能正常工作 ---
    @property
    def encoding(self):
        return getattr(self._primary, "encoding", None) or "utf-8"

    @property
    def errors(self):
        return getattr(self._primary, "errors", None) or "replace"

    def isatty(self) -> bool:
        isatty = getattr(self._primary, "isatty", None)
        return bool(isatty()) if callable(isatty) else False

    def fileno(self):
        return self._primary.fileno() if self._primary is not None else super().fileno()

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def reconfigure(self, **kwargs):
        """转发给底层流（env_bootstrap.setup_console 会调用）。"""
        for stream in self._streams:
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                try:
                    reconfigure(**kwargs)
                except Exception:
                    pass

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        for stream in self._streams:
            try:
                stream.write(text)
            except UnicodeEncodeError:
                encoding = getattr(stream, "encoding", None) or "utf-8"
                try:
                    stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
                except Exception:
                    pass
            except Exception:
                pass
        return len(text)

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    log_path = pathlib.Path(os.getenv("BACKEND_LOG_FILE", str(ROOT / "logs" / "backend.log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)

    # 先接上 tee，再做任何会写日志的导入
    sys.stdout = _Tee(sys.__stdout__, log_fh)
    sys.stderr = _Tee(sys.__stderr__, log_fh)

    logging.basicConfig(
        level=os.getenv("BACKEND_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    host = os.getenv("BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("BACKEND_PORT", "8088"))
    reload_enabled = os.getenv("BACKEND_RELOAD", "").strip().lower() in ("1", "true", "yes", "on")

    banner = (
        "\n" + "=" * 78 + "\n"
        f" 后端启动：http://{host}:{port}\n"
        f" 日志文件：{log_path}\n"
        f" 实时查看：前端「🖥️ 后端日志」页，或  Get-Content '{log_path}' -Wait -Tail 50\n"
        + "=" * 78
    )
    print(banner, flush=True)

    import uvicorn

    if reload_enabled:
        uvicorn.run(
            "main:app", host=host, port=port,
            log_level=os.getenv("BACKEND_UVICORN_LEVEL", "info"), reload=True,
            reload_dirs=[str(ROOT / "controller"), str(ROOT / "services"), str(ROOT / "functions")],
        )
    else:
        uvicorn.run(
            "main:app", host=host, port=port,
            log_level=os.getenv("BACKEND_UVICORN_LEVEL", "info"),
        )


if __name__ == "__main__":
    main()
