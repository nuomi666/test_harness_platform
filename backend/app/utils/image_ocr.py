# -*- coding: utf-8 -*-
"""
@File    : image_ocr.py
@Time    : 2026/7/9 11:49
@Author  : tanlei
"""
import os
import logging
from io import BytesIO
from typing import Optional

logger = logging.getLogger("IMAGE_OCR")

try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    logger.info("ℹ️ OCR依赖未安装（pytesseract/Pillow），图片识别能力不可用，不影响其他流程")

# Windows用户如果没配环境变量，可取消下面注释并修改为你的安装路径
# if OCR_AVAILABLE and os.name == 'nt':
#     pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# 支持OCR识别的图片后缀
IMAGE_SUFFIXES = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff"}


def _ocr_image(img) -> Optional[str]:
    """对已打开的PIL图像执行OCR"""
    try:
        # 简体中文+英文识别
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        # 清洗空行
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines) if lines else None
    except Exception as e:
        logger.warning(f"⚠️ OCR识别失败: {str(e)}")
        return None


def extract_text_from_image(img_path: str) -> Optional[str]:
    """
    从图片文件中提取文字（Web页面截图、弹窗、报错提示）
    未安装OCR依赖时返回None，不影响主流程
    """
    if not OCR_AVAILABLE:
        return None
    if not os.path.exists(img_path):
        logger.warning(f"⚠️ 图片文件不存在 | path={img_path}")
        return None
    try:
        img = Image.open(img_path)
        return _ocr_image(img)
    except Exception as e:
        logger.warning(f"⚠️ 图片打开失败: {str(e)}")
        return None


def extract_text_from_image_bytes(data: bytes) -> Optional[str]:
    """
    从图片字节流中提取文字（供上传接口直接使用，无需先落盘）
    未安装OCR依赖时返回None，不影响主流程
    """
    if not OCR_AVAILABLE:
        logger.info("ℹ️ OCR不可用，跳过图片内容识别")
        return None
    if not data:
        return None
    try:
        img = Image.open(BytesIO(data))
        logger.info(f"🖼️ 开始OCR识别图片 | 大小={len(data)}字节 | 格式={img.format}")
        result = _ocr_image(img)
        if result:
            logger.info(f"✅ OCR识别完成 | 识别文字长度={len(result)}字")
        else:
            logger.warning("⚠️ OCR未识别到有效文字")
        return result
    except Exception as e:
        logger.warning(f"⚠️ 图片字节流OCR失败: {str(e)}")
        return None

def is_image_file(filename: str) -> bool:
    """判断文件是否为图片格式"""
    if not filename or "." not in filename:
        return False
    suffix = filename.lower().split(".")[-1]
    return suffix in IMAGE_SUFFIXES
