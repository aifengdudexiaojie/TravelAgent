import json

import redis

class RedisMemory:

    def __init__(self):
        self.redis = redis.Redis(host='localhost', port=6379, decode_responses=True, protocol=2)

    def _key(self, task_id:str):
        return f"task:{task_id}"

    def add_message(self, task_id, note_id, content):

        key = f"task:{task_id}"

        self.redis.hset(
            key,
            note_id,
            json.dumps(content, ensure_ascii=False)
        )

        # message = {
        #     "task_id": task_id,
        #     "note_id": note_id,
        #     "content": content
        # }
        # self.redis.rpush(self._key(task_id), json.dumps(message, ensure_ascii=False))

    def get_message(self, task_id, note_id=None):
        if not note_id:
            messages = self.redis.hvals(f"task:{task_id}")
        else:
            messages = self.redis.hget(f"task:{task_id}", note_id)
        return json.loads(messages)

    def remove_message(self, task_id, note_id):
        if not note_id:
            self.redis.delete(self._key(task_id))
        else:
            self.redis.delete(f"task:{task_id}")
