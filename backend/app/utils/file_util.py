# -*- coding: utf-8 -*-
"""
@File    : file_util.py
@Time    : 2026/5/22 14:15
@Author  : tanlei
"""
import os
from app.core.config import config

def create_dirs():
    os.makedirs(config.upload_dir, exist_ok=True)
    os.makedirs(config.vector_db_path, exist_ok=True)