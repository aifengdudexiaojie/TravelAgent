"""pytest 全局配置（放在 test/ 下，自动被 pytest 发现）。

作用：
1. 把项目根目录加入 sys.path —— 单测里用的是 `from services... import ...` 这类
   绝对导入，不管从哪个目录调用 pytest 都能工作。
2. **禁止收集 test/manual/ 下的手动脚本**：那些脚本会真的调用 LLM / 小红书 MCP /
   数据库（有的甚至在 import 时就 asyncio.run 了），绝不能混进单元测试。
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 手动/联调脚本目录：pytest 不收集（配合根目录 pytest.ini 的 norecursedirs 双保险）
collect_ignore_glob = ["manual/*"]
