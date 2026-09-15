"""上传 GitHub 前的密钥 / 敏感信息自查
================================================================================

用法：
    python check_secrets.py            # 扫描 git 已跟踪的文件（最贴近真实上传内容）
    python check_secrets.py --all      # 忽略 git，扫描工作区（排除 .venv/node_modules 等）

检出内容：各类 API Key、数据库/ES 口令、JWT、疑似硬编码凭据。
发现命中时会以非 0 退出码结束，方便挂到 pre-commit / CI 上。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent

# 这些目录/后缀不进仓库，扫描时跳过
SKIP_PARTS = {".venv", "venv", "node_modules", "__pycache__", ".git", "logs",
              "xiaohongshumcp", "xiaohongshu-mcp", "dist", ".pytest_cache",
              ".ruff_cache", ".mypy_cache", ".npm-cache"}
# 跳过本文件自身：里面的 pattern 字符串会被自己匹配到（自检误报）
SKIP_FILES = {"check_secrets.py"}
TEXT_EXT = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".ts", ".vue", ".js",
            ".example", ".cfg", ".ini", ".toml", ".html", ".css", ".ps1", ".bat", ".sh"}

# 本机口令这类"只在本地有意义"的值，不写死在源码里（避免源码本身成了泄密点）
LOCAL_SECRETS = [s for s in (__import__("os").getenv("CHECK_SECRETS_EXTRA", "").split(",")) if s]

PATTERNS = [
    (r"sk-[A-Za-z0-9_\-]{20,}", "API Key（sk- 开头，OpenAI/DeepSeek 等）"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
    (r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", "GitHub Token"),
    (r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.", "疑似 JWT"),
    (r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
     "疑似硬编码凭据"),
] + [(re.escape(s), "本机自定义口令（CHECK_SECRETS_EXTRA）") for s in LOCAL_SECRETS]

ALLOW_HINTS = ("your_", "xxx", "change", "example", "placeholder", "<", "填入", "占位")


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8", timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return [line for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


def walk_files() -> list[str]:
    result = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_EXT or path.name in (".gitignore", ".gitattributes", ".env.example"):
            result.append(str(path.relative_to(ROOT)).replace("\\", "/"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="上传前敏感信息自查")
    parser.add_argument("--all", action="store_true", help="忽略 git，直接扫描工作区")
    args = parser.parse_args()

    files = walk_files() if args.all else (tracked_files() or walk_files())
    source = "工作区（--all）" if args.all else ("git 已跟踪文件" if tracked_files() else "工作区（未检测到 git）")
    print(f"扫描范围：{source}，共 {len(files)} 个文件\n")

    hits = []
    for rel in files:
        if pathlib.Path(rel).name in SKIP_FILES:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for pattern, label in PATTERNS:
                if re.search(pattern, line):
                    if any(h in low for h in ALLOW_HINTS):
                        continue                    # 占位值（your_xxx / change-this…）跳过
                    hits.append((rel, lineno, label, line.strip()[:120]))

    if hits:
        print("❌ 发现可疑内容，请先处理再上传：")
        for rel, lineno, label, line in hits:
            print(f"  [{label}] {rel}:{lineno}\n      {line}")
        return 1

    print("✅ 未发现密钥或凭据")
    print("\n提醒：确认 .env / logs/ / xiaohongshumcp/ 均未被跟踪（本脚本已跳过它们，"
          "但要确认 .gitignore 生效）：")
    print("  git ls-files | findstr /i \"env log cookies exe\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
