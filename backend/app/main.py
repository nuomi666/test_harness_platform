# -*- coding: utf-8 -*-
"""
@File    : main.py
@Time    : 2026/5/22 14:12
@Author  : tanlei
"""
import os
import re
import time
import json
import uuid
import logging
import traceback
from datetime import datetime
from typing import Optional

os.environ["CHROMA_TELEMETRY_ENABLED"] = "false"

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
import os
from app.core.config import config
from app.core.logging_config import setup_logging
from app.utils.file_output_manager import output_manager
from app.utils.file_util import create_dirs
from app.services.plugin_loader import load_all_skills, is_reloading
from app.services.doc_processor import save_business_doc
from app.scripts.flow_scheduler import flow_scheduler, compose_requirement
from app.utils.context_manager import context_manager
from app.scripts.skill_state_manager import skill_state_manager
from fastapi.responses import Response
from app.utils.file_exporter import export_to_file

# 初始化全局日志
setup_logging()
logger = logging.getLogger("MAIN")

app = FastAPI(title="测试管理平台 API | 优先级+重试+超时+状态展示版")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== 规则重载窗口的并发闸门 ====================
# load_all_skills() 是 `clear_skills()` + `add_skills(全部)`，两者之间有一个窗口；
# 若此时并发发起生成，会**读到空技能库** —— 规则注入为空、结果静默变差且无任何报错。
# 规则管理页上传/编辑规则后会触发热重载（秒级），故在窗口期直接拒绝生成类请求，
# 前端会看到「规则正在重载，请稍后重试」——宁可失败一次，也不要一次静默的错误产物。
_RELOAD_GUARD_PATHS = {
    "/review", "/generate-cases", "/regenerate", "/generate-direct",
    "/generate-incremental", "/generate-review", "/api/analyze-impact",
    "/api/score-cases", "/api/propose-rule-patch",
}


@app.middleware("http")
async def guard_during_rule_reload(request: Request, call_next):
    if is_reloading() and request.method == "POST" and request.url.path in _RELOAD_GUARD_PATHS:
        logger.warning(f"⏸️ 规则正在重载，拒绝生成类请求 | path={request.url.path}")
        return JSONResponse(
            status_code=409,
            content={"detail": "业务规则正在重载，请稍后重试（约几秒）"})
    return await call_next(request)


@app.on_event("startup")
def startup():
    logger.info("🚀 平台启动中...")
    create_dirs()
    load_all_skills()
    logger.info("✅ 平台启动完成 | 代码/文档分离 + 重试/超时/优先级 + 前端状态展示")

# ========== 测试接口：验证代码是否生效 ==========
@app.get("/test")
def test():
    all_methods = [m for m in dir(flow_scheduler) if not m.startswith('_')]
    return {
        "msg": "接口正常，代码已加载",
        "flow_scheduler_methods": all_methods
    }

@app.get("/")
def health():
    return {"status": "running"}

