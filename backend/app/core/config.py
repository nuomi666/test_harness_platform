# -*- coding: utf-8 -*-
"""
@File    : config.py
@Time    : 2026/5/22 14:12
@Author  : tanlei
"""
import os
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class HarnessConfig(BaseModel):
    # 环境变量
    env: str = os.getenv("ENV", "dev")
    port: int = int(os.getenv("SERVICE_PORT", 8000))
    llm_api_key: str = os.getenv("DASHSCOPE_API_KEY")
    llm_model: str = os.getenv("LLM_MODEL", "qwen-plus")
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", 0.3))

    # 路径配置
    vector_db_path: str = os.getenv("VECTOR_DB_PATH", "./chroma_db")
    upload_dir: str = os.getenv("UPLOAD_DIR", "./temp_documents")
    plugin_dir: str = os.path.join(os.path.dirname(__file__), "../test_skills")  # 兼容保留

    # 技能知识库目录（SKILL.md + references）
    # 显式指向 app/skills：原先由 plugin_dir 反推父目录再拼 skills，
    # 路径推导依赖目录层级巧合，改为绝对路径后从任意工作目录启动均可用
    skills_dir: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills"
    )

    # 加载Harness骨架配置
    @staticmethod
    def load_harness_config():
        with open("harness_config.yml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)


# 全局单例
config = HarnessConfig()
harness_core_config = config.load_harness_config()