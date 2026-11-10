# -*- coding: utf-8 -*-
"""
@File    : vector_db.py
@Time    : 2026/5/22 14:13
@Author  : tanlei
"""
import chromadb
from chromadb.utils import embedding_functions
import os
import logging

logger = logging.getLogger("VECTOR_DB")


def page_matches(meta_page, target_page):
    """
    判断某条规则文档/沉淀资产的 page 是否命中目标页

    :param meta_page:   文档元数据里记录的 page（"" 或 None 表示模块层，即该模块所有页面通用）
    :param target_page: 本次生成所针对的页面（"" 或 None 表示模块层，召回一切）
    :return: True=命中
    """
    if not target_page:
        # 模块层生成：召回该模块下所有页面 + 模块层资产
        return True
    meta_page = (meta_page or "").strip()
    # 页面层生成：召回模块层资产 + 本页资产，排除其他页
    return meta_page == "" or meta_page == target_page


class ChromaDBSingleton:
    _instance = None
    _client = None
    _skill_collection = None    # 通用技能集合
    _business_collection = None # 业务文档集合
    _project_collection = None  # 项目沉淀资产集合

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
            db_path = os.getenv("VECTOR_DB_PATH", "./chroma_db")
            cls._client = chromadb.PersistentClient(path=db_path)

            # Embedding 函数选择策略：
            # - USE_SENTENCE_TRANSFORMERS=1（默认）：使用 sentence-transformers
            #   PyTorch 版 all-MiniLM-L6-v2（384 维，中文效果较好）
            #   本地缓存路径：~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/
            #   离线模式（HF_HUB_OFFLINE=1）下可零网络启动
            # - USE_SENTENCE_TRANSFORMERS=0：使用 ChromaDB 默认 ONNX 版（80MB，更轻）
            #   本地缓存路径：~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/
            #   注意：本机 ONNX 模型可能损坏，建议离线环境保持 1
            use_st = os.getenv("USE_SENTENCE_TRANSFORMERS", "1") == "1"

            if use_st:
                logger.info("📦 使用 SentenceTransformerEmbeddingFunction（PyTorch版，本地缓存可用）")
                embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2",
                    device="cpu"  # 有NVIDIA显卡可改为 "cuda" 加速
                )
            else:
                # chromadb 自带的 DefaultEmbeddingFunction，首次使用会下载 ~80MB ONNX 模型
                logger.info("📦 使用 ChromaDB DefaultEmbeddingFunction（ONNX版）")
                embed_fn = embedding_functions.DefaultEmbeddingFunction()

            # 1. 通用技能集合
            cls._skill_collection = cls._client.get_or_create_collection(
                name="harness_skills",
                embedding_function=embed_fn
            )
            # 2. 业务文档集合（用户上传的需求文档、截图）
            cls._business_collection = cls._client.get_or_create_collection(
                name="harness_business_docs",
                embedding_function=embed_fn
            )
            # 3. 项目沉淀资产集合（历史用例、评审报告）
            cls._project_collection = cls._client.get_or_create_collection(
                name="harness_project_assets",
                embedding_function=embed_fn
            )
        return cls._instance

    # ========== 技能集合操作 ==========
    def add_skills(self, docs, metas, ids):
        """添加通用技能/知识库到技能集合"""
        self._skill_collection.add(documents=docs, metadatas=metas, ids=ids)
        logger.info(f"📚 技能文档入库 | 共{len(docs)}份")

    def get_common_rules(self, skill_name: str = None) -> str:
        """
        获取【通用规则】全文（rule_scope=common），不依赖向量检索，保证不被 top-K 截断

        通用规则（如后台列表页通用交互规则）必须强制注入到每次生成，
        若走语义召回，会因与需求描述语义距离较远而漏召，导致通用校验项缺失。

        :param skill_name: 限定某个Skill下的通用规则，None 表示全部
        :return: 通用规则全文拼接
        """
        try:
            where = {"rule_scope": "common"}
            if skill_name:
                where = {"$and": [{"rule_scope": "common"}, {"skill_name": skill_name}]}

            res = self._skill_collection.get(where=where, include=["documents", "metadatas"])

            # 兼容旧数据：早期加载的文档无 rule_scope 字段，按文件名兜底匹配
            if not res.get("documents"):
                logger.debug("ℹ️ 未命中 rule_scope 标记，尝试按文件名兜底匹配通用规则")
                all_res = self._skill_collection.get(include=["documents", "metadatas"])
                docs = []
                for doc, meta in zip(all_res.get("documents", []), all_res.get("metadatas", [])):
                    fname = (meta or {}).get("filename", "")
                    if "page_common_rules" in fname:
                        docs.append(doc)
                if docs:
                    logger.info(f"⭐ 通用规则兜底匹配成功 | 命中{len(docs)}份")
                    return "\n\n".join(docs)
                logger.warning("⚠️ 未找到任何通用规则文档")
                return ""

            docs = res["documents"]
            # 按 priority 降序拼装，高优先级规则在前
            metas = res.get("metadatas", []) or []
            paired = sorted(
                zip(docs, metas),
                key=lambda x: int((x[1] or {}).get("priority", 0) or 0),
                reverse=True
            )
            content = "\n\n".join([d for d, _ in paired])
            logger.info(f"⭐ 通用规则强制注入 | 命中{len(docs)}份 | 长度={len(content)}字")
            return content
        except Exception as e:
            logger.error(f"❌ 获取通用规则失败: {str(e)}")
            return ""

    # ========== 模块专属规则：按 (biz_module, page) 精确取全文（不走语义 top-K）==========
    def get_module_rules_full(
            self,
            biz_module: str,
            page: str = None,
            skill_name: str = None,
            max_chars: int = None
    ) -> str:
        """
        精确取回【模块专属规则】全文，替代"语义 top-K 召回"。

        为什么必须精确取（而不是靠向量召回）：
        规则文件走语义 top-K 时**不保证被召回**。一旦漏召：
          module_rules 为空 → ReqTestCases._extract_rule_ids 提取 0 个编号
          → 静默退化为单次全量生成（既不分片、也无编号自检），且全程不报错。
        编号是「分片 / 编号自检 / 受影响范围识别 / 规则补丁更新」的公共锚点，
        因此这里必须保证"规则全文可信"，与 get_common_rules 是同一设计取舍。

        过滤条件（缺一不可）：
          type=skill 且 file_type=reference
          rule_scope != common           （通用规则由 get_common_rules 强制注入，避免重复）
          page_matches(meta.page, page)  （页面级隔离）
          biz_module 命中                （meta.biz_module == biz_module 或 == "all"）
          skill_name 命中（传入时）

        排序：priority 降序 → 文件名升序，保证 prompt 稳定可复现。

        :param biz_module: 目标业务模块（必填，空则直接返回空，不做全库扫描）
        :param page: 目标页面；"" / None = 模块层（召回该模块全部页面规则）
        :param skill_name: 限定 Skill，如 req-test-cases
        :param max_chars: 总长上限；缺省读环境变量 MAX_MODULE_RULES_CHARS（默认 60000）
        :return: 拼接后的规则全文；未命中返回 ""
        """
        if not biz_module:
            logger.warning("⚠️ 精确取模块规则时 biz_module 为空，已跳过（避免全库扫描）")
            return ""

        try:
            res = self._skill_collection.get(include=["documents", "metadatas"])
        except Exception as e:
            logger.error(f"❌ 精确取模块规则失败（读取技能集合异常）: {str(e)}")
            return ""

        docs = res.get("documents") or []
        metas = res.get("metadatas") or []

        picked = []
        for doc, meta in zip(docs, metas):
            meta = meta or {}
            if meta.get("type") != "skill":
                continue
            if meta.get("file_type") != "reference":
                continue
            if (meta.get("rule_scope") or "module") == "common":
                continue
            if skill_name and meta.get("skill_name") != skill_name:
                continue
            # 页面级隔离：模块层规则 + 本页规则；排除其他页
            if not page_matches(meta.get("page"), page):
                continue
            mod = (meta.get("biz_module") or "").strip()
            if mod not in (biz_module, "all"):
                continue
            picked.append((
                int(meta.get("priority", 0) or 0),
                meta.get("filename") or "",
                doc or "",
            ))

        if not picked:
            logger.warning(
                f"⚠️ 未匹配到模块专属规则 | biz_module={biz_module} | page={page or '模块层'} | "
                f"skill={skill_name or '全部'}（请确认 rules front matter 的 biz_module / page 是否正确）"
            )
            return ""

        picked.sort(key=lambda x: (-x[0], x[1]))

        limit = max_chars if max_chars is not None else int(os.getenv("MAX_MODULE_RULES_CHARS", "60000"))
        parts, filenames, dropped = [], [], []
        used = 0
        for _prio, fname, doc in picked:
            if used + len(doc) > limit:
                dropped.append(fname or "(未命名)")
                continue
            parts.append(doc)
            filenames.append(fname or "(未命名)")
            used += len(doc)

        content = "\n\n".join(parts)
        logger.info(
            f"📏 模块专属规则精确注入 | biz_module={biz_module} | page={page or '模块层'} | "
            f"命中文件={len(picked)}个 | 已注入={len(filenames)}个 | 长度={len(content)}字 | "
            f"文件={filenames}"
        )
        if dropped:
            logger.warning(
                f"⚠️ 模块规则超出上限 {limit} 字，已跳过 {len(dropped)} 个文件：{dropped}"
                f"（可调大环境变量 MAX_MODULE_RULES_CHARS；注意被跳过的规则不会参与编号分片与自检）"
            )
        return content

    # ========== 业务文档集合操作 ==========
    def add_business_docs(self, docs, metas, ids):
        """添加用户上传的业务文档/截图"""
        self._business_collection.add(documents=docs, metadatas=metas, ids=ids)

    # ========== 项目资产操作 ==========
    def add_project_asset(self, content: str, asset_type: str, slug: str, requirement: str):
        """添加项目沉淀资产（历史用例、评审报告等）"""
        import time
        asset_id = f"{asset_type}_{slug}_{int(time.time())}"
        self._project_collection.add(
            documents=[content],
            metadatas=[{
                "type": "project_asset",
                "asset_type": asset_type,
                "slug": slug,
                "requirement": requirement,
                "created_at": str(time.strftime("%Y-%m-%d %H:%M:%S"))
            }],
            ids=[asset_id]
        )
        logger.info(f"💾 项目资产入库 | asset_type={asset_type} | slug={slug}")

    def add_project_asset_with_meta(self, content: str, meta: dict, asset_id: str):
        """
        添加项目沉淀资产（支持完整自定义元数据）

        沉淀需要携带 module_name / version / skill_name 等字段用于模块隔离检索，
        原先业务层直接访问 _project_collection 私有集合，此处收敛为公开方法。
        """
        self._project_collection.add(
            documents=[content],
            metadatas=[meta],
            ids=[asset_id]
        )
        logger.info(
            f"💾 项目资产入库 | asset_type={meta.get('asset_type', '')} | "
            f"module={meta.get('module_name', '') or '通用'} | id={asset_id}"
        )

    # ========== 混合检索 ==========
    def search_all(self, query: str, n_results: int = 5, biz_module: str = None, page: str = None):
        # 页面层召回时适当多取候选，避免 top-K 截断导致本页/模块层资产漏召，
        # 最终由调用方（_retrieve_full_context）按 page_matches 精确过滤
        fetch_n = n_results * 4 if page else n_results

        # ========== 1. 技能库：不过滤模块，全量语义检索 ==========
        # 原因：skill文档可能是通用的，也可能覆盖多个模块，严格过滤会误杀
        skill_res = self._skill_collection.query(
            query_texts=[query],
            n_results=fetch_n,
            where={"type": "skill"}
        )

        # ========== 2. 项目历史库：严格按模块过滤 ==========
        project_where = {"type": "project_asset"}
        if biz_module and biz_module.strip():
            project_where = {
                "$and": [
                    {"type": "project_asset"},
                    {"module_name": biz_module}
                ]
            }

        project_res = self._project_collection.query(
            query_texts=[query],
            n_results=fetch_n,
            where=project_where
        )

        # ========== 3. 合并结果 ==========
        merged_docs = skill_res["documents"][0] + project_res["documents"][0]
        merged_metas = skill_res["metadatas"][0] + project_res["metadatas"][0]
        merged_dists = skill_res["distances"][0] + project_res["distances"][0]
        merged_ids = skill_res["ids"][0] + project_res["ids"][0]

        return {
            "documents": [merged_docs],
            "metadatas": [merged_metas],
            "distances": [merged_dists],
            "ids": [merged_ids]
        }

    def search_by_module(
            self,
            query: str,
            module_name: str,
            n_results: int = 5,
            asset_type: str = "test_cases",
            page: str = None
    ):
        """
        按业务模块 + 资产类型（+ 可选页面）检索项目历史库
        :param query: 查询文本
        :param module_name: 业务模块值（如 bin、merchant）
        :param n_results: 返回条数
        :param asset_type: 资产类型（test_cases / review_report）
        :param page: 页面标识；空=模块层召回（全部页面），非空=仅模块层+本页
        """
        where_clause = {
            "$and": [
                {"type": "project_asset"},
                {"module_name": module_name},
                {"asset_type": asset_type}
            ]
        }

        # 页面层召回时适当多取候选，再按 page_matches 精确过滤
        fetch_n = n_results * 4 if page else n_results
        results = self._project_collection.query(
            query_texts=[query],
            n_results=fetch_n,
            where=where_clause
        )

        # 格式化返回，和现有调用格式对齐
        docs = []
        for idx, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][idx]
            if not page_matches(meta.get("page"), page):
                continue
            docs.append({
                "document": doc,
                "metadata": meta,
                "distance": results["distances"][0][idx]
            })
            if len(docs) >= n_results:
                break
        return docs

    def delete_project_assets(self, module_name: str, asset_type: str, page: str = "",
                              exclude_ids: list = None):
        """
        删除同 (模块, 资产类型, 页面) 下的旧项目资产，实现「每个模块/页面只留一份最新基线」

        :param module_name: 业务模块
        :param asset_type: 资产类型（test_cases / review_report）
        :param page: 页面标识，"" = 模块层
        :param exclude_ids: 需要保留的资产 id。用于「先写入新基线、再清理旧资产」的顺序 ——
                           chroma 的 delete(where=...) 没有排除参数，若直接整批删会把刚写入的
                           新基线一起删掉，故此处先 get 出候选 id、再按 id 精确删除。

        :return: 实际删除的条数（查询失败时返回 0，调用方据此判断是否降级）
        """
        where_clause = {
            "$and": [
                {"type": "project_asset"},
                {"module_name": module_name},
                {"asset_type": asset_type},
                {"page": page or ""}
            ]
        }
        keep = set(exclude_ids or [])

        # 查询失败时**不删**（最坏留一条旧数据），绝不能降级成整批 delete(where=...)，
        # 那会把刚写入的新基线一并抹掉 —— 正是本次修复要避免的
        try:
            got = self._project_collection.get(where=where_clause)
            target_ids = [i for i in (got.get("ids") or []) if i not in keep]
        except Exception as e:
            logger.warning(
                f"⚠️ 旧基线查询失败，本次跳过清理（最坏留一条旧数据）| "
                f"module={module_name} | asset_type={asset_type} | page={page or '模块层'} | {str(e)}"
            )
            return 0

        if not target_ids:
            logger.info(
                f"🧹 无待清理旧基线 | module={module_name} | asset_type={asset_type} | page={page or '模块层'}"
            )
            return 0

        self._project_collection.delete(ids=target_ids)
        logger.info(
            f"🗑️ 已清理旧基线 {len(target_ids)} 条 | module={module_name} | "
            f"asset_type={asset_type} | page={page or '模块层'}"
        )
        return len(target_ids)

    # ========== 清理操作 ==========
    def clear_skills(self):
        """清空技能集合（重启重载时用）"""
        try:
            self._skill_collection.delete(where={"type": "skill"})
        except Exception:
            pass
