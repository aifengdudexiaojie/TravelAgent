"""分析任务的归属与鉴权单元测试（多用户越权防护）。

背景：这两个接口原来**没有鉴权**，且 task_id 是 `random.randint(0, 10000)`，
任何人猜到/枚举到 task_id 就能读到别人的分析进度和最终攻略。
现在：接口要求登录 + 任务记录归属用户 + 越权返回 403。
"""

import unittest
from unittest.mock import patch

from services import summary_task as st


class TaskOwnerTest(unittest.TestCase):
    def setUp(self):
        st._TASKS.clear()
        # 不要让测试真的去跑分析流程（那会调 LLM / 小红书 / 数据库）
        self._patcher = patch.object(st, "_run_analysis", lambda task: None)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        st._TASKS.clear()

    def test_start_is_idempotent_and_records_owner(self):
        t1 = st.start_summary_task(123456789, {"nickname": "A"}, owner_user_id="u1")
        t2 = st.start_summary_task(123456789, {"is_public": True}, owner_user_id="u1")
        self.assertIs(t1, t2)                       # 同一 task_id 只建一次
        self.assertEqual(t1.owner_user_id, "u1")
        self.assertTrue(t1.owner.get("is_public"))  # 后续调用会补齐字段

    def test_assert_task_owner_rules(self):
        task = st.start_summary_task(111, owner_user_id="u1")

        st.assert_task_owner(task, "u1")            # 本人：通过

        with self.assertRaises(PermissionError):    # 他人：拒绝
            st.assert_task_owner(task, "u2")
        with self.assertRaises(PermissionError):    # 匿名：拒绝
            st.assert_task_owner(task, None)

    def test_task_without_owner_is_open(self):
        """历史任务（改造前创建、没有归属）不做拦截，避免把老任务全锁死。"""
        task = st.start_summary_task(222)
        st.assert_task_owner(task, "anyone")

    def test_snapshot_respects_ownership(self):
        st.start_summary_task(333, owner_user_id="u1")

        self.assertIsNotNone(st.snapshot_task(333, user_id="u1"))
        with self.assertRaises(PermissionError):
            st.snapshot_task(333, user_id="u2")
        self.assertIsNone(st.snapshot_task(999, user_id="u1"))   # 不存在 → None（路由转 404）

    def test_task_id_entropy(self):
        """task_id 必须不可枚举，同时仍在 JS 安全整数范围内。"""
        from functions.get_intent import _new_task_id

        ids = {_new_task_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        self.assertTrue(all(v > 10_000_000 for v in ids))
        self.assertTrue(all(v < 2 ** 53 for v in ids))


class SummaryRouteAuthTest(unittest.TestCase):
    """路由层：未登录必须 401（用 FastAPI TestClient 验证依赖生效）。"""

    def test_endpoints_require_auth(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import controller.summary_routes as routes

        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)

        self.assertEqual(client.get("/api/summary/task/1").status_code, 401)
        self.assertEqual(client.post("/api/summary/cancel/1").status_code, 401)
        self.assertEqual(
            client.post("/api/summary/stream", json={"task_id": 1}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