@app.post("/upload")
def upload_file(file: UploadFile = File(...)):
    """
    上传业务文档 / 页面截图
    支持 pdf / docx / txt / md / 图片（图片走OCR提取文字）
    返回 file_text 供前端带入后续生成流程
    """
    try:
        start = time.time()
        logger.info(f"📤 收到上传请求 | filename={file.filename}")

        # 统一使用配置中的上传目录（修复：原代码硬编码 ./uploads，与 config.upload_dir 不一致）
        upload_dir = os.path.abspath(config.upload_dir)
        os.makedirs(upload_dir, exist_ok=True)

        # ⚠️ 文件名净化：原实现直接把 file.filename 拼进路径，`safe_name = f"{int(time.time())}_{file.filename}"`，
        #    形如 `..\..\app\main.py` 的 filename 可以写出到**任意路径**（Windows 下 `..` 与反斜杠均有效），
        #    而 CORS 为 * 且接口无鉴权 ⇒ 可远程利用。这里只取 basename 并剔除危险字符。
        raw_name = os.path.basename((file.filename or "").replace("\\", "/"))
        safe_part = re.sub(r"[^\w.\-]", "_", raw_name) or "upload"
        safe_name = f"{uuid.uuid4().hex[:8]}_{safe_part}"
        file_path = os.path.join(upload_dir, safe_name)

        # 双保险：解析后的真实路径必须仍落在 upload_dir 内
        real_dir = os.path.realpath(upload_dir)
        if os.path.dirname(os.path.realpath(file_path)) != real_dir:
            logger.warning(f"🚫 拒绝越界的上传文件名 | filename={file.filename!r}")
            raise HTTPException(status_code=400, detail="非法文件名")

        # 大小上限：原实现 file.file.read() 全量进内存，超大文件可直接打爆内存
        max_bytes = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
        file_bytes = file.file.read(max_bytes + 1)
        if len(file_bytes) > max_bytes:
            logger.warning(
                f"🚫 上传文件超过大小上限 | filename={file.filename} | "
                f"上限={max_bytes // (1024 * 1024)}MB"
            )
            raise HTTPException(
                status_code=413,
                detail=f"文件超过大小上限（{max_bytes // (1024 * 1024)}MB），请压缩后重试"
            )
        with open(file_path, "wb") as f:
            f.write(file_bytes)

        # 统一走 file_parser 解析：pdf/docx/txt/md/图片OCR 全覆盖
        from app.utils.file_parser import extract_document_text
        file_text = extract_document_text(file_bytes, file.filename)

        # 入库向量库（复用已解析文本，避免二次OCR）
        save_business_doc(file_path, file.filename, parsed_text=file_text)

        # 限制长度：原 5000 字会静默砍掉大半 PDF 需求；放宽后又发现 20 万字会把
        # 单次评审 prompt 顶到上下文窗口之外（输入端无守卫 ⇒ 平台直接报错）。
        # 现取 5 万字：覆盖常见需求文档，同时为「规则 + 历史用例 + 文档」留出预算空间。
        MAX_UPLOAD_TEXT_CHARS = int(os.getenv("MAX_UPLOAD_TEXT_CHARS", "50000"))
        returned_text = file_text[:MAX_UPLOAD_TEXT_CHARS]
        if len(file_text) > MAX_UPLOAD_TEXT_CHARS:
            logger.warning(
                f"⚠️ 上传文档文本被截断 | filename={file.filename} | "
                f"原始={len(file_text)}字 | 上限={MAX_UPLOAD_TEXT_CHARS}字"
            )

        cost = round(time.time() - start, 2)
        logger.info(
            f"✅ 上传处理完成 | filename={file.filename} | 耗时={cost}s | "
            f"提取文本长度={len(file_text)}字 | 回传={len(returned_text)}字"
        )

        return {
            "code": 0,
            "message": "上传成功",
            "filename": file.filename,
            "file_text": returned_text,
            # 实际提取长度与是否被截断：前端据此提示「已带入 N 字」，避免静默丢内容
            "file_text_length": len(file_text),
            "truncated": len(file_text) > len(returned_text)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 上传失败 | filename={file.filename} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/review")
def review_requirement(
        requirement: str = Form(...),
        biz_module: str = Form(None),
        upload_content: str = Form("")  # 新增：上传文件的文本内容
):
    try:
        # 把上传内容拼到需求前面（统一走 compose_requirement，与评审线/快速模式同一份文本）
        full_requirement = compose_requirement(requirement, upload_content)

        logger.info(
            f"📥 收到需求评审请求 | biz_module={biz_module or '未指定'} | "
            f"需求长度={len(requirement)}字 | 上传内容={len(upload_content)}字"
        )
        return flow_scheduler.run_requirement_review(full_requirement, biz_module)
    except Exception as e:
        logger.error(f"❌ 需求评审失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate-cases")
def generate_cases(slug: str = Form(...), biz_module: str = Form(""), page: str = Form("")):
    """
    基于评审结果生成测试用例（完整模式第二步）
    biz_module: 业务模块，前端已传但原后端未接收，导致模块过滤失效，此处补齐
    page: 页面标识，空=模块层（召回该模块全部页面）
    """
    try:
        logger.info(
            f"📥 收到用例生成请求 | slug={slug} | biz_module={biz_module or '未指定'} | "
            f"page={page or '模块层'}"
        )
        return flow_scheduler.run_test_case_generation(slug, biz_module or None, page or None)
    except Exception as e:
        logger.error(f"❌ 用例生成失败 | slug={slug} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/regenerate")
def regenerate_cases(supplement: str = Form(...), flow_id: str = Form(""), slug: str = Form("")):
    """
    补充业务规则后重新生成用例（支持多轮迭代）

    定位流程的优先级：flow_id > slug > 最近活跃流程
    （原实现固定取"最近的一个流程"，多任务并发时会串到别人的流程上，破坏流程隔离）
    """
    try:
        # 1. 优先按 flow_id 定位
        if not flow_id and slug:
            flow_id = context_manager.find_by_slug(slug) or ""

        # 2. 兜底：取最近一个活跃流程（保持旧行为兼容）
        if not flow_id:
            logger.warning("⚠️ 未提供flow_id/slug，回退到最近活跃流程（多任务并发时可能串流程）")
            for fid in list(context_manager._flow_contexts.keys()):
                flow_id = fid
                break

        if not flow_id:
            raise HTTPException(status_code=400, detail="请先启动流程")

        context_manager.add_supplement(flow_id, supplement)
        ctx = context_manager.get_context(flow_id)
        if not ctx:
            raise HTTPException(status_code=400, detail="流程上下文不存在，请重新生成")

        logger.info(f"🔁 重新生成用例 | flow_id={flow_id} | 补充规则长度={len(supplement)}字")
        return flow_scheduler.run_test_case_generation(ctx["slug"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 重新生成用例失败 | flow_id={flow_id} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

from urllib.parse import quote

@app.post("/export")
def export_file(content: str = Form(...), format: str = Form(...)):
    try:
        file_bytes = export_to_file(content, format)

        # 后缀名映射（word 修正为 docx，与 python-docx 实际产出格式一致）
        suffix_map = {
            "excel": "xlsx",
            "markdown": "md",
            "txt": "txt",
            "word": "docx"
        }
        suffix = suffix_map.get(format, "md")

        # 中文文件名编码：兼容HTTP头标准，解决latin-1编码报错
        filename_cn = f"测试用例.{suffix}"
        filename_encoded = quote(filename_cn)
        content_disposition = f"attachment; filename=test_case.{suffix}; filename*=UTF-8''{filename_encoded}"

        # 对应MIME类型（word 修正为 docx 标准MIME，避免Office打开时提示格式错误）
        mime_map = {
            "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "markdown": "text/markdown; charset=utf-8",
            "txt": "text/plain; charset=utf-8",
            "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        }
        media_type = mime_map.get(format, "application/octet-stream")

        logger.info(f"📤 文件导出成功 | format={format} | 后缀={suffix} | 大小={len(file_bytes)}字节")

        return Response(
            content=file_bytes,
            media_type=media_type,
            headers={"Content-Disposition": content_disposition}
        )
    except Exception as e:
        logger.error(f"❌ 文件导出失败 | format={format} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

# ==========【新增：需求评审生成接口】==========
@app.post("/generate-review")
async def generate_review(
    biz_module: str = Form(""),
    requirement_desc: str = Form(""),
    page: str = Form(""),
    upload_content: str = Form(""),
    upload_file: Optional[UploadFile] = File(None),
    history_version: str = Form(""),
    history_mode: str = Form("auto")
):
    """
    需求评审生成接口
    biz_module：前端下拉业务模块，可以为空（为空=全新需求，直接按规则评审）
    page：前端下拉页面，传入后评审报告按该页面归属（ctx / 落盘 / 历史召回三处一致）
    requirement_desc：需求输入文本；与 upload_content 至少有一个（只上传文档也能评审）
    upload_content：已解析的上传文本（前端先调 /upload 拿到，与 /review、/generate-direct 同一通道）
    upload_file：兼容保留，直接传真文件时由后端解析（pdf/txt/md/图片OCR）
    history_version：指定带入评审的历史用例基线版本，空=自动取该 (module,page) 最新版本
    history_mode：auto=按模块/页面带入历史用例（精简后注入，取不到则按新需求评审）；off=不带历史用例
    """
    try:
        if not (requirement_desc or "").strip() and not (upload_content or "").strip() \
                and upload_file is None:
            raise HTTPException(status_code=400, detail="需求描述与上传文档至少需要提供一项")

        file_bytes: Optional[bytes] = None
        filename = ""
        if upload_file is not None and not (upload_content or "").strip():
            file_bytes = await upload_file.read()
            filename = upload_file.filename
            logger.info(f"📎 评审请求携带上传文件 | filename={filename} | 大小={len(file_bytes)}字节")

        logger.info(
            f"📥 收到评审报告生成请求 | biz_module={biz_module or '未指定（按新需求评审）'} | "
            f"page={page or '模块层'} | 需求长度={len(requirement_desc or '')}字 | "
            f"上传文本={len(upload_content or '')}字 | history_mode={history_mode} | "
            f"history_version={history_version or '自动取最新'}"
        )
        resp = flow_scheduler.run_review_generation(
            biz_module=biz_module,
            requirement_desc=requirement_desc,
            upload_file_bytes=file_bytes,
            upload_filename=filename,
            page=page or None,
            upload_content_text=upload_content or "",
            history_version=history_version or "",
            history_mode=history_mode or "auto"
        )
        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 评审报告生成失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

# ==================== 统一沉淀接口（适配前端调用）====================
@app.post("/api/precipitate")
def precipitate_asset(
    slug: str = Form(...),
    asset_type: str = Form(...),
    precipitate_type: str = Form(...),
    module_name: str = Form(""),
    skill_name: str = Form("req-test-cases"),
    page: str = Form(""),
    version: str = Form("v1.0"),
    requirement: str = Form(""),
    removed_case_names: str = Form(""),
    removed_case_reasons: str = Form("")
):
    """
    precipitate_type 取值:
    - project: 沉淀到项目历史库（本模块复用）
    - general: 沉淀为通用模板（全团队复用）
    page: 页面标识，空=模块层（该模块下所有页面召回时都会命中）
    version: 用例版本号（默认 v1.0），与 page 一起构成结构化基线的唯一键
    requirement: 需求描述（无内存上下文时由前端兜底传入，仅用于沉淀备注）
    removed_case_names: JSON数组，用例评审后人工确认删除的用例名（仅 test_cases 生效）——
        沉淀前从内容中剔除这些用例行，并同步覆盖产物文件保持「产物 = 基线」一致；
        删除原因回写评分 sidecar（有评分记录时），可追溯
    removed_case_reasons: JSON对象，{用例名: 删除原因}，与 removed_case_names 配套
    ✅兼容逻辑：
    1. 需求评审：上下文存在biz_module → 优先使用上下文模块，忽略前端module_name
    2. 测试用例：上下文无biz_module → 降级使用前端传入module_name，原有逻辑完全不变
    """
    try:
        logger.info(
            f"💾 收到沉淀请求 | slug={slug} | asset_type={asset_type} | "
            f"precipitate_type={precipitate_type} | module_name={module_name} | "
            f"page={page or '模块层'} | version={version or 'v1.0'} | skill_name={skill_name}"
        )

        # 根据slug匹配上下文（改用公开方法）
        ctx = context_manager.get_context_by_slug(slug)

        # 优先级：上下文biz_module > 前端入参module_name
        ctx_biz_module = (ctx.get("biz_module", "") if ctx else "")
        final_module = ctx_biz_module.strip() if ctx_biz_module.strip() else module_name.strip()

        # 页面优先级：上下文page > 前端入参page
        ctx_page = (ctx.get("page", "") if ctx else "")
        final_page = ctx_page.strip() if ctx_page.strip() else page.strip()

        # 校验资产类型（兼容单复数两种写法：test_case / test_cases；报告类两种都允许沉淀）
        if asset_type not in ("review_report", "case_review_report", "test_case", "test_cases"):
            logger.error(f"❌ 未知资产类型 | asset_type={asset_type}")
            raise HTTPException(status_code=400, detail=f"未知资产类型: {asset_type}")

        # 优先读落盘产物文件：用户手动改磁盘文件后点沉淀，沉淀的就是改后内容；
        # 文件不存在（老流程/异常）则回退内存上下文，保证向后兼容
        content = output_manager.get_asset_file(slug, asset_type) or ""
        if not content:
            if asset_type == "review_report":
                content = ctx.get("review_report", "") if ctx else ""
            else:  # test_case / test_cases
                content = ctx.get("test_cases", "") if ctx else ""
            logger.info(f"📄 产物文件不存在，回退内存上下文 | slug={slug} | asset_type={asset_type}")

        if not content:
            logger.warning(f"⚠️ 无可沉淀内容 | slug={slug} | 上下文是否存在={ctx is not None}")
            raise HTTPException(status_code=400, detail="没有可沉淀的内容")

        # ============ 用例评审删除：沉淀前剔除人工确认的差评用例（仅 test_cases）============
        # 与增量删除闸门同一哲学：模型建议 → 人工勾选 → 这里才执行剔除。
        # 剔除后同步覆盖产物文件，保证「产物文件 = 基线内容」一致（参照 confirm-deletions 的做法）。
        if asset_type in ("test_case", "test_cases") and (removed_case_names or "").strip():
            try:
                names_to_remove = json.loads(removed_case_names)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="removed_case_names 不是合法 JSON 数组")
            if not isinstance(names_to_remove, list):
                raise HTTPException(status_code=400, detail="removed_case_names 必须是 JSON 数组")
            if names_to_remove:
                from app.scripts.case_quality_scorer import remove_cases_from_markdown
                content, removed_cnt = remove_cases_from_markdown(content, names_to_remove)
                logger.info(
                    f"🗑️ 沉淀前剔除评审确认删除的用例 | slug={slug} | "
                    f"传入={len(names_to_remove)}个 | 实际剔除={removed_cnt}行"
                )
                if removed_cnt > 0:
                    # 覆盖产物文件（保持产物与基线一致；旧内容可在 git/备份中追溯）
                    try:
                        output_manager.save_file(
                            slug, "test_cases", content,
                            module=final_module, page=final_page, version=(version or "v1.0"),
                            requirement=requirement,
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ 产物文件覆盖失败（沉淀仍按剔除后内容进行）: {str(e)}")
                    # 删除原因回写评分 sidecar（该版本有评分记录时）
                    try:
                        reasons_map = json.loads(removed_case_reasons) if removed_case_reasons.strip() else {}
                        if not isinstance(reasons_map, dict):
                            reasons_map = {}
                    except json.JSONDecodeError:
                        reasons_map = {}
                    try:
                        from app.utils.case_score_store import case_score_store
                        from app.scripts.req_test_cases import ReqTestCases
                        decisions = {
                            ReqTestCases._norm_case_name(n): {
                                "decision": "deleted",
                                "reason": reasons_map.get(n) or "评审确认删除",
                            }
                            for n in names_to_remove
                        }
                        case_score_store.update_decisions(
                            final_module, final_page, (version or "v1.0"), decisions)
                    except Exception as e:
                        logger.warning(f"⚠️ 删除决策回写评分失败（不影响沉淀）: {str(e)}")

        requirement = (ctx.get("requirement_text", "") if ctx else "") or requirement
        if not requirement:
            # 无内存上下文（例如后端重启后「事后补沉淀」）时，用 slug 去掉尾部时间戳兜底，
            # 保证沉淀记录里的需求标题可读
            parts = slug.rsplit("-", 1)
            requirement = parts[0] if len(parts) == 2 and parts[1].isdigit() else slug
            logger.info(f"📝 需求标题兜底（取自 slug）| requirement={requirement}")
        # 版本号：前端传入优先，缺省 v1.0（与 page 一起决定结构化基线的键）
        final_version = (version or "").strip() or "v1.0"

        # ============ 分支1：沉淀到项目历史库（本模块复用）============
        if precipitate_type == "project":
            if not final_module:
                logger.warning(f"⚠️ 沉淀项目库但无有效业务模块 | slug={slug}")
                raise HTTPException(
                    status_code=400,
                    detail="未识别到有效业务模块，不允许沉淀到本模块历史库，请选择沉淀为通用模板"
                )
            msg = output_manager.precipitate_to_project(
                slug=slug,
                asset_type=asset_type,
                content=content,
                requirement=requirement,
                module_name=final_module,
                version=final_version,
                page=final_page
            )
            logger.info(
                f"✅ 沉淀到项目历史库成功 | slug={slug} | 模块={final_module} | "
                f"页面={final_page or '模块层'} | 版本={final_version}"
            )
            return {"code": 0, "message": msg, "biz_module": final_module,
                    "page": final_page, "version": final_version}

        # ============ 分支2：沉淀为通用技能模板（全团队复用 + 自动重载知识库）============
        elif precipitate_type == "general":
            filename = f"precipitated_{asset_type}_{slug}.md"
            msg = output_manager.precipitate_to_general(
                content=content,
                skill_name=skill_name,
                filename=filename,
                requirement=requirement
            )
            logger.info(f"✅ 沉淀为通用技能模板成功 | slug={slug} | skill={skill_name} | 文件={filename}")
            return {"code": 0, "message": msg, "biz_module": final_module}

        else:
            logger.error(f"❌ 未知沉淀类型 | precipitate_type={precipitate_type}")
            raise HTTPException(status_code=400, detail=f"未知沉淀类型: {precipitate_type}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 沉淀失败 | slug={slug} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 新增：前端状态查询接口 ====================
@app.get("/api/skill-status")
def get_skill_status(flow_id: str = Query(..., description="流程ID")):
    """
    查询当前所有子Skill执行状态 & 中间结果（供前端轮询展示执行进度）
    说明：流程刚创建、子Skill尚未注册时返回空数据而非404，避免轮询期间前端报错
    """
    data = skill_state_manager.get_flow_states(flow_id)
    if not data:
        return {"code": 0, "data": {}, "message": "流程尚未开始或无状态数据"}
    return {"code": 0, "data": data}

# ==================== 新增：跳过评审，直接生成用例 ====================
@app.post("/generate-direct")
async def generate_cases_direct(
        requirement: str = Form(...),
        biz_module: str = Form(None),
        page: str = Form(""),                       # 页面标识，空=模块层
        upload_content: str = Form(""),          # 已解析的上传文本（前端先调 /upload 拿到）
        upload_file: Optional[UploadFile] = File(None)  # 也支持直接传文件（含图片OCR）
):
    try:
        # 直接上传的文件优先解析（覆盖手工传入的 upload_content）
        if upload_file is not None:
            file_bytes = await upload_file.read()
            from app.utils.file_parser import extract_document_text
            parsed = extract_document_text(file_bytes, upload_file.filename)
            if parsed:
                upload_content = parsed
                logger.info(
                    f"📎 快速模式直传文件解析成功 | filename={upload_file.filename} | 长度={len(parsed)}字"
                )

        # 把上传内容拼到需求前面（统一走 compose_requirement）
        full_requirement = compose_requirement(requirement, upload_content)

        logger.info(
            f"📥 收到快速模式生成请求 | biz_module={biz_module or '未指定'} | "
            f"page={page or '模块层'} | 需求长度={len(requirement)}字 | 上传内容={len(upload_content)}字"
        )
        return flow_scheduler.run_direct_case_generation(full_requirement, biz_module, page or None)
    except Exception as e:
        logger.error(f"❌ 快速模式生成失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
@app.post("/generate-incremental")
def generate_incremental_cases(
    module_name: str = Form(...),
    change_description: str = Form(...),
    history_version: str = Form(None),
    page: str = Form(""),
    impact_analysis: str = Form("")
):
    """
    增量用例生成（第二阶段）：
    impact_analysis 为第一阶段影响面分析经用户确认后的结果，非空时注入生成约束
    """
    try:
        logger.info(
            f"📥 收到增量用例生成请求 | module={module_name} | page={page or '模块层'} | "
            f"history_version={history_version or '默认'} | 携带影响面分析={bool(impact_analysis)} | "
            f"变更描述长度={len(change_description)}字"
        )
        return flow_scheduler.run_incremental_cases(
            module_name, change_description, history_version,
            page=page or None, impact_analysis=impact_analysis or None
        )
    except Exception as e:
        logger.error(f"❌ 增量用例生成失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 增量「建议删除」确认接口（hold → 人工勾选 → 二次归并覆盖产物）====================
@app.post("/api/confirm-deletions")
def confirm_incremental_deletions(
    slug: str = Form(...),
    deleted_names: str = Form("[]", description='JSON 数组：确认要删除的用例名；空数组=全部保留'),
    module_name: str = Form(""),
    page: str = Form(""),
    change_description: str = Form(""),
    history_version: str = Form("")
):
    """
    增量生成后，模型「建议删除」的用例**默认不会真的被删掉**（全部保留在产物里）。

    本接口用于人工勾选确认：只剔除确认过的用例，并用同一 slug 覆盖 {slug}-test_cases.md，
    其余用例逐字不变。传空数组即「全部保留」，不执行任何删除。

    安全约束：未出现在本次「建议删除」清单里的用例名会被拒绝（防误删、防跨流程操作）；
    module_name / page / history_version 仅在服务重启导致归并缓存丢失时用于降级重建。
    """
    try:
        names = json.loads(deleted_names) if deleted_names else []
        if not isinstance(names, list):
            raise HTTPException(status_code=400, detail="deleted_names 必须是 JSON 数组")
        logger.info(
            f"📥 收到增量删除确认请求 | slug={slug} | 提交待删={len(names)}条 | "
            f"page={page or '模块层'}"
        )
        return flow_scheduler.apply_incremental_deletions(
            slug, names,
            module_name=module_name, page=page,
            change_description=change_description, version=history_version
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 增量删除确认失败 | slug={slug} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 增量模式【第一阶段】影响面分析接口 ====================
@app.post("/api/analyze-impact")
def analyze_impact(
    module_name: str = Form(...),
    change_description: str = Form(...),
    page: str = Form(""),
    history_version: str = Form("")
):
    """
    分析需求变更对现有模块的影响范围，产出供用户确认后再生成增量用例

    history_version：前端选定的历史基线版本号；传入则按 (module, page, version)
                     走元数据精确取全文作为分析基线（不传=向量语义兜底）
    """
    try:
        logger.info(
            f"📥 收到影响面分析请求 | module={module_name} | page={page or '模块层'} | "
            f"history_version={history_version or '未选（走向量兜底）'} | "
            f"变更描述长度={len(change_description)}字"
        )
        return flow_scheduler.run_impact_analysis(
            module_name, change_description, page or None, history_version or None
        )
    except Exception as e:
        logger.error(f"❌ 影响面分析失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 模块历史版本列表（供增量模式版本下拉使用）====================
@app.get("/api/module-versions")
def get_module_versions(
    module_name: str = Query(..., description="业务模块名"),
    page: str = Query("", description="页面标识；传入则只返回该页面已沉淀的版本")
):
    """
    查询指定模块（可细化到页面）已沉淀的历史用例版本列表。

    返回：
    - data  : 版本号字符串列表（去重排序，保持向后兼容）
    - items : 明细列表 [{page, version, case_total, create_time}]，供下拉框展示「页面 / 版本」
    说明：同一模块下不同页面的版本互不干扰（历史教训：跨页串味），
          因此前端选页后应带上 page 过滤，避免选到别的页面的版本号。
    """
    try:
        from app.utils.metadata_manager import meta_manager
        target_page = (page or "").strip()
        items = meta_manager.get_module_page_versions(module_name)
        # 页面层过滤：只保留该页面的版本（页面层查询时排除模块层基线）
        if target_page:
            items = [it for it in items if (it.get("page") or "") == target_page]
        # 版本号去重排序（向后兼容旧的 data 字段）
        versions = sorted({it["version"] for it in items if it.get("version")})
        # 下一个可用版本号（增量沉淀默认值，避免覆盖基线）
        next_version = meta_manager.get_next_version(module_name, target_page)
        logger.info(
            f"🔍 查询模块版本列表 | module={module_name} | page={target_page or '全部页面'} | "
            f"版本数={len(versions)} | 明细数={len(items)} | 下一版本={next_version}"
        )
        return {"code": 0, "data": versions, "items": items, "next_version": next_version}
    except Exception as e:
        logger.error(f"❌ 查询模块版本列表失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 疑点澄清知识保存（增量影响面闭环）====================
@app.post("/api/save-clarifications")
def save_clarifications(
    module_name: str = Form(...),
    page: str = Form(""),
    items: str = Form(..., description='JSON 数组：[{"id","question","answer"}]')
):
    """
    保存影响面分析中用户对「需用户确认的疑点」的答复，作为该 (module, page) 的澄清知识。

    闭环语义：下次对同一页面做影响面分析时，这些答复会被自动注入 prompt，
    模型据此判断，不再重复提问同一批疑点（避免每次分析都被同样的问题卡住）。
    """
    try:
        import json as _json
        parsed = _json.loads(items or "[]")
        if not isinstance(parsed, list):
            raise HTTPException(status_code=400, detail="items 必须是 JSON 数组")
        from app.utils.metadata_manager import meta_manager
        total = meta_manager.save_clarifications(module_name, page, parsed)
        logger.info(
            f"📝 疑点澄清已保存 | module={module_name} | page={page or '模块层'} | "
            f"本次={len(parsed)}条 | 累计={total}条"
        )
        return {
            "code": 0,
            "message": f"已保存 {len(parsed)} 条澄清答复（累计 {total} 条）",
            "total": total
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 保存疑点澄清失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 方案C：规则补丁（补丁式 skill 更新 + 人工确认）====================
@app.get("/api/rule-files")
def list_rule_files(
    module_name: str = Query(..., description="业务模块名"),
    page: str = Query("", description="页面标识；空=模块层")
):
    """
    列出该 (模块, 页面) 对应的规则文件及其 R/S/F 编号清单。

    编号是「分片生成 / 编号自检 / 受影响范围识别 / 规则补丁定位」的公共锚点，
    因此前端在做补丁确认时，需要把「当前有哪些编号」一并展示给人工核对。
    """
    try:
        return flow_scheduler.list_rule_files(module_name, page or None)
    except Exception as e:
        logger.error(f"❌ 查询规则文件失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/propose-rule-patch")
def propose_rule_patch(
    module_name: str = Form(...),
    page: str = Form(""),
    change_description: str = Form(...),
    impact_analysis: str = Form("")
):
    """
    【方案C·第一步】基于「规则文件全文 + 变更确认书」生成结构化规则补丁（不落盘）。

    依赖前置修复：模块规则已改为「按 (biz_module, page) 精确取全文」，
    否则规则漏召会导致补丁依据不全（编号提取为 0 时分片与自检都会静默退化）。
    """
    try:
        logger.info(
            f"📥 收到规则补丁提案请求 | module={module_name} | page={page or '模块层'} | "
            f"变更描述长度={len(change_description)}字 | 影响面分析={bool(impact_analysis)}"
        )
        return flow_scheduler.propose_rule_patch(
            module_name, page or None, change_description, impact_analysis or ""
        )
    except Exception as e:
        logger.error(f"❌ 规则补丁提案失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/apply-rule-patch")
def apply_rule_patch(
    module_name: str = Form(...),
    page: str = Form(""),
    patches: str = Form(..., description='JSON 数组：补丁列表'),
    selected_ids: str = Form("", description='JSON 数组：勾选保留的补丁 id，空=全选'),
    change_note: str = Form("", description="本次需求变更描述（写入规则 front matter 供追溯）")
):
    """
    【方案C·第二步】把人工确认后的补丁定点应用到规则文件。

    落盘链路：备份到 references/_versions/ → 定点替换 → 打「需求变更改动」标记 → load_all_skills() 热重载。
    编号只增不改；废弃用 deprecate（改写为 R-10 形式），不物理删除。
    """
    try:
        import json as _json
        patch_list = _json.loads(patches or "[]")
        if not isinstance(patch_list, list):
            raise HTTPException(status_code=400, detail="patches 必须是 JSON 数组")
        sel_raw = _json.loads(selected_ids) if (selected_ids or "").strip() else None
        if sel_raw is not None and not isinstance(sel_raw, list):
            raise HTTPException(status_code=400, detail="selected_ids 必须是 JSON 数组或留空")

        logger.info(
            f"📥 收到规则补丁应用请求 | module={module_name} | page={page or '模块层'} | "
            f"补丁数={len(patch_list)} | 勾选数={len(sel_raw) if sel_raw else '全部'}"
        )
        return flow_scheduler.apply_rule_patch(
            module_name, page or None, patch_list, selected_ids=sel_raw,
            change_note=change_note or ""
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 规则补丁应用失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 规则管理：业务规则文件的浏览 / 上传 / 替换 / 编辑 ====================
# 概念对齐：「上传业务 skill」在本框架里 = 往 skills/req-test-cases/references/ 放页面规则文件。
#   · skill 本体由流程代码写死，上传整包新 skill 目录不会被启用；
#   · 判定「同一页面的规则是否已存在」靠 front matter 的 (biz_module, page) 等值匹配，
#     不看文件名、不看目录名（plugin_loader 递归扫描，目录名不参与匹配）；
#   · 保存后会**自动热重载知识库**（load_all_skills），因此**无需重启后端**即生效。

@app.get("/api/rule-file-target")
def rule_file_target(
    module_name: str = Query(..., description="业务模块（必填，规则归属锚点）"),
    page: str = Query("", description="页面标识；空=模块层"),
    filename: str = Query("", description="拟用文件名（可留空，后端按 page 兜底）"),
    content: str = Query("", description="拟写入内容（传了才会做 front matter 冲突检查）"),
    description: str = Query("", description="规则用途描述（写入 front matter）"),
):
    """
    落盘前预检：会命中哪个文件（替换）还是会新建到哪个目录，以及 front matter 有没有冲突。
    冲突**不静默改写**：这里只报差异，由前端弹窗二选一，选择通过 /api/rule-file-save 的
    front_matter_mode 传回（override=以页面选择为准补正 / keep=原样保留）。
    """
    try:
        from app.utils.rule_file_manager import rule_file_manager
        logger.info(
            f"🔎 规则文件预检 | module={module_name} | page={page or '模块层'} | filename={filename or '（自动）'}"
        )
        return rule_file_manager.plan_target(module_name, page or "", filename, content, description)
    except Exception as e:
        logger.error(f"❌ 规则文件预检失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rule-file-content")
def rule_file_content(
    module_name: str = Query("", description="业务模块"),
    page: str = Query("", description="页面标识"),
    filename: str = Query("", description="文件名（与 module_name+page 二选一）"),
):
    """读取单个规则文件全文（规则管理页的查看/编辑用）"""
    try:
        from app.utils.rule_file_manager import rule_file_manager
        res = rule_file_manager.read_file(filename, module_name or "", page or "")
        if not res.get("ok"):
            raise HTTPException(status_code=404, detail=res.get("message", "规则文件不存在"))
        logger.info(f"📖 读取规则文件 | {res['data']['rel']} | {res['data']['char_count']} 字")
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 读取规则文件失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rule-files-all")
def rule_files_all():
    """列出 references 下全部规则文件（规则管理页的「全部规则」视图，不按作用域过滤）"""
    try:
        from app.utils.rule_file_manager import rule_file_manager
        return rule_file_manager.list_files("", "")
    except Exception as e:
        logger.error(f"❌ 列出全部规则文件失败 | 错误: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rule-template")
def rule_template():
    """
    【规则管理】下发「业务 skill（页面规则）」骨架模板。

    模板文件固定放 `app/metadata/modules/业务skill模板.md`（可直接手工改，改完刷新即生效）；
    前端点【下载业务规则模板】时调用，只读、不改盘、不触发热重载。
    """
    try:
        from app.utils.rule_file_manager import rule_file_manager
        res = rule_file_manager.get_template()
        if not res.get("ok"):
            raise HTTPException(status_code=404, detail=res.get("message", "模板文件不存在"))
        return {
            "code": 0,
            "message": "OK",
            "filename": res.get("filename"),
            "content": res.get("content", ""),
            "char_count": res.get("char_count", 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 下发业务规则模板失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rule-file-save")
def rule_file_save(
    module_name: str = Form("", description="业务模块（编辑已有文件时可留空，靠 target_rel 定位）"),
    page: str = Form("", description="页面标识；空=模块层"),
    filename: str = Form("", description="文件名（留空按 page 兜底）"),
    content: str = Form(..., description="规则文件全文（含或不含 front matter）"),
    front_matter_mode: str = Form("override", description="override=以页面选择为准补正；keep=原样保留"),
    description: str = Form("", description="规则用途描述（写入 front matter）"),
    target_rel: str = Form("", description="要覆盖的既有文件（references 内相对路径，仅「编辑」用）"),
):
    """
    保存规则文件（新建或覆盖）：front matter 处理 → 备份到 references/_versions/ → 写盘
    → 重建 SKILL.md「规则文件索引」→ load_all_skills() 热重载。

    ⚠️ 覆盖同页面已存在的规则文件属破坏性操作，前端**必须先弹窗二次确认**再调本接口。
    """
    try:
        from app.utils.rule_file_manager import rule_file_manager
        if front_matter_mode not in ("override", "keep"):
            raise HTTPException(status_code=400, detail="front_matter_mode 只能是 override / keep")
        logger.info(
            f"📥 收到规则文件保存请求 | module={module_name} | page={page or '模块层'} | "
            f"filename={filename or '（自动）'} | target_rel={target_rel or '（按作用域定位）'} | "
            f"内容={len(content)}字 | fm模式={front_matter_mode}"
        )
        res = rule_file_manager.save_rule_file(
            module=module_name, page=page or "", filename=filename,
            content=content, front_matter_mode=front_matter_mode, description=description,
            target_rel=target_rel or "")
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("message", "保存失败"))
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 规则文件保存失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rule-file-upload")
def rule_file_upload(
    module_name: str = Form(..., description="业务模块"),
    page: str = Form("", description="页面标识；空=模块层"),
    filename: str = Form("", description="上传文件的原文件名（用于兜底命名）"),
    content: str = Form(..., description="上传文件的文本内容"),
    front_matter_mode: str = Form("override", description="override=以页面选择为准补正；keep=原样保留"),
    description: str = Form("", description="规则用途描述"),
):
    """
    上传业务规则（= 页面规则文件）。

    走**文本通道**（历史坑：把文本塞进 UploadFile 字段会 422）；
    「同页面已存在 → 直接替换」需要前端弹窗警告 + 二次确认后才调用本接口。
    """
    try:
        from app.utils.rule_file_manager import rule_file_manager
        if not (content or "").strip():
            raise HTTPException(status_code=400, detail="上传内容为空")
        logger.info(
            f"📥 收到规则文件上传 | module={module_name} | page={page or '模块层'} | "
            f"原文件名={filename or '（无）'} | 内容={len(content)}字"
        )
        res = rule_file_manager.save_rule_file(
            module=module_name, page=page or "", filename=filename or "",
            content=content, front_matter_mode=front_matter_mode, description=description)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("message", "上传失败"))
        res["uploaded_from"] = filename
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 规则文件上传失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 读取产物文件（前端改磁盘后重新加载预览）====================
@app.get("/api/asset-file")
def get_asset_file(
    slug: str = Query(..., description="流程slug"),
    asset_type: str = Query("test_cases", description="资产类型：test_cases / review_report / case_review_report")
):
    """读取已落盘的产物文件内容（沉淀真相源），供前端「重新加载最新内容」使用。
    报告类资产（review_report / case_review_report）在 report/ 目录，由后端按类型自动解析。"""
    try:
        content = output_manager.get_asset_file(slug, asset_type)
        if content is None:
            logger.info(f"🔎 产物文件不存在 | slug={slug} | asset_type={asset_type}")
            return {"code": 1, "data": "", "message": "产物文件不存在"}
        return {"code": 0, "data": content, "message": "ok"}
    except Exception as e:
        logger.error(f"❌ 读取产物文件失败 | slug={slug} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 产物删除 / 编辑（用例资产域、报告中心域）====================
@app.post("/api/delete-asset")
def delete_asset(
    slug: str = Form(..., description="产物 slug"),
    asset_type: str = Form(..., description="资产类型：test_cases / review_report / case_review_report"),
):
    """
    删除单个产物文件（用例类在 outputs/，报告类在 report/）。

    ⚠️ 只删本地产物文件，**不动沉淀基线**（向量库 + metadata/modules/{module}_cases_meta.json）。
    基线是权威召回源，删掉会让增量迭代静默退化成「无历史」；如需删基线请单独操作（高风险）。
    产物索引 _manifest.json 与 outputs/_meta/ 下的快照由既有惰性 GC 自动回收，这里不重复清理。
    """
    try:
        logger.info(f"📥 收到产物删除请求 | slug={slug} | asset_type={asset_type}")
        res = output_manager.delete_asset_file(slug, asset_type)
        return {"code": 0, "data": res, "message": res.get("message", "ok")}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"❌ 产物删除失败 | slug={slug} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/save-asset-content")
def save_asset_content(
    slug: str = Form(..., description="产物 slug"),
    asset_type: str = Form(..., description="资产类型"),
    content: str = Form(..., description="编辑后的产物全文"),
):
    """
    覆盖保存产物内容（产物编辑）。归属（module/page/version）从产物索引读回并沿用，
    保证「补沉淀」按模块+页面筛选时仍能命中。

    ⚠️ 编辑不会同步到已沉淀的基线：产物 ≠ 基线时需重新沉淀一次才会一致。
    """
    try:
        logger.info(
            f"📥 收到产物编辑保存 | slug={slug} | asset_type={asset_type} | 内容={len(content)}字"
        )
        res = output_manager.save_asset_content(slug, asset_type, content)
        return {"code": 0, "data": res, "message": "已保存产物内容（如已沉淀，请重新沉淀以同步基线）"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"❌ 产物编辑保存失败 | slug={slug} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 历史产物清单（供“忘了沉淀 → 事后补沉淀”）====================
@app.get("/api/output-files")
def list_output_files(
    module: Optional[str] = Query(None, description="业务模块；传入则只返回该模块的产物"),
    page: Optional[str] = Query(None, description="页面标识；传入则只返回该页面的产物（空串=模块层）"),
    asset_type: Optional[str] = Query(None, description="资产类型过滤：test_cases / review_report")
):
    """
    扫描产物目录（outputs/ 用例类 + report/ 报告类），列出可补沉淀的历史产物文件。

    场景：生成用例后忘了点沉淀（或想手动改完文件再沉淀），事后不想重新调大模型生成，
    直接按「模块 + 页面」筛出对应产物 → 走 /api/precipitate 补沉淀即可。

    module/page 为 None  = 不过滤（返回全部）；
    module/page 传空串  = 显式筛选「未指定模块 / 模块层」的产物。
    产物归属来自落盘时写入的产物索引（_manifest.json）；旧产物无索引时 module/page 为空串，
    前端会标注「页面未知」，不会被隐藏。
    """
    try:
        files = output_manager.list_asset_files(module=module, page=page)
        if asset_type:
            files = [f for f in files if f.get("asset_type") == asset_type]
        filtered = module is not None or page is not None
        msg = "ok"
        if filtered and not files:
            msg = "找不到相关页面的用例产物"
        logger.info(
            f"📂 返回历史产物清单 | module={module if module is not None else '全部'} | "
            f"page={(page if page is not None else '全部') or '模块层'} | 过滤={filtered} | 文件数={len(files)}"
        )
        return {"code": 0, "data": files, "message": msg, "filtered": filtered}
    except Exception as e:
        logger.error(f"❌ 获取历史产物清单失败 | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 用例质量评审（case-quality-scorer）====================
@app.post("/api/score-cases")
def score_cases_api(
    slug: str = Form(""),
    module_name: str = Form(""),
    page: str = Form(""),
    version: str = Form(""),
    use_engine_b: bool = Form(True)
):
    """
    用例质量评审（引擎 A 必跑、引擎 B 可跳过）。

    两种模式（二选一）：
    - slug 模式：评产物文件（生成后沉淀前 / 存量产物补评），模块/页面/版本自动取自产物索引
    - 基线模式：传 module_name + version（+ page），评结构化基线（产物文件已删时用）

    返回逐条评分 + 汇总 + suggest_delete 清单；删除不直接生效，
    需人工确认后走 /api/precipitate（removed_case_names）或 /api/apply-score-decisions。
    评审报告落 report/{slug}-case_review_report.md。
    """
    try:
        result = flow_scheduler.run_case_scoring(
            slug=slug.strip(),
            module_name=module_name.strip(),
            page=page.strip(),
            version=version.strip(),
            use_engine_b=use_engine_b,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 用例评审失败 | slug={slug} | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/case-scores")
def get_case_scores_api(
    module_name: str = Query(..., description="业务模块"),
    page: str = Query("", description="页面标识；空=模块层"),
    version: str = Query("", description="版本号；空=返回该模块全部版本评分")
):
    """查询用例评分 sidecar（metadata/modules/{module}_case_scores.json）"""
    try:
        from app.utils.case_score_store import case_score_store
        data = case_score_store.get_scores(module_name.strip(), page.strip(), version.strip())
        return {"code": 0, "data": data or {}, "message": "ok" if data else "该模块暂无评分记录"}
    except Exception as e:
        logger.error(f"❌ 查询用例评分失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/apply-score-decisions")
def apply_score_decisions_api(
    module_name: str = Form(...),
    base_version: str = Form(...),
    page: str = Form(""),
    removed_names: str = Form("[]"),
    reasons: str = Form("{}")
):
    """
    存量基线的评审处置：人工确认的差评用例从**新版本基线**中移除（旧版本保留可追溯）。

    removed_names: JSON数组，要移除的用例名（按归一化匹配）
    reasons: JSON对象，{用例名: 删除原因}，回写评分 sidecar 留审计痕
    """
    try:
        try:
            names = json.loads(removed_names)
            reasons_map = json.loads(reasons)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="removed_names / reasons 不是合法 JSON")
        if not isinstance(names, list):
            raise HTTPException(status_code=400, detail="removed_names 必须是 JSON 数组")
        if not isinstance(reasons_map, dict):
            reasons_map = {}

        result = flow_scheduler.apply_score_decisions(
            module_name=module_name.strip(),
            page=page.strip(),
            base_version=base_version.strip(),
            removed_names=names,
            reasons=reasons_map,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 评审处置失败 | module={module_name} | 错误: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


