import json

import redis


class RedisMemory:

    def __init__(self):
        self.redis = redis.Redis(host='localhost', port=6379, decode_responses=True, protocol=2)

    # ================================================================
    # key 结构：task:{task_id}:{location}:{note_id}
    # 支持逐级查询：task_id → location → note_id
    # ================================================================

    def _key(self, task_id: str, location: str, note_id: str) -> str:
        return f"task:{task_id}:{location}:{note_id}"

    def add_message(self, task_id, location, note_id, content):
        """
        存储一条消息。

        Args:
            task_id: 任务号
            location: 地点名称（如 成都）
            note_id: 笔记 ID
            content: 内容（任意可 JSON 序列化的对象）
        """
        key = self._key(task_id, location, note_id)
        self.redis.set(key, json.dumps(content, ensure_ascii=False))

    # ----------------------------------------------------------------
    # 三级查询：task_id + location + note_id → 单条
    # ----------------------------------------------------------------

    def get_message(self, task_id, location, note_id):
        """
        按 task_id + location + note_id 精确查询单条消息。
        """
        key = self._key(task_id, location, note_id)
        val = self.redis.get(key)
        return json.loads(val) if val else None

    # ----------------------------------------------------------------
    # 二级查询：task_id + location → 该地点下所有消息
    # ----------------------------------------------------------------

    def get_messages_by_location(self, task_id, location):
        """
        按 task_id + location 查询该地点下所有笔记消息。
        返回 {note_id: content, ...}
        """
        pattern = f"task:{task_id}:{location}:*"
        return self._scan_get(pattern, strip_prefix=f"task:{task_id}:{location}:")

    # ----------------------------------------------------------------
    # 一级查询：task_id → 该任务下所有消息
    # ----------------------------------------------------------------

    def get_messages_by_task(self, task_id):
        """
        按 task_id 查询该任务下所有消息。
        返回 {"{location}:{note_id}": content, ...}
        """
        pattern = f"task:{task_id}:*"
        return self._scan_get(pattern, strip_prefix=f"task:{task_id}:")

    def _scan_get(self, pattern: str, strip_prefix: str) -> dict:
        """扫描匹配 key，返回 {剩余部分: content}"""
        result = {}
        for key in self.redis.scan_iter(match=pattern):
            val = self.redis.get(key)
            if val is not None:
                remaining = key[len(strip_prefix):]  # location:note_id
                result[remaining] = json.loads(val)
        return result

    # ----------------------------------------------------------------
    # 删除
    # ----------------------------------------------------------------

    def remove_message(self, task_id, location=None, note_id=None):
        """
        删除消息，支持三种粒度：
        - remove_message(task_id)                    → 删除整个任务
        - remove_message(task_id, location)          → 删除某地点下所有
        - remove_message(task_id, location, note_id) → 删除单条
        """
        if note_id:
            self.redis.delete(self._key(task_id, location, note_id))
        elif location:
            pattern = f"task:{task_id}:{location}:*"
            for key in self.redis.scan_iter(match=pattern):
                self.redis.delete(key)
        else:
            pattern = f"task:{task_id}:*"
            for key in self.redis.scan_iter(match=pattern):
                self.redis.delete(key)
