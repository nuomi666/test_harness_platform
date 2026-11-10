# -*- coding: utf-8 -*-
"""
@File    : file_exporter.py
@Time    : 2026/5/26 17:52
@Author  : tanlei
"""
import io
import re
import logging
import unicodedata
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side

from app.utils.md_table import split_md_row

logger = logging.getLogger("FILE_EXPORTER")

# Excel 公式注入防护：以这些字符开头的字符串会被 openpyxl 当作公式（data_type='f'）写入，
# 导出后由 Excel/WPS 执行（若再被 CSV 化则构成 CSV 注入）。
# 用例的「步骤描述 / 预期结果」等列以这些符号开头时（如 `=SUM(A1)`、`-1`、`@名称`），
# 统一前置单引号强制为文本 —— Excel 打开时不会显示该引号。
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# 用例表格默认表头（与 SKILL.md 输出规范口径一致，9列）
# 说明：实际导出优先采用模型输出的真实表头，此处仅在解析不到表格时兜底
CASE_HEADERS = [
    "用例名称", "模块", "标签", "前置条件", "步骤描述",
    "预期结果", "编辑模式", "备注", "用例等级"
]


def _display_width(text: str) -> int:
    """计算字符串显示宽度：中文/全角字符计2，英文数字计1（用于Excel列宽自适应）"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _split_row(line: str):
    """
    拆分单行Markdown表格为单元格列表。

    走统一的 `split_md_row`：支持 `\\|` 转义 —— 单元格内含竖线（如「使用中|已弃用」、
    XPath 的 `//a|//b`）时不会被当成列分隔符，避免后续列整体右移。
    """
    return split_md_row(line)


def _is_separator(cells) -> bool:
    """判断是否为分隔行（如 |---|---|）"""
    if not cells:
        return False
    return all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c)


def parse_markdown_table(md_content: str):
    """
    解析Markdown用例表格，返回 (表头, 行数据)

    改进点：
    1. 动态识别表头：以模型实际输出的列名与列数为准，
       不再硬编码为固定列数（避免9列用例被截断成8列、丢失「标签/编辑模式/备注/用例等级」）
    2. 支持多表格合并：模型按「新增/修改/删除」等分块输出时，全部收集而非只取第一个表
    3. 自动跳过重复表头行与分隔行
    """
    lines = [line.strip() for line in (md_content or "").splitlines() if line.strip()]

    # 1. 切分出所有表格块（表头 + 分隔行 + 至少1行数据）
    tables = []
    current = []
    for line in lines:
        if line.startswith("|"):
            current.append(line)
        else:
            if len(current) >= 3:
                tables.append(current)
            current = []
    if len(current) >= 3:
        tables.append(current)

    if not tables:
        logger.warning("⚠️ 未在内容中解析到Markdown表格，导出将使用兜底表头且无数据行")
        return CASE_HEADERS, []

    # 2. 以第一个表格的首行作为表头
    headers = _split_row(tables[0][0])
    logger.info(f"📊 识别到用例表格 | 列数={len(headers)} | 表头={headers}")

    # 3. 收集所有表格的数据行
    #    仅合并表头与基准表头一致的表格：
    #    - 支持模型按「新增/修改/删除」分块输出的多个用例表
    #    - 自动排除自检表、说明表等非用例表格，避免脏数据混入导出文件
    rows = []
    skipped_tables = 0
    for table in tables:
        table_headers = _split_row(table[0])
        if table_headers != headers:
            skipped_tables += 1
            logger.debug(f"⏭️ 跳过非用例表格（表头不一致）| 表头={table_headers}")
            continue

        for row_line in table[1:]:
            cells = _split_row(row_line)
            if not cells or _is_separator(cells):
                continue
            # 跳过重复表头行（模型在每个分块重复输出表头时会写入该行）
            if cells == headers:
                continue
            # 列数对齐：不足补空，超出截断
            if len(cells) < len(headers):
                cells = cells + [""] * (len(headers) - len(cells))
            else:
                cells = cells[:len(headers)]
            rows.append(cells)

    if skipped_tables:
        logger.info(f"ℹ️ 已跳过 {skipped_tables} 个非用例表格（如自检表/说明表）")

    logger.info(f"📊 表格解析完成 | 解析到 {len(rows)} 行用例数据")
    return headers, rows


def generate_excel_bytes(md_content: str) -> bytes:
    """将Markdown用例转换为标准Excel二进制流"""
    headers, rows = parse_markdown_table(md_content)

    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"

    # 表头样式
    header_font = Font(bold=True)
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    # 写入表头
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border

    # 写入数据行
    for row_idx, row_data in enumerate(rows, 2):
        for col_idx, value in enumerate(row_data, 1):
            if isinstance(value, str):
                # 将 Markdown 的 <br> 换行标签转为 Excel 单元格内真实换行符（配合 wrap_text 实现真正换行）
                value = re.sub(r"<br\s*/?>", "\n", value)
                # 防公式注入：= + - @ 开头的字符串会被 openpyxl 当公式写入，前置单引号强制为文本
                if value.startswith(_FORMULA_PREFIXES):
                    value = "'" + value
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = thin_border

    # 自动调整列宽（中文按双字节宽度计算，避免中文字段被截断）
    for col in ws.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                max_length = max(max_length, _display_width(str(cell.value)))
            except Exception:
                pass
        adjusted_width = min(max(max_length + 2, 8), 50)
        ws.column_dimensions[column].width = adjusted_width

    # 首行冻结，便于长用例滚动查看
    ws.freeze_panes = "A2"

    # 输出为二进制流
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    logger.info(f"📗 Excel生成完成 | 列数={len(headers)} | 用例行数={len(rows)}")
    return output.read()


def generate_word_bytes(md_content: str) -> bytes:
    """
    将Markdown用例转换为标准Word文档(.docx)二进制流
    表格内容渲染为Word表格，无表格时按段落输出
    """
    try:
        import docx
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        logger.error("❌ python-docx未安装，无法生成Word，请执行 pip install python-docx")
        raise Exception("Word导出依赖 python-docx，请先安装")

    headers, rows = parse_markdown_table(md_content)

    document = docx.Document()

    # 中文字体设置，避免Word中显示为默认等宽字体
    try:
        style = document.styles["Normal"]
        style.font.name = "微软雅黑"
        style.font.size = Pt(10)
    except Exception:
        pass

    document.add_heading("测试用例", level=1)

    if rows:
        table = document.add_table(rows=1, cols=len(headers))
        try:
            table.style = "Table Grid"   # 带边框样式
        except Exception:
            logger.warning("⚠️ 未找到 Table Grid 样式，使用默认表格样式")

        # 表头：加粗居中
        hdr_cells = table.rows[0].cells
        for i, header in enumerate(headers):
            hdr_cells[i].text = ""
            paragraph = hdr_cells[i].paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = paragraph.add_run(header)
            run.bold = True
            run.font.size = Pt(10)

        # 数据行
        for row_data in rows:
            cells = table.add_row().cells
            for i, value in enumerate(row_data):
                cells[i].text = ""
                paragraph = cells[i].paragraphs[0]
                run = paragraph.add_run(str(value) if value is not None else "")
                run.font.size = Pt(9)

        logger.info(f"📝 Word文档生成完成 | 列数={len(headers)} | 用例行数={len(rows)}")
    else:
        # 无表格场景（如评审报告），按段落原样输出
        for line in (md_content or "").splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith("#"):
                document.add_heading(text.lstrip("#").strip(), level=2)
            else:
                document.add_paragraph(re.sub(r'^[-*]\s*', '', text))
        logger.info("📝 Word文档生成完成（无表格，按段落输出）")

    output = io.BytesIO()
    document.save(output)
    output.seek(0)
    return output.read()


def export_to_file(content: str, fmt: str) -> bytes:
    """统一导出入口，支持多种格式"""
    if fmt == "excel":
        return generate_excel_bytes(content)
    elif fmt == "markdown":
        return content.encode("utf-8")
    elif fmt == "txt":
        # 纯文本：去掉Markdown格式符号
        plain_text = re.sub(r'[#|*`\-]', '', content)
        return plain_text.encode("utf-8")
    elif fmt == "word":
        # 生成真正的 .docx 文档（不再降级为纯文本）
        return generate_word_bytes(content)
    else:
        logger.warning(f"⚠️ 未知导出格式[{fmt}]，降级为Markdown原样输出")
        return content.encode("utf-8")
