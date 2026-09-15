# 测试说明

## 目录约定

```
test/
├── conftest.py            # pytest 配置：把项目根加入 sys.path + 禁止收集 manual/
├── test_auth.py           # 单元测试：注册/登录/鉴权（用 FakeES 模拟 ES，不连真库）
├── test_chat_service.py   # 单元测试：聊天流式事件被正确消费为完整回复（mock LLM）
└── manual/                # ⚠️ 手动/联调脚本：会真的调用 LLM、小红书 MCP、数据库
    ├── manual_summary_flow.py   # 跑一遍"意图识别 → 帖子分析 → 最终总结"
    ├── manual_intent_notes.py   # 意图识别 + 小红书搜索 + 多 Agent 并发分析
    ├── manual_tool_calling.py   # function calling（get_location 工具）联调
    └── manual_mcp_search.py     # 小红书 MCP 搜索/详情接口联调
```

`manual/` 里的脚本**不是单元测试**：它们需要真实的 API Key、运行中的服务，有的甚至在
import 阶段就 `asyncio.run(...)`。所以：

- 根目录 `pytest.ini` 里 `norecursedirs = test/manual ...`
- `test/conftest.py` 里 `collect_ignore_glob = ["manual/*"]`

两层保护，`python -m pytest` 绝不会误跑它们。

## 跑单元测试

```bash
pip install -r requirements-dev.txt
python -m pytest              # 从项目根目录执行
python -m pytest -v test/test_auth.py          # 只跑某个文件
python -m unittest discover -s test -p "test_*.py"   # 不用 pytest 也行（两个文件都是 unittest 风格）
```

单测**不需要**任何外部服务：ES / LLM / MCP 全部用 mock 或 `dependency_overrides` 替换。

## 跑手动脚本（需要完整环境）

```bash
# 前置：PG / Qdrant / Redis 已启动，.env 配好 Key；MCP 相关脚本还需要
#       xiaohongshumcp/xiaohongshu-mcp-windows-amd64.exe 已登录
python -m test.manual.manual_summary_flow
python -m test.manual.manual_mcp_search
```

## 后续补充单测的建议优先级

1. `services/rag/validate.py`：JSON 格式修复与结构校验（纯函数，最容易测）
2. `services/rag/retrieval.py`：`resolve_visibility()` / `rrf_fuse()` / `finalize_ranking()`
   （纯函数，覆盖 own/public/own_or_public 的可见性分支）
3. `services/summary_task.py`：任务幂等、事件重放、客户端断开后继续（用 fake 生成器）
4. `services/chat_service.py`：三种模式（normal/history/public）的分流
