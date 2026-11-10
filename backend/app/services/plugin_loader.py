# -*- coding: utf-8 -*-
"""
@File    : plugin_loader.py
@Time    : 2026/5/22 14:13
@Author  : tanlei
"""
import os
import re
import yaml
import logging
import threading
from io import StringIO
from pathlib import Path
from app.core.vector_db import ChromaDBSingleton
from app.core.config import config

logger = logging.getLogger("PLUGIN_LOADER")

# 全局正则：匹配Markdown文件开头的Front Matter
FRONT_MATTER_REG = re.compile(r'^---\s*(.*?)\s*---', re.DOTALL)

# ==================== 重载互斥（规则上传/编辑落盘后热重载用）====================
# 为什么必须加锁：load_all_skills() 内部是 `clear_skills()` + `add_skills(全部)`，
# 两个调用之间有一个窗口，此时并发发起生成会**读到空技能库** —— 规则注入为空，
# 生成结果静默退化成「无语义精确注入」，既不报错也没有任何提示（最难查的一类问题）。
# 于是：① 用锁串行化重载；② 用 _reloading 事件让生成类接口在窗口期直接返回 409。
_RELOAD_LOCK = threading.RLock()
_reloading = threading.Event()


def is_reloading() -> bool:
    """当前是否正在重载技能库（生成类接口据此拒绝请求，避免读到空集合）"""
    return _reloading.is_set()


def parse_markdown_front_matter(file_path: str) -> dict:
    """解析Markdown文档开头的YAML Front Matter元数据"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        match = FRONT_MATTER_REG.match(content)
        if not match:
            return {}
        front_matter_content = match.group(1)
        front_matter = yaml.safe_load(StringIO(front_matter_content))
        return front_matter if isinstance(front_matter, dict) else {}
    except Exception as e:
        print(f"⚠️ 解析Front Matter失败: {file_path}, 错误: {str(e)}")
        return {}


def load_all_skills():
    """
    加载所有Skill及其知识库文档（**全量重载**，带互斥保护）。

    调用时机：① 进程启动；② 沉淀为通用模板；③ 规则补丁落盘；④ 规则管理页上传/编辑规则。
    因为是全量 clear + add，所以任何时刻调用都能让磁盘上的最新规则生效
    —— 这正是「规则管理页保存后无需重启后端」的依据。

    并发保护：重载期间 is_reloading() 为真，生成类接口据此返回 409（见 main.py 中间件），
    避免在 clear 与 add 之间读到空技能库导致规则静默丢失。
    """
    with _RELOAD_LOCK:
        _reloading.set()
        try:
            return _load_all_skills_impl()
        finally:
            _reloading.clear()


def _load_all_skills_impl():
    """
    加载所有Skill及其知识库文档，自动提取Front Matter元数据

    Front Matter 扩展字段（用于通用规则强制注入 + 页面级隔离）：
      - biz_module: 适用业务模块，all 表示全模块通用
      - rule_scope: common=通用规则（强制注入，不依赖向量检索）
                    module=模块专属规则（语义召回），默认 module
      - priority:   规则优先级，数值越大越优先，默认 0
      - page:       页面标识（如 pool），缺省=模块层（该模块下所有页面通用）
    """
    skills_dir = Path(config.skills_dir)
    docs = []
    metas = []
    ids = []
    idx = 1
    common_count = 0

    if not skills_dir.exists():
        logger.warning(f"⚠️ 技能目录不存在: {skills_dir}")
        return {"count": 0}

    logger.info(f"🔄 开始加载技能知识库 | 目录={skills_dir}")

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue

        skill_name = skill_dir.name
        logger.info(f"🔍 加载Skill: {skill_name}")

        # 1. 加载主SKILL.md文件
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            front_matter = parse_markdown_front_matter(str(skill_file))
            with open(skill_file, "r", encoding="utf-8") as f:
                content = f.read()

            docs.append(content)
            metas.append({
                "type": "skill",
                "skill_name": skill_name,
                "file_type": "main",
                "biz_module": "all",
                "rule_scope": "module",
                "priority": 0,
                "skill_id": front_matter.get("name", skill_name),
                "description": front_matter.get("description", "")
            })
            ids.append(f"skill_{idx}")
            idx += 1
            logger.info(f"  ✅ 主Skill文件已加载: {skill_file.name}")

        # 2. 加载references目录下所有知识库MD文件
        refs_dir = skill_dir / "references"
        if refs_dir.exists() and refs_dir.is_dir():
            ref_count = 0
            for ref_file in refs_dir.rglob("*.md"):
                file_path_str = str(ref_file)
                front_matter = parse_markdown_front_matter(file_path_str)
                biz_module = front_matter.get("biz_module", "unknown")
                skill_id = front_matter.get("name", ref_file.name)
                description = front_matter.get("description", "")
                rule_scope = front_matter.get("rule_scope", "module")
                priority = int(front_matter.get("priority", 0) or 0)
                # page：页面标识，缺省=模块层（该模块下所有页面通用）
                page = str(front_matter.get("page", "") or "").strip()

                with open(file_path_str, "r", encoding="utf-8") as f:
                    content = f.read()

                docs.append(content)
                metas.append({
                    "type": "skill",
                    "skill_name": skill_name,
                    "file_type": "reference",
                    "filename": ref_file.name,
                    "biz_module": biz_module,
                    "rule_scope": rule_scope,
                    "priority": priority,
                    "page": page,
                    "skill_id": skill_id,
                    "description": description
                })
                ids.append(f"skill_ref_{idx}")
                idx += 1
                ref_count += 1

                if rule_scope == "common":
                    common_count += 1
                    logger.info(
                        f"  ⭐ 通用规则已标记: {ref_file.name} | biz_module={biz_module} | priority={priority}"
                    )

            logger.info(f"  ✅ 加载了 {ref_count} 份参考文档")

    # 全量重载：先清空旧技能文档，避免重复加载导致ID冲突与库膨胀
    db = ChromaDBSingleton.get_instance()
    if docs:
        db.clear_skills()
        db.add_skills(docs, metas, ids)
        logger.info(
            f"✅ 所有技能加载完成 | 共{len(docs)}份文档 | "
            f"其中通用规则{common_count}份（强制注入，不依赖语义召回）"
        )
    else:
        logger.warning("⚠️ 未加载到任何技能文档")

    return {"count": len(docs), "common_rules": common_count}


if __name__ == "__main__":
    load_all_skills()
