# -*- coding: utf-8 -*-
"""
@File    : file_output_manager.py
@Time    : 2026/6/10 15:01
@Author  : tanlei
"""
import os
import re
import json
import uuid
import logging
from datetime import datetime
from typing import Optional
from app.core.vector_db import ChromaDBSingleton
from app.core.config import config

logger = logging.getLogger("FILE_OUTPUT_MANAGER")


class FileOutputManager:
    # 产物索引文件名（下划线前缀，避免被 list_asset_files 的 *.md 扫描命中）
    MANIFEST_NAME = "_manifest.json"
    # 一次性辅助文件目录（增量归并基线的快照、待确认删除清单等）
    # 单独放子目录的原因：① 这些文件不是「用例产物」，混在 outputs 根目录会被用户误当成产物；
    #                      ② 子目录名不以 -{asset_type}.md 结尾，天然不会被产物扫描命中。
    META_DIR_NAME = "_meta"
    # 报告类资产类型：统一落 report/ 目录（与 outputs 同级），与测试用例产物分开。
    #   review_report       —— 需求评审报告
    #   case_review_report  —— 用例质量评审报告（case-quality-scorer 产出）
    # 约定：以后所有「报告」类产物一律放 report/，outputs/ 只放用例类产物。
    REPORT_ASSET_TYPES = ("review_report", "case_review_report")

    def __init__(self):
        self.output_dir = "./app/outputs"
        os.makedirs(self.output_dir, exist_ok=True)
        # 报告目录：与 outputs 同级（app/report）
        self.report_dir = "./app/report"
        os.makedirs(self.report_dir, exist_ok=True)
        self.vector_db = ChromaDBSingleton.get_instance()
        self._manifest_path = os.path.join(self.output_dir, self.MANIFEST_NAME)
        self.meta_dir = os.path.join(self.output_dir, self.META_DIR_NAME)
        os.makedirs(self.meta_dir, exist_ok=True)

    @staticmethod
    def _canonical_asset_type(asset_type: str) -> str:
        """归一化资产类型单复数（test_case / test_cases），其余原样返回"""
        return "test_cases" if asset_type in ("test_case", "test_cases") else asset_type

    def _dir_for(self, asset_type: str) -> str:
        """按资产类型解析落盘目录：报告类 → report/，其余 → outputs/"""
        return self.report_dir if self._canonical_asset_type(asset_type) in self.REPORT_ASSET_TYPES \
            else self.output_dir

    # ========== 产物索引（manifest）==========
    # 背景：产物文件名 = {slug}-{asset_type}.md，**不含 module/page**。
    #       仅凭文件名既无法判断产物属于哪个页面，也无法按页面过滤（只能靠人眼猜），
    #       故在**落盘时**把 module/page/version 一并记录，供「补沉淀」按模块+页面精确筛选。
    #       索引只做「查询索引」，文件系统始终是产物的存在性真相源（索引里没有但磁盘有 → 仍返回，页面标记为未知）。
    def _load_manifest(self) -> dict:
        if not os.path.exists(self._manifest_path):
            return {}
        try:
            with open(self._manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            files = data.get("files") if isinstance(data, dict) else None
            return files if isinstance(files, dict) else {}
        except Exception as e:
            logger.warning(f"⚠️ 产物索引读取失败（按空索引处理）: {str(e)}")
            return {}

    def _save_manifest(self, files: dict):
        try:
            payload = {"version": 1, "files": files}
            with open(self._manifest_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ 产物索引写入失败（不影响产物落盘）: {str(e)}")

    def _prune_manifest(self, files: dict) -> dict:
        """
        回收「产物文件已被删除」的索引项。

        设计：文件系统是产物存在性的唯一真相源，索引只是归属信息的旁挂记录。
             所以产物被删后，索引项必须跟着消失，否则 _manifest.json 会无限膨胀，
             且残留项会让「该页面有几份产物」的判断失真。
             清理时机 = 读索引时（惰性 GC），无需用户手动维护，产物删了下一次扫描就自动对齐。
        """
        alive = {}
        dropped = []
        for filename, meta in (files or {}).items():
            # 报告类产物在 report/ 目录，存在性检查必须按资产类型解析目录，
            # 否则报告文件会被误判为「已删除」而错误回收索引
            asset_type = (meta or {}).get("asset_type", "")
            if os.path.exists(os.path.join(self._dir_for(asset_type), filename)):
                alive[filename] = meta
            else:
                dropped.append(filename)
        if dropped:
            preview = ", ".join(dropped[:5]) + (" …" if len(dropped) > 5 else "")
            logger.info(f"🧹 产物索引自动回收 {len(dropped)} 条（对应产物已删除）: {preview}")
        return alive

    # ========== 一次性辅助文件（增量归并快照 / 待确认删除清单）==========
    def get_meta_path(self, slug: str, name: str) -> str:
        """获取某次流程的辅助文件路径：outputs/_meta/{slug}-{name}"""
        os.makedirs(self.meta_dir, exist_ok=True)
        return os.path.join(self.meta_dir, f"{slug}-{name}")

    # ========== 续跑上下文（断点续跑必需，故与产物同生命周期）==========
    # 背景：索引（_manifest.json）里只存需求前 200 字摘要，够前端展示、不够重新生成。
    #       后端重启 / 隔天续跑时若无完整需求与上传文档，第二步就会拿 200 字需求产出用例，
    #       且**没有任何提示**。故把完整上下文单独落盘到 _meta（索引保持轻量、不膨胀）。
    REQ_CTX_NAME = "requirement_context.json"

    def save_requirement_context(
        self,
        slug: str,
        requirement_text: str = "",
        requirement_full: str = "",
        upload_doc_text: str = ""
    ) -> str:
        """
        落盘「断点续跑所需的需求上下文」（原子写：tmp + os.replace）。

        只在确有内容时写；产物被删后由 _prune_meta_snapshots 一并回收。
        """
        if not (requirement_text or "").strip() and \
                not (requirement_full or "").strip() and \
                not (upload_doc_text or "").strip():
            return ""
        path = self.get_meta_path(slug, self.REQ_CTX_NAME)
        payload = {
            "requirement_text": requirement_text or "",
            "requirement_full": requirement_full or "",
            "upload_doc_text": upload_doc_text or "",
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
            logger.info(
                f"🧷 续跑上下文已落盘 | slug={slug} | 需求={len(payload['requirement_text'])}字 | "
                f"合成文本={len(payload['requirement_full'])}字 | 文档段={len(payload['upload_doc_text'])}字"
            )
            return path
        except Exception as e:
            logger.error(f"❌ 续跑上下文落盘失败（不影响本次生成）: {str(e)}")
            return ""

    def load_requirement_context(self, slug: str) -> dict:
        """读取该 slug 的续跑上下文；不存在/损坏时返回 {}（调用方自行降级）"""
        path = self.get_meta_path(slug, self.REQ_CTX_NAME)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"⚠️ 续跑上下文读取失败（按缺失处理）| slug={slug} | {str(e)}")
            return {}

    def _prune_meta_snapshots(self, alive_slugs: set) -> list:
        """
        回收「产物已不存在」的一次性辅助文件（归并基线快照 / 待确认删除清单）。

        与 _prune_manifest 同一原则：产物删了，为它服务的一次性文件也就失去意义，
        继续留在 _meta 下只会变成无人认领的垃圾。
        """
        removed = []
        try:
            for filename in os.listdir(self.meta_dir):
                slug = re.sub(
                    r"-(merge_baseline\.md|pending_deletions\.json|requirement_context\.json)$",
                    "", filename
                )
                if slug == filename:        # 不是本模块管理的文件，不碰
                    continue
                if slug in alive_slugs:
                    continue
                try:
                    os.remove(os.path.join(self.meta_dir, filename))
                    removed.append(filename)
                except OSError:
                    pass
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"⚠️ 一次性辅助文件回收失败: {str(e)}")
        if removed:
            logger.info(f"🧹 一次性辅助文件自动回收 {len(removed)} 个（对应产物已删除）: {removed[:5]}")
        return removed

    def record_asset_meta(
        self,
        slug: str,
        asset_type: str,
        module: str = "",
        page: str = "",
        version: str = "",
        requirement: str = ""
    ):
        """把产物的 (module, page, version) 记入索引（键 = 产物文件名）"""
        canonical = "test_cases" if asset_type in ("test_case", "test_cases") else asset_type
        filename = f"{slug}-{canonical}.md"
        files = self._load_manifest()
        prev = files.get(filename) or {}
        files[filename] = {
            "slug": slug,
            "asset_type": canonical,
            "module": (module or "").strip(),
            "page": (page or "").strip(),
            "version": (version or "").strip(),
            "requirement": (requirement or prev.get("requirement", ""))[:200],
            "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._save_manifest(files)
        logger.info(
            f"🗂️ 产物索引已记录 | 文件={filename} | module={module or '未指定'} | "
            f"page={page or '模块层'} | version={version or '-'}"
        )

    def get_asset_meta(self, slug: str, asset_type: str) -> dict:
        """
        读取某产物的索引记录（module / page / version / requirement）。

        用途：后端重启后按 slug 从产物恢复上下文时，取回模块与页面归属
        （索引是「记，不猜」的产物归属真相源）。
        """
        canonical = "test_cases" if asset_type in ("test_case", "test_cases") else asset_type
        filename = f"{slug}-{canonical}.md"
        try:
            return self._load_manifest().get(filename) or {}
        except Exception as e:
            logger.warning(f"⚠️ 读取产物索引失败 | 文件={filename} | {str(e)}")
            return {}

    def generate_slug(self, title: str) -> str:
        slug = re.sub(r'[^\w\s-]', '', title).strip().lower()
        slug = re.sub(r'[-\s]+', '-', slug)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{slug}-{timestamp}"

    # ========== 基础：保存本地MD文件 ==========
    def save_file(
        self,
        slug: str,
        asset_type: str,
        content: str,
        module: str = "",
        page: str = "",
        version: str = "",
        requirement: str = ""
    ) -> str:
        """
        产物落盘，并把 (module, page, version) 记入产物索引。

        :param module/page/version: 生成时的业务上下文，供「补沉淀」按模块+页面精确筛选产物
        """
        filename = f"{slug}-{asset_type}.md"
        # 报告类资产（review_report / case_review_report）落 report/，用例类落 outputs/
        file_path = os.path.join(self._dir_for(asset_type), filename)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.debug(f"💾 结果落盘 | 文件={filename} | 长度={len(content)}字")
        # 记录索引（失败不影响落盘）
        try:
            self.record_asset_meta(slug, asset_type, module, page, version, requirement)
        except Exception as e:
            logger.warning(f"⚠️ 产物索引记录失败（不影响落盘）: {str(e)}")
        return file_path

    # ========== 沉淀1：写入项目历史库（向量库）==========
    def precipitate_to_project(
        self,
        slug: str,
        asset_type: str,
        content: str,
        requirement: str,
        module_name: str = "",
        version: str = "v1.0",
        page: str = ""
    ) -> str:
        """沉淀到项目历史库，后续同模块生成可语义召回

        :param page: 页面标识；"" = 模块层（该模块下所有页面召回时都会命中），
                     非空 = 仅该页面召回时命中
        """
        # 归一化资产类型：兼容 test_case 单数写法，保证删除/召回用同一标识
        canonical_asset_type = "test_cases" if asset_type in ("test_case", "test_cases") else asset_type
        page = (page or "").strip()

        # 只留最新基线：同 (模块, 资产类型, 页面) 只保留这一份，避免用例池无限膨胀污染召回
        meta = {
            "type": "project_asset",
            "asset_type": canonical_asset_type,
            "slug": slug,
            "requirement": requirement,
            "created_at": str(datetime.now()),
            "module_name": module_name,
            "version": version,
            "page": page
        }
        # asset_id 追加随机后缀：原实现用秒级时间戳，同秒两次沉淀会撞 id（后者覆盖前者）
        asset_id = f"{canonical_asset_type}_{slug}_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}"

        # ⚠️ 顺序：**先写入新基线，成功后再清理旧资产**。
        #    原实现是「先删后写」且中间无事务：add 一旦抛异常（embedding 失败 / chroma 报错），
        #    旧基线已删、新基线未入 ⇒ 该 (module, page) 基线彻底消失，
        #    后续增量召回与影响面分析全部静默退化为「无历史」，且没有任何提示。
        #    改为「先写后删」后最坏只是多留一条旧数据。
        #    索引内容剔除 segment_map 注释行：它只服务于「编号归属核对 / 评审装箱」，
        #    语义检索用不到，留在向量文档里只会稀释召回质量（产物文件与基线原文都保留它）。
        index_content = content
        if content and "segment_map" in content:
            from app.utils import segment_map as _segment_map
            index_content = _segment_map.strip_marker(content)
        self.vector_db.add_project_asset_with_meta(index_content, meta, asset_id)
        try:
            self.vector_db.delete_project_assets(
                module_name=module_name,
                asset_type=canonical_asset_type,
                page=page,
                exclude_ids=[asset_id],
            )
        except Exception as e:
            logger.warning(
                f"⚠️ 旧基线清理失败（新基线已写入，最坏多留一条旧数据，召回侧可能命中旧版）| "
                f"module={module_name} | page={page or '模块层'} | {str(e)}"
            )
        logger.info(
            f"✅ 沉淀到项目历史库完成 | 模块={module_name} | 页面={page or '模块层'} | 资产ID={asset_id}"
        )

        # 如果是测试用例，同步保存结构化元数据（供增量迭代按版本召回基线）
        if asset_type in ("test_cases", "test_case") and module_name:
            try:
                from app.utils.metadata_manager import meta_manager
                case_list = meta_manager.parse_markdown_cases(content)
                meta_manager.save_module_cases(
                    module_name=module_name,
                    req_version=version,
                    req_title=requirement[:100],
                    source_slug=slug,
                    md_content=content,
                    case_list=case_list,
                    page=page
                )
            except Exception as e:
                logger.error(f"❌ 结构化用例元数据保存失败（不影响向量库沉淀）: {str(e)}")

        return f"已成功沉淀到模块【{module_name}】项目历史库，资产ID：{asset_id}"

    # ========== 沉淀2：升级为通用技能模板 ==========
    def precipitate_to_general(
        self,
        content: str,
        skill_name: str,
        filename: str,
        requirement: str = ""
    ) -> str:
        """沉淀到对应Skill的参考文档目录，全团队复用，自动重载知识库"""
        # 改用配置中的绝对路径，避免依赖当前工作目录
        skill_dir = os.path.join(config.skills_dir, skill_name, "references")
        os.makedirs(skill_dir, exist_ok=True)
        file_path = os.path.join(skill_dir, filename)

        with open(file_path, "w", encoding="utf-8") as f:
            if requirement:
                f.write(f"# 沉淀自需求：{requirement[:100]}\n\n")
            f.write(content)
        logger.info(f"📝 通用技能模板落盘 | 路径={file_path} | 内容长度={len(content)}字")

        # 重新加载所有Skill知识库，立即生效（README承诺：沉淀通用库后自动重载）
        from app.services.plugin_loader import load_all_skills
        load_result = load_all_skills()
        logger.info(f"🔄 知识库已重载 | 加载文档数={load_result.get('count', 0)}")

        return f"已成功沉淀为通用技能模板：{filename}（知识库已重载，立即生效）"

    def get_file(self, filename: str) -> str:
        # 兼容两个目录：报告类文件在 report/，用例类在 outputs/
        suffix_type = ""
        for t in self.REPORT_ASSET_TYPES:
            if filename.endswith(f"-{t}.md"):
                suffix_type = t
                break
        file_path = os.path.join(self._dir_for(suffix_type), filename)
        if not os.path.exists(file_path):
            logger.error(f"❌ 文件不存在 | path={file_path}")
            raise FileNotFoundError(f"文件不存在: {filename}")
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    # ========== 产物文件路径 / 内容读取（供“沉淀读文件”与前端“重新加载”使用）==========
    def get_asset_file_path(self, slug: str, asset_type: str) -> str:
        """
        计算产物文件的绝对路径，归一化资产类型单复数（test_case / test_cases）。
        约定：落盘文件名 = {slug}-{asset_type}.md，与 save_file 保持一致；
              报告类资产在 report/ 目录，用例类在 outputs/ 目录。
        """
        canonical = self._canonical_asset_type(asset_type)
        filename = f"{slug}-{canonical}.md"
        return os.path.join(self._dir_for(canonical), filename)

    def get_asset_file(self, slug: str, asset_type: str) -> str:
        """
        读取产物文件内容（沉淀时的“真相源”）。
        文件不存在时返回空字符串，由调用方决定回退逻辑（如回退到内存上下文）。
        """
        file_path = self.get_asset_file_path(slug, asset_type)
        if not os.path.exists(file_path):
            logger.error(f"❌ 产物文件不存在 | path={file_path}")
            return ""
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    # ========== 产物编辑 / 删除（用例资产域、报告中心域）==========
    @staticmethod
    def _assert_slug_safe(slug: str):
        """slug 会参与拼路径，必须挡掉目录穿越与绝对路径（与 /upload 的越界校验同一思路）"""
        s = (slug or "").strip()
        if not s:
            raise ValueError("slug 不能为空")
        if "/" in s or "\\" in s or ".." in s or os.path.isabs(s) or s.startswith("."):
            raise ValueError(f"slug 含非法字符: {slug}")

    def _resolve_asset_path(self, slug: str, asset_type: str) -> str:
        """拼接并校验产物绝对路径，确保落在 outputs/ 或 report/ 之内"""
        canonical = self._canonical_asset_type(asset_type)
        path = self.get_asset_file_path(slug, canonical)
        base = os.path.realpath(self._dir_for(canonical))
        real = os.path.realpath(path)
        if not (real == base or real.startswith(base + os.sep)):
            raise ValueError(f"目标路径越界: {path}")
        return real

    def delete_asset_file(self, slug: str, asset_type: str) -> dict:
        """
        删除单个产物文件（用例类 outputs/ 或报告类 report/）。

        ⚠️ **只删本地产物文件，不动沉淀基线**（向量库 harness_project_assets 与
        metadata/modules/{module}_cases_meta.json）。理由：基线是权威召回源，
        删掉会让增量迭代静默退化成「无历史」且不报错。删基线属单独的高风险动作。

        连带清理这里**不需要做**，已有惰性 GC 兜住：
          · _manifest.json 的条目          → list_asset_files() 内 _prune_manifest()
          · outputs/_meta/{slug}-merge_baseline.md / {slug}-requirement_context.json
                                          → _prune_meta_snapshots()
        """
        self._assert_slug_safe(slug)
        real = self._resolve_asset_path(slug, asset_type)
        if not os.path.exists(real):
            logger.warning(f"⚠️ 删除产物：文件不存在 | path={real}")
            return {"deleted": False, "path": real, "message": "产物文件不存在（可能已被删除）"}
        os.remove(real)
        logger.info(
            f"🗑️ 产物文件已删除 | slug={slug} | asset_type={asset_type} | path={real} | "
            f"沉淀基线未改动（索引与快照由惰性 GC 回收）"
        )
        return {"deleted": True, "path": real,
                "message": "已删除产物文件；已沉淀的基线不受影响"}

    def save_asset_content(self, slug: str, asset_type: str, content: str) -> dict:
        """
        按 slug 覆盖写产物内容（产物编辑）。

        归属（module / page / version）从 _manifest.json 读回并沿用，保证「补沉淀」按页面
        筛选时仍然找得到；索引无记录时按空归属写入（与旧产物行为一致，前端标「页面未知」）。
        """
        self._assert_slug_safe(slug)
        if content is None:
            raise ValueError("content 不能为空")
        canonical = self._canonical_asset_type(asset_type)
        meta = self.get_asset_meta(slug, canonical) or {}
        real = self._resolve_asset_path(slug, canonical)
        existed = os.path.exists(real)
        with open(real, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            self.record_asset_meta(
                slug, canonical,
                meta.get("module", "") or "", meta.get("page", "") or "",
                meta.get("version", "") or "", meta.get("requirement", "") or "")
        except Exception as e:
            logger.warning(f"⚠️ 产物索引更新失败（内容已写入）: {str(e)}")
        logger.info(
            f"✏️ 产物内容已保存 | slug={slug} | asset_type={canonical} | "
            f"新建={not existed} | 长度={len(content)}字"
        )
        return {"saved": True, "path": real, "created": not existed, "length": len(content)}

    # ========== 历史产物扫描（供“忘了沉淀，事后补沉淀”使用）==========
    def list_asset_files(self, module: str = None, page: str = None) -> list:
        """
        扫描产物目录（outputs/ 用例类 + report/ 报告类），返回可选沉淀的历史产物清单。

        场景：生成后忘了点沉淀，事后想补沉淀 —— 只要产物 md 还在，
        就能凭 slug + asset_type 直接沉淀，无需重新调用大模型生成。
        约定文件名 = {slug}-{asset_type}.md
        （asset_type: test_cases / incremental_cases / review_report / case_review_report）

        :param module: 传入则只返回该模块的产物（依据产物索引记录；索引无记录的按“未知”处理）
        :param page:   传入则只返回该页面的产物（空字符串视为「模块层」，同样按索引判断）
        说明：**文件系统是存在性真相源**，索引只提供 module/page 归属；
              索引缺失（旧产物）→ module/page 返回空串，前端标注「页面未知」，
              不会因为没索引就凭空把产物藏起来。
        """
        manifest = self._load_manifest()
        # 惰性回收：产物被删 → 对应索引项同步清掉（避免 _manifest.json 无限膨胀/统计失真）
        pruned = self._prune_manifest(manifest)
        if len(pruned) != len(manifest):
            self._save_manifest(pruned)
        manifest = pruned
        items = []
        try:
            # (目录, 该目录下的资产类型)；报告类资产类型集合与 REPORT_ASSET_TYPES 保持一致
            scan_plans = (
                (self.output_dir, ("test_cases", "incremental_cases")),
                (self.report_dir, self.REPORT_ASSET_TYPES),
            )
            for scan_dir, asset_types in scan_plans:
                if not os.path.isdir(scan_dir):
                    continue
                for filename in os.listdir(scan_dir):
                    if not filename.lower().endswith(".md"):
                        continue
                    for asset_type in asset_types:
                        suffix = f"-{asset_type}.md"
                        if not filename.endswith(suffix):
                            continue
                        slug = filename[: -len(suffix)]
                        file_path = os.path.join(scan_dir, filename)
                        try:
                            st = os.stat(file_path)
                        except OSError:
                            continue
                        meta = manifest.get(filename) or {}
                        items.append({
                            "filename": filename,
                            "slug": slug,
                            "asset_type": asset_type,
                            "size": st.st_size,
                            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                            "module": meta.get("module", ""),
                            "page": meta.get("page", ""),
                            "version": meta.get("version", ""),
                            "indexed": bool(meta),
                        })
                        break
        except Exception as e:
            logger.error(f"❌ 扫描历史产物失败: {str(e)}")

        # 一次性辅助文件惰性回收（产物没了 → 归并快照/待确认清单也没用了）
        self._prune_meta_snapshots({it["slug"] for it in items})

        # 按模块/页面过滤（未传 = 不过滤）
        if module is not None:
            target_module = (module or "").strip()
            items = [it for it in items if (it.get("module") or "") == target_module]
        if page is not None:
            target_page = (page or "").strip()
            items = [it for it in items if (it.get("page") or "") == target_page]

        items.sort(key=lambda x: x.get("mtime", ""), reverse=True)
        logger.info(
            f"📂 历史产物扫描完成 | 过滤={('module=' + module) if module is not None else '全部'}"
            f"{(' | page=' + (page or '模块层')) if page is not None else ''} | 命中文件数={len(items)}"
        )
        return items

# 全局单例
output_manager = FileOutputManager()
