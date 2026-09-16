"""推送前仓库完整性自查（防止"本地能跑、云端 clone 下来缺文件"）
================================================================================

为什么需要它
------------
`.gitignore` 里一旦有**没有锚定到仓库根**的目录名规则，就会误伤嵌套的同名源码目录。
真实踩过的坑：Python 打包产物规则 `lib/` 把 `vue/src/lib/` 整个忽略了，
于是 `vue/src/lib/api.ts` 从未进仓库 —— 本地构建正常，云端 `npm run build` 直接报
`Cannot find module '@/lib/api'`（TS2307 一片）。

本脚本做两件事：
  1. **源码目录逐文件比对**：`services/ controller/ vue/src/ …` 下本地存在的文件，
     必须都在 `git ls-files` 里（自动跳过 __pycache__ / node_modules / dist 等产物）；
  2. **根目录关键文件清单**：Dockerfile、.env.example、pytest.ini、check_*.py 之类
     必须在仓库里（`.env`、日志、cookies 等敏感/产物文件则必须**不**在）。

用法：
    python check_repo.py          # 自查，有问题会以非 0 退出码结束
    # 建议在 git push 前跑一次；也可以挂到 pre-commit / CI
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

# Windows 控制台默认 GBK，打印 ✅/⚠️ 会 UnicodeEncodeError（看起来像脚本失败，其实是编码）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent

# 逐文件比对的源码目录（本地文件都应被跟踪）
# 注意 vue 用整个目录：除了 vue/src，index.html / tsconfig.json / vite.config.ts /
# package-lock.json 少了任何一个云端都构建不起来；node_modules 与 dist 由
# ARTIFACT_PARTS 自动跳过。
SOURCE_DIRS = [
    "auth", "agents", "config", "controller", "functions", "models",
    "services", "skills", "utils", "test", "docs", "deploy", "vue",
]

# 产物/依赖目录：出现在路径里就跳过
ARTIFACT_PARTS = {"__pycache__", "node_modules", ".venv", "venv", "dist", "build",
                  ".pytest_cache", ".ruff_cache", ".mypy_cache", ".npm-cache"}
ARTIFACT_SUFFIX = {".pyc", ".pyo", ".pyd", ".log", ".tmp", ".bak", ".orig", ".rej"}

# 根目录必须被跟踪的文件
REQUIRED_ROOT = [
    "main.py", "run_backend.py", "log_viewer.py", "check_secrets.py", "check_repo.py",
    "requirements.txt", "requirements-dev.txt", "pytest.ini",
    ".gitignore", ".gitattributes", ".dockerignore", ".env.example",
    "docker-compose.yml", "Dockerfile", "README.md",
]

# 根目录必须**不**被跟踪的文件（敏感数据 / 运行产物）
FORBIDDEN_ROOT = [".env", "mcp_server.log"]


def tracked_files() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    if out.returncode != 0:
        print("❌ 当前目录不是 git 仓库（或 git 不可用）")
        sys.exit(2)
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def is_artifact(path: pathlib.Path) -> bool:
    if any(part in ARTIFACT_PARTS for part in path.parts):
        return True
    if path.suffix.lower() in ARTIFACT_SUFFIX:
        return True
    return path.name in {"Thumbs.db", "desktop.ini", ".DS_Store"}


def main() -> int:
    tracked = tracked_files()
    problems: list[str] = []

    # ---------- 1) 源码目录逐文件比对 ----------
    missing_sources: list[pathlib.Path] = []
    for rel_dir in SOURCE_DIRS:
        base = ROOT / rel_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or is_artifact(path):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel not in tracked:
                missing_sources.append(path.relative_to(ROOT))

    if missing_sources:
        problems.append(f"{len(missing_sources)} 个源码文件没有进仓库（本地有、git 里没有）")
        for rel in missing_sources[:20]:
            rel_posix = rel.as_posix()          # 统一用 / 输出，和 git 的路径写法一致
            hint = subprocess.run(["git", "check-ignore", "-v", rel_posix], cwd=ROOT,
                                  capture_output=True, text=True, encoding="utf-8")
            reason = (hint.stdout or "").strip() or "（未被 .gitignore 命中 → 可能只是没 git add）"
            problems.append(f"    · {rel_posix}\n        原因：{reason}")
        if len(missing_sources) > 20:
            problems.append(f"    … 其余 {len(missing_sources) - 20} 个省略")

    # ---------- 2) 根目录清单 ----------
    for name in REQUIRED_ROOT:
        if name not in tracked:
            exists = "本地存在" if (ROOT / name).exists() else "本地也不存在"
            problems.append(f"缺少必需文件：{name}（{exists}）")

    for name in FORBIDDEN_ROOT:
        if name in tracked:
            problems.append(f"⚠️ 敏感/产物文件被跟踪了：{name} —— 请 git rm --cached 并从 .gitignore 排除")

    # ---------- 汇总 ----------
    print(f"仓库文件 {len(tracked)} 个；逐文件校验了 {len(SOURCE_DIRS)} 个源码目录")
    if not problems:
        print("✅ 仓库完整：源码齐全、敏感文件未入库")
        print("   （推送后建议在服务器上再确认一次：git ls-files vue/src/lib）")
        return 0

    print("❌ 发现问题：")
    for line in problems:
        print(f"  {line}")
    print("\n排查建议：")
    print("  1) git check-ignore -v <文件>   看是哪条 .gitignore 规则命中的")
    print("  2) 目录名类规则一定要写成 /xxx/（锚定仓库根），别写 xxx/")
    print("  3) 改完 .gitignore 后 git add -A，再跑一次本脚本")
    return 1


if __name__ == "__main__":
    sys.exit(main())
