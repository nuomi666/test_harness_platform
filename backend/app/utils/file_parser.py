# -*- coding: utf-8 -*-
"""
@File    : file_parser.py
@Time    : 2026/8/26 16:08
@Author  : tanlei
"""
# backend/app/utils/file_parser.py
import logging
from io import BytesIO
from typing import Union

logger = logging.getLogger("FILE_PARSER")


def _to_stream(file_data: Union[bytes, BytesIO]) -> BytesIO:
    """
    统一输入为 BytesIO，兼容 bytes / BytesIO / 类文件对象
    （历史调用方既有传 bytes 的，也有传 BytesIO 的，不兼容会导致PDF解析静默失败）
    """
    if isinstance(file_data, bytes):
        return BytesIO(file_data)
    if hasattr(file_data, "read"):
        try:
            file_data.seek(0)
        except Exception:
            pass
        return BytesIO(file_data.read())
    raise ValueError(f"不支持的文件数据类型: {type(file_data)}")


def _extract_pdf(stream: BytesIO) -> str:
    """PDF文本提取"""
    import fitz
    stream.seek(0)
    doc = fitz.open(stream=stream.read(), filetype="pdf")
    content = ""
    for page in doc:
        content += page.get_text()
    doc.close()
    return content


def _extract_docx(stream: BytesIO) -> str:
    """Word(.docx)文本提取：段落 + 表格"""
    try:
        import docx
    except ImportError:
        logger.warning("⚠️ python-docx未安装，无法解析docx，请确认 requirements.txt 已包含 python-docx")
        return ""

    stream.seek(0)
    document = docx.Document(stream)
    parts = []

    # 段落文本
    for para in document.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)

    # 表格文本（需求文档常以表格描述字段规则，不能丢）
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            line = " | ".join([c for c in cells if c])
            if line:
                parts.append(line)

    return "\n".join(parts)


def _extract_plain_text(stream: BytesIO) -> str:
    """纯文本提取（txt/md），自动尝试多种编码"""
    stream.seek(0)
    raw = stream.read()
    for encoding in ("utf-8", "gbk", "utf-16"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def _extract_image(stream: BytesIO, filename: str) -> str:
    """图片OCR文本提取"""
    from app.utils.image_ocr import extract_text_from_image_bytes
    stream.seek(0)
    text = extract_text_from_image_bytes(stream.read())
    if text:
        return f"【用户上传的页面截图，OCR识别内容】\n{text}"
    logger.warning(f"⚠️ 图片未识别到文字（可能未安装OCR依赖或图片无文字）| filename={filename}")
    return "【用户上传的页面截图，未能识别出文字内容，请结合需求描述理解】"


def extract_document_text(file_data: Union[bytes, BytesIO], filename: str) -> str:
    """
    多格式文档解析：支持 pdf / docx / txt / md / 图片OCR

    :param file_data: 文件字节流（bytes 或 BytesIO 均可）
    :param filename: 原始文件名，用来判断后缀
    :return: 提取出来的纯文本，解析失败返回空字符串，不阻断业务
    """
    lower_name = (filename or "").lower()
    content = ""

    try:
        stream = _to_stream(file_data)

        if lower_name.endswith(".pdf"):
            content = _extract_pdf(stream)
        elif lower_name.endswith(".docx"):
            content = _extract_docx(stream)
        elif lower_name.endswith(".doc"):
            # 旧版 .doc 为二进制格式，无法直接解析，明确提示用户转换
            logger.warning(f"⚠️ 旧版.doc格式无法解析，请转换为.docx后上传 | filename={filename}")
            content = "【旧版.doc格式无法自动解析，请转换为.docx或PDF后重新上传】"
        elif lower_name.endswith((".txt", ".md")):
            content = _extract_plain_text(stream)
        elif lower_name.split(".")[-1] in {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff"}:
            content = _extract_image(stream, filename)
        else:
            logger.warning(f"⚠️ 暂不支持的文件格式 | filename={filename}")
            return ""

    except Exception as e:
        # 解析异常返回空，不阻断生成，降级只用需求框文本
        logger.error(f"❌ 文档解析失败 | filename={filename} | 错误: {str(e)}")
        return ""

    content = (content or "").strip()
    if content:
        logger.info(f"✅ 文档解析成功 | filename={filename} | 提取文本长度={len(content)}字")
    else:
        logger.warning(f"⚠️ 文档解析结果为空 | filename={filename}")
    return content
