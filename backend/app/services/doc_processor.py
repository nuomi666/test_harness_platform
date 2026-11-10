# -*- coding: utf-8 -*-
"""
@File    : doc_processor.py
@Time    : 2026/5/22 14:13
@Author  : tanlei
"""
import os
import time
import logging
from app.core.vector_db import ChromaDBSingleton
from app.core.config import config
from app.utils.image_ocr import extract_text_from_image, is_image_file

logger = logging.getLogger("DOC_PROCESSOR")


def save_business_doc(file_path: str, filename: str, parsed_text: str = "") -> dict:
    """
    统一处理业务文档 + Web页面截图，存入向量库

    :param file_path: 文件落盘路径
    :param filename: 原始文件名
    :param parsed_text: 已解析好的文本内容（由调用方传入时不再重复解析，避免二次OCR）
    :return: 处理结果，其中 text 字段为提取出的纯文本
    """
    file_type = "image_screenshot" if is_image_file(filename) else "document"

    # 优先复用已解析文本（上传接口已通过 file_parser 统一解析，含OCR）
    if parsed_text and parsed_text.strip():
        full_text = parsed_text.strip()
        logger.debug(f"♻️ 复用已解析文本 | filename={filename} | 长度={len(full_text)}字")
    elif file_type == "image_screenshot":
        ocr_text = extract_text_from_image(file_path)
        if ocr_text:
            full_text = f"【Web页面截图识别内容】\n{ocr_text}"
        else:
            full_text = "【Web页面截图】OCR未识别到有效文字"
    else:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                full_text = f.read()
        except Exception:
            full_text = f"【文档】{filename} 内容无法直接读取"

    # 修复点：先获取单例实例，调用 add_business_docs 方法
    docs = [full_text]
    metas = [{
        "type": "business",
        "file_name": filename,
        "file_type": file_type
    }]
    # id 增加时间戳，避免同名文件重复上传时主键冲突
    ids = [f"biz_doc_{int(time.time())}_{os.path.basename(file_path)}"]

    ChromaDBSingleton.get_instance().add_business_docs(docs, metas, ids)
    logger.info(f"💾 业务文档入库完成 | filename={filename} | 类型={file_type} | 文本长度={len(full_text)}字")

    return {
        "code": 0,
        "msg": f"文件上传解析成功，类型:{file_type}",
        "filename": filename,
        "text": full_text
    }
