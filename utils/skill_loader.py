"""
Skill 文件加载工具

将 skills/ 目录下的 .md 文件内容解析为 system prompt。
自动去除 YAML frontmatter，只保留纯指令正文。
"""

import os
import re
from pathlib import Path


# skills 目录相对于此文件的路径
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def load_skill(skill_path: str | Path) -> str:
    """
    读取 skill 文件并返回可直接作为 system prompt 的内容。

    Args:
        skill_path: skill 文件的路径。
                    支持三种方式：
                    - 绝对路径（以 / 或盘符开头）
                    - 相对路径（相对于当前工作目录）
                    - 文件名（自动在 skills/ 目录下查找）

    Returns:
        去除 YAML frontmatter 后纯文本内容（首尾空白已清理）。

    Raises:
        FileNotFoundError: 文件不存在或 skills/ 下找不到。
        ValueError:      文件内容为空（去除 frontmatter 后无正文）。
    """
    path = _resolve_path(skill_path)
    raw = path.read_text(encoding="utf-8")
    body = _strip_frontmatter(raw)

    if not body.strip():
        raise ValueError(
            f"Skill 文件 '{skill_path}' 去除 frontmatter 后内容为空，"
            f"无法作为 system prompt 使用"
        )

    return body.strip()


def _resolve_path(skill_path: str | Path) -> Path:
    """将各种形式的 skill_path 统一解析为绝对 Path。"""
    p = Path(skill_path)

    # 1) 已经是绝对路径 → 直接使用
    if p.is_absolute():
        if p.exists():
            return p
        raise FileNotFoundError(f"Skill 文件不存在（绝对路径）: {p}")

    # 2) 相对路径且文件存在 → 相对 cwd
    if p.exists():
        return p.resolve()

    # 3) 带子目录路径（如 "tipsAnalysis/travel-post-summary.md"）→ 在 skills/ 下按路径查找
    for ext in ("", ".md"):
        candidate = SKILLS_DIR / (skill_path if ext == "" else skill_path + ext)
        if candidate.exists():
            return candidate

    # 4) 仅有文件名（如 "Intent.md"）→ 在 skills/ 下直接查找
    name = p.name if p.suffix else p.name + ".md"
    candidate = SKILLS_DIR / name
    if candidate.exists():
        return candidate

    # 5) 在 skills/ 子目录中递归查找
    for f in SKILLS_DIR.rglob(name):
        return f

    raise FileNotFoundError(
        f"Skill 文件 '{skill_path}' 未找到。\n"
        f"  已搜索路径:\n"
        f"    - 当前工作目录\n"
        f"    - {SKILLS_DIR}/\n"
        f"    - {SKILLS_DIR}/**/*.md"
    )


_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    """
    移除 YAML frontmatter（前后各一行 ---）。
    如果没有 frontmatter，原样返回。
    """
    return _FRONTMATTER_RE.sub("", text, count=1)
