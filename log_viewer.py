"""独立的后端日志查看服务（单独端口 + 固定口令）
================================================================================

为什么单独做：日志里含有请求路径、task_id、用户信息等，直接挂在主应用的
`/api/dev/logs` 上意味着"任何登录用户都能看全站日志"。这里改成**独立进程 + 独立端口 +
HTTP Basic 固定口令**，只给你自己排查问题用，主应用可以完全不暴露日志接口。

启动：
    python log_viewer.py                       # 默认 127.0.0.1:8099
    LOG_VIEWER_PORT=9000 python log_viewer.py
    # 生产（只在内网/跳板机可达的地址上监听）：
    LOG_VIEWER_HOST=0.0.0.0 LOG_VIEWER_PASSWORD='<你自己的口令>' python log_viewer.py

环境变量：
    LOG_VIEWER_HOST      监听地址（默认 127.0.0.1，公网部署务必保持本机/内网）
    LOG_VIEWER_PORT      监听端口（默认 8099）
    LOG_VIEWER_USER      用户名（默认 admin）
    LOG_VIEWER_PASSWORD  登录口令（默认 logviewer，**请务必改掉**）
    BACKEND_LOG_FILE     日志文件路径（默认 <项目根>/logs/backend.log）
    LOG_VIEWER_MAX_LINES 页面上最多保留多少行（默认 3000）

也能同时看多个日志文件：用 `?file=` 指定 logs/ 目录下的文件名（默认 backend.log）。
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import pathlib
import secrets
import sys
from typing import List, Optional

ROOT = pathlib.Path(__file__).resolve().parent

# ⚠️ 必须在读取任何环境变量**之前**加载 .env：
#    用户名/口令等配置统一写在项目根目录的 .env 里（与主应用共用同一份）。
#    env_bootstrap.load_env() 幂等、定位稳定（不依赖当前工作目录），且会顺手修正
#    Windows 控制台编码。
from services.env_bootstrap import load_env  # noqa: E402

load_env()

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402

LOG_DIR = ROOT / "logs"

HOST = os.getenv("LOG_VIEWER_HOST", "127.0.0.1")
PORT = int(os.getenv("LOG_VIEWER_PORT", "8099"))
USERNAME = os.getenv("LOG_VIEWER_USER", "admin")
PASSWORD = os.getenv("LOG_VIEWER_PASSWORD", "logviewer")
MAX_LINES = int(os.getenv("LOG_VIEWER_MAX_LINES", "3000"))
POLL_INTERVAL = float(os.getenv("LOG_VIEWER_POLL", "1"))
PING_INTERVAL = 15.0

# 口令来源（启动时打印，便于确认到底读到了哪份配置）
PASSWORD_FROM_ENV_FILE = bool(os.getenv("LOG_VIEWER_PASSWORD", "").strip())
DEFAULT_PASSWORD = "logviewer"

app = FastAPI(title="后端日志查看器", docs_url=None, redoc_url=None, openapi_url=None)


def default_log_file() -> pathlib.Path:
    raw = os.getenv("BACKEND_LOG_FILE", "").strip()
    return pathlib.Path(raw) if raw else (LOG_DIR / "backend.log")


def resolve_log_file(name: Optional[str]) -> pathlib.Path:
    """只允许访问 logs/ 目录下（或 BACKEND_LOG_FILE 指定）的文件，避免路径穿越。"""
    if not name:
        return default_log_file()
    candidate = (LOG_DIR / name).resolve()
    if LOG_DIR.resolve() not in candidate.parents:
        raise HTTPException(status_code=400, detail="非法文件名")
    return candidate


# ================================================================
# 固定口令鉴权（HTTP Basic）
# ================================================================
def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"detail": "需要登录：请用配置的用户名/口令访问"},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="backend-logs"'},
    )


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return _unauthorized()
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        user, _, pwd = raw.partition(":")
    except Exception:
        return _unauthorized()
    # 常量时间比较，避免时序侧信道
    ok_user = hmac.compare_digest(user, USERNAME)
    ok_pwd = hmac.compare_digest(pwd, PASSWORD)
    if not (ok_user and ok_pwd):
        return _unauthorized()
    return await call_next(request)


# ================================================================
# 读取日志
# ================================================================
def read_tail(path: pathlib.Path, max_lines: int) -> List[str]:
    if not path.exists() or max_lines <= 0:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 64 * 1024
            data = b""
            while size > 0 and data.count(b"\n") <= max_lines:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _event(event_type: str, data) -> str:
    return f"data: {json.dumps({'type': event_type, 'data': data}, ensure_ascii=False)}\n\n"


@app.get("/api/logs")
async def api_logs(file: Optional[str] = Query(None), tail: int = Query(500, ge=1, le=MAX_LINES)):
    path = resolve_log_file(file)
    lines = read_tail(path, tail)
    return {
        "file": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
        "lines": lines,
    }


@app.get("/api/logs/stream")
async def api_logs_stream(file: Optional[str] = Query(None), tail: int = Query(300, ge=0, le=MAX_LINES)):
    path = resolve_log_file(file)

    async def gen():
        for line in read_tail(path, tail):
            yield _event("line", {"text": line})
        try:
            offset = path.stat().st_size if path.exists() else 0
        except OSError:
            offset = 0
        pending = ""
        idle = 0.0
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                if not path.exists():
                    continue
                size = path.stat().st_size
                if size < offset:                 # 日志被截断/轮转
                    offset = 0
                    pending = ""
                if size > offset:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                        offset = fh.tell()
                    pending += chunk
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        yield _event("line", {"text": line})
                    idle = 0.0
                else:
                    idle += POLL_INTERVAL
                    if idle >= PING_INTERVAL:
                        idle = 0.0
                        yield ": ping\n\n"
            except OSError as exc:
                yield _event("error", {"message": f"读取日志失败: {exc}"})
                return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/files")
async def api_files():
    """logs/ 目录下的日志文件列表（方便切换查看）。"""
    LOG_DIR.mkdir(exist_ok=True)
    items = []
    for p in sorted(LOG_DIR.glob("*.log")):
        items.append({"name": p.name, "size": p.stat().st_size,
                      "mtime": p.stat().st_mtime})
    return {"dir": str(LOG_DIR), "files": items}


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>后端日志 · {{TITLE}}</title>
<style>
  body{margin:0;font-family:ui-monospace,Consolas,monospace;background:#0f172a;color:#e2e8f0}
  header{padding:10px 14px;background:#1e293b;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  header b{font-family:system-ui,sans-serif;font-size:14px}
  select,input,button{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:4px 8px;font-size:12px}
  button{cursor:pointer} button:hover{background:#1e293b}
  #log{padding:10px 14px;white-space:pre-wrap;word-break:break-all;font-size:12px;line-height:1.5}
  .err{color:#fca5a5}.warn{color:#fcd34d}.ok{color:#6ee7b7}.dim{color:#64748b}
  #status{font-family:system-ui,sans-serif;font-size:12px;color:#94a3b8}
</style></head><body>
<header>
  <b>🖥️ 后端日志</b>
  <select id="file"></select>
  <input id="filter" placeholder="过滤关键字，如 ERROR / task_id" style="width:240px"/>
  <label style="font-size:12px;color:#94a3b8"><input type="checkbox" id="follow" checked/> 自动滚动</label>
  <button id="pause">⏸ 暂停</button>
  <button id="reload">🔄 重载</button>
  <button id="clear">🧹 清屏</button>
  <span id="status">连接中…</span>
</header>
<div id="log"></div>
<script>
const logEl = document.getElementById('log');
const statusEl = document.getElementById('status');
const fileSel = document.getElementById('file');
const filterEl = document.getElementById('filter');
const followEl = document.getElementById('follow');
let lines = [], paused = false, es = null;

function classify(t){
  if (/ ERROR |Traceback|❌/.test(t)) return 'err';
  if (/ WARNING |⚠/.test(t)) return 'warn';
  if (/SSE|分析任务|启动/.test(t)) return 'ok';
  return '';
}
function render(){
  const kw = filterEl.value.trim();
  const shown = kw ? lines.filter(l=>l.includes(kw)) : lines;
  logEl.innerHTML = shown.map(l=>`<div class="${classify(l)}">${l.replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</div>`).join('');
  if (followEl.checked) window.scrollTo(0, document.body.scrollHeight);
}
async function loadFiles(){
  const r = await fetch('/api/files'); const d = await r.json();
  fileSel.innerHTML = d.files.map(f=>`<option value="${f.name}">${f.name} (${(f.size/1024).toFixed(1)}KB)</option>`).join('');
}
async function connect(){
  if (es) es.close();
  lines = []; render();
  const file = fileSel.value;
  const snap = await fetch('/api/logs?tail=800' + (file?`&file=${encodeURIComponent(file)}`:''));
  const d = await snap.json();
  lines = d.lines || []; render();
  statusEl.textContent = `已加载 ${lines.length} 行 · ${d.file}`;
  es = new EventSource('/api/logs/stream?tail=0' + (file?`&file=${encodeURIComponent(file)}`:''));
  es.onmessage = (ev)=>{
    const m = JSON.parse(ev.data);
    if (m.type === 'line' && !paused){ lines.push(m.data.text); if (lines.length>3000) lines.splice(0,lines.length-3000); render(); }
    if (m.type === 'error'){ statusEl.textContent = m.data.message; }
  };
  es.onerror = ()=>{ statusEl.textContent = '连接断开，3 秒后重连…'; es.close(); setTimeout(connect, 3000); };
}
document.getElementById('pause').onclick = (e)=>{ paused=!paused; e.target.textContent = paused?'▶ 继续':'⏸ 暂停'; };
document.getElementById('reload').onclick = ()=>{ loadFiles().then(connect); };
document.getElementById('clear').onclick = ()=>{ lines=[]; render(); };
filterEl.oninput = render;
fileSel.onchange = connect;
loadFiles().then(connect);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE.replace("{{TITLE}}", os.path.basename(str(default_log_file())))


def main() -> None:
    import uvicorn

    from services.env_bootstrap import env_file_path

    env_file = env_file_path()
    if PASSWORD == DEFAULT_PASSWORD:
        print("⚠️  口令仍是默认值 logviewer：请在 .env 里设置 LOG_VIEWER_PASSWORD=<你自己的口令>", flush=True)
    print(f"🖥️  日志查看器：http://{HOST}:{PORT}", flush=True)
    print(f"    用户名：{USERNAME}   口令：**已设置**（来自 {'.env' if PASSWORD_FROM_ENV_FILE else '默认值'}）", flush=True)
    print(f"    配置文件：{env_file or '未找到 .env（可用 .env.example 复制一份）'}", flush=True)
    print(f"    日志文件：{default_log_file()}", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
