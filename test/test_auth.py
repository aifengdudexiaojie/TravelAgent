"""认证模块测试：用内存用户表模拟 ES，覆盖注册/登录/鉴权流程。"""

import unittest

from elastic_transport import ConnectionError
from elasticsearch.exceptions import NotFoundError
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
import controller.auth_routes as auth_routes
import controller.user_routes as user_routes


class FakeES:
    """最小可用的用户表模拟。"""

    def __init__(self):
        self.users = {}

    def index(self, index, id, document, refresh="false"):
        self.users[id] = document
        return {"_id": id}

    def search(self, index, body=None):
        size = (body or {}).get("size", 10)
        username = (body or {}).get("query", {}).get("term", {}).get("username")
        hits = [
            {"_id": user_id, "_source": doc}
            for user_id, doc in self.users.items()
            if doc.get("username") == username
        ]
        return {
            "hits": {
                "hits": hits[:size],
                "total": {"value": len(hits)},
            }
        }

    def get(self, index, id):
        if id not in self.users:
            raise NotFoundError("missing", "404", None)
        return {"_source": self.users[id]}


class AuthFlowTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeES()

        def fake_create_user(user_id, username, nickname, password_hash):
            doc = {
                "user_id": user_id,
                "username": username,
                "nickname": nickname,
                "password_hash": password_hash,
                "created_at": "2026-08-26T00:00:00+00:00",
            }
            self.fake.users[user_id] = doc
            return doc

        def fake_get_user_by_username(username):
            for doc in self.fake.users.values():
                if doc["username"] == username:
                    return doc
            return None

        def fake_get_user_by_id(user_id):
            return self.fake.users.get(user_id)

        auth_routes.create_user = fake_create_user
        auth_routes.get_user_by_username = fake_get_user_by_username
        auth.get_user_by_id = fake_get_user_by_id
        user_routes.get_user_by_id = fake_get_user_by_id
        user_routes.list_user_guides = lambda user_id, page=1, page_size=20: {
            "items": [],
            "total": 0,
        }

        app = FastAPI()
        app.include_router(auth_routes.router)
        app.include_router(user_routes.router)
        self.client = TestClient(app)

    def test_register_login_profile(self):
        resp = self.client.post("/api/auth/register", json={
            "username": "alice",
            "password": "test123456",
            "nickname": "小爱",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user"]["username"], "alice")
        self.assertEqual(body["user"]["nickname"], "小爱")

        resp = self.client.post("/api/auth/login", json={
            "username": "alice",
            "password": "test123456",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["nickname"], "小爱")

        resp = self.client.get(
            "/api/user/profile",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["nickname"], "小爱")

    def test_duplicate_register_rejected(self):
        payload = {"username": "alice", "password": "test123456"}
        self.assertEqual(self.client.post("/api/auth/register", json=payload).status_code, 200)
        resp = self.client.post("/api/auth/register", json=payload)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "用户名已存在")

    def test_wrong_password_rejected(self):
        self.client.post("/api/auth/register", json={
            "username": "alice",
            "password": "test123456",
        })
        resp = self.client.post("/api/auth/login", json={
            "username": "alice",
            "password": "wrong-password",
        })
        self.assertEqual(resp.status_code, 401)

    def test_es_unavailable_returns_503(self):
        def raise_connection_error(username):
            raise ConnectionError("es down")

        auth_routes.get_user_by_username = raise_connection_error
        resp = self.client.post("/api/auth/login", json={
            "username": "alice",
            "password": "test123456",
        })
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "数据库连接失败，请稍后重试")

    def test_password_hash_roundtrip(self):
        hashed = auth.hash_password("test123456")
        self.assertTrue(auth.verify_password("test123456", hashed))
        self.assertFalse(auth.verify_password("wrong", hashed))


if __name__ == "__main__":
    unittest.main()
