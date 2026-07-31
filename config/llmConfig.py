from functools import lru_cache

from dotenv import load_dotenv
import os

load_dotenv()
# print("DEEPSEEK_API_KEY =", os.getenv("DEEPSEEK_API_KEY"))
# print("SEARCH_PROVIDER =", os.getenv("SEARCH_PROVIDER"))
# print(os.getcwd())

class ModelSettings:

    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL")

    KIMI_API_KEY = os.getenv("KIMI_API_KEY")
    KIMI_BASE_URL = os.getenv("KIMI_BASE_URL")


    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL")
    DEEPSEEK_PROVIDER = os.getenv("DEEPSEEK_PROVIDER")

    KIMI_MODEL = os.getenv("KIMI_MODEL")
    KIMI_PROVIDER = os.getenv("KIMI_PROVIDER")

    # MongoDB 配置
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "self_auto")

    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    GAODE_API_KEY = os.getenv("GAODE_API_KEY")

@lru_cache()
def get_settings() -> ModelSettings:
    """获取配置单例"""
    return ModelSettings()