"""
小红书 MCP 服务启动脚本（共享单实例模式）

⚠️ 注意：**多用户模式（XHS_MULTI_USER=true，默认）不需要跑这个脚本** ——
   应用会在用户点「登录小红书」时按 user_id 各自启动独立实例（见
   services/xhs_manager.py 与 docs/xhs-multi-user.md）。

本脚本用于"共享一个账号"的本地/演示场景：启动一个默认实例（:18060）。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

# ⚠️ 真实目录名是 xiaohongshumcp（没有连字符）——原来的 xiaohongshu-mcp 是错的，
#    导致这个脚本永远找不到可执行文件。
MCP_DIR = Path(__file__).parent / "xiaohongshumcp"
MCP_LOG = Path(__file__).parent / "logs" / "mcp_server.log"


def find_exe() -> Path | None:
    """查找 xiaohongshu-mcp 可执行文件"""
    candidates = [
        MCP_DIR / "xiaohongshu-mcp-windows-amd64.exe",
        MCP_DIR / "xiaohongshu-mcp",
        MCP_DIR / "xiaohongshu-mcp-darwin-amd64",
        MCP_DIR / "xiaohongshu-mcp-darwin-arm64",
        MCP_DIR / "xiaohongshu-mcp-linux-amd64",
    ]
    for exe in candidates:
        if exe.exists() and os.access(exe, os.X_OK):
            return exe
    # Windows 下 .exe 可能没有可执行权限标记
    if (MCP_DIR / "xiaohongshu-mcp-windows-amd64.exe").exists():
        return MCP_DIR / "xiaohongshu-mcp-windows-amd64.exe"
    return None


def start_mcp() -> subprocess.Popen | None:
    """启动 xiaohongshu-mcp 服务，返回进程对象"""
    exe = find_exe()
    if not exe:
        print(f"❌ 未找到 xiaohongshu-mcp 可执行文件（查找目录：{MCP_DIR}）")
        print("请从 https://github.com/xpzouying/xiaohongshu-mcp/releases 下载")
        print(f"并解压到 {MCP_DIR} 目录")
        return None

    MCP_LOG.parent.mkdir(parents=True, exist_ok=True)
    print(f"🚀 启动 xiaohongshu-mcp: {exe}")
    log_fp = open(MCP_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(exe)],
        stdout=log_fp,
        stderr=log_fp,
        cwd=str(MCP_DIR),
    )

    # 等待服务启动
    for i in range(15):
        time.sleep(0.5)
        if proc.poll() is not None:
            print(f"❌ MCP 服务异常退出 (exit code={proc.returncode})")
            with open(MCP_LOG, encoding="utf-8") as f:
                print(f.read()[-500:])
            return None

        try:
            import httpx
            resp = httpx.get("http://localhost:18060/mcp", timeout=2)
            if resp.status_code < 500:
                print(f"✅ MCP 服务已启动 (PID={proc.pid})")
                return proc
        except Exception:
            continue

    print("⚠️  MCP 服务可能尚未就绪，请检查日志:", MCP_LOG)
    return proc


def stop_mcp(proc: subprocess.Popen | None):
    """停止 MCP 服务"""
    if proc and proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=5)
        print("🛑 MCP 服务已停止")


if __name__ == "__main__":
    proc = start_mcp()
    if proc:
        try:
            print("按 Ctrl+C 停止服务...")
            proc.wait()
        except KeyboardInterrupt:
            stop_mcp(proc)
            sys.exit(0)
