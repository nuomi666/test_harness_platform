# -*- coding: utf-8 -*-
"""
@File    : logging_config.py
@Time    : 2026/5/26 15:53
@Author  : tanlei
"""
import logging
from logging.config import dictConfig

def setup_logging():
    """配置全局日志，输出到控制台，支持DEBUG级别"""
    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
            },
            "detailed": {
                "format": "[%(asctime)s] %(levelname)s in %(module)s:%(lineno)d: %(message)s",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "detailed",
                "level": "DEBUG",
            }
        },
        "loggers": {
            "": {  # 根日志器
                "handlers": ["console"],
                "level": "INFO",
                "propagate": True,
            },
            # 各业务模块显式声明，便于按需单独调整级别
            # 排查问题时可将对应模块或根日志器 level 改为 DEBUG
            "MAIN": {"level": "INFO", "propagate": True},
            "FLOW_SCHEDULER": {"level": "INFO", "propagate": True},
            "SKILL_REQ_REVIEW": {"level": "INFO", "propagate": True},
            "SKILL_REQ_TEST_CASES": {"level": "INFO", "propagate": True},
            "PLUGIN_LOADER": {"level": "INFO", "propagate": True},
            "VECTOR_DB": {"level": "INFO", "propagate": True},
            "CONTEXT_MANAGER": {"level": "INFO", "propagate": True},
            "FILE_OUTPUT_MANAGER": {"level": "INFO", "propagate": True},
            "FILE_EXPORTER": {"level": "INFO", "propagate": True},
            "FILE_PARSER": {"level": "INFO", "propagate": True},
            "DOC_PROCESSOR": {"level": "INFO", "propagate": True},
            "METADATA_MANAGER": {"level": "INFO", "propagate": True},
            "IMAGE_OCR": {"level": "INFO", "propagate": True},
            "llm_adapter": {
                "level": "DEBUG",
                "propagate": True,
            },
            "uvicorn": {
                "level": "INFO",
            }
        }
    })