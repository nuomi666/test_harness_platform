# -*- coding: utf-8 -*-
"""
@File    : case_quality_scorer.py
@Time    : 2026/9/29 14:30
@Author  : tanlei

用例质量评审 Skill（case-quality-scorer）：双引擎量化打分。

  引擎 A（代码硬指标，零模型调用、可复算）：
    A1 9 列完整性 / A2 步骤-预期 [n] 配对 / A3 编号引用有效性（对照规则全文）
    A4 用例名重复 / A5 历史语义重复（向量距离，仅提示）/ A6 枚举合法性
    A7 模型自语泄漏 + 模糊预期（正则黑名单）
  引擎 B（LLM rubric 锚定打分，可跳过）：
    B1 步骤可执行性 25% / B2 预期可验证性 30% / B3 业务正确性 25%（注入规则全文）/ B4 覆盖价值 20%

总分 = Σ(维度分/5 × 权重 × 100) − 引擎 A 硬伤扣分；引擎 B 跳过时按 100 − A 扣分。
分档：≥85 保留 / 60~84 待改进 / <60 或命中 high 硬伤 → 建议删除（删除不直接生效，走人工确认）。

设计约束（与平台既有哲学一致）：
  - 能用代码判的绝不问模型（引擎 A 先于引擎 B）；
  - 业务正确性的依据是规则全文（get_module_rules_full 精确取，不走语义 top-K）；
  - 单片失败不牵连整批，失败片标「评审失败」进报告（与 _call_segment 的 (text, ok) 模式一致）；
  - LLM 调用走 call_with_guard（超时/重试/截断保护）。
"""
import os
import re
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from collections import Counter

from app.scripts.skill_base import BaseSkill
from app.core.vector_db import ChromaDBSingleton
from app.utils.metadata_manager import CASE_HEADERS
from app.utils.md_table import split_md_row, escape_md_cell
from app.utils import segment_map
from app.scripts.req_test_cases import ReqTestCases

logger = logging.getLogger("CASE_QUALITY_SCORER")

_norm_case_name = ReqTestCases._norm_case_name


class CaseQualityScorer(BaseSkill):
    """用例质量评审：引擎 A（必跑）+ 引擎 B（可跳过）"""

    # ---- 阈值与常量（环境变量可调，不写死）----
    SEGMENT_SIZE = int(os.getenv("CASE_SCORE_SEGMENT_SIZE", "12"))       # 引擎 B 每片用例数
    # 「按编号归属装箱」时单片允许的最大条数（防同一编号的用例在产物里相隔很远，
    # 硬并成一箱会把输入预算撑爆）。超限强制切箱，并把跨片编号如实记进报告。
    PACK_MAX_SIZE = int(os.getenv("CASE_SCORE_PACK_MAX", str(SEGMENT_SIZE * 2)))
    KEEP_THRESHOLD = int(os.getenv("CASE_SCORE_KEEP_THRESHOLD", "85"))   # ≥ 此分：保留
    DELETE_THRESHOLD = int(os.getenv("CASE_SCORE_DELETE_THRESHOLD", "60"))  # < 此分：建议删除
    DRIFT_DELTA = int(os.getenv("CASE_SCORE_DRIFT_DELTA", "15"))         # 跨批次分数漂移告警阈值
    # 语义重复距离阈值（chroma 默认 L2，越小越相似）；仅提示不参与删留，实测后再调
    DUP_DISTANCE = float(os.getenv("CASE_SCORE_DUP_DISTANCE", "0.6"))

    VALID_TAGS = {"功能测试", "异常测试", "UI校验", "边界测试", "状态测试"}
    VALID_LEVELS = {"P0", "P1", "P2"}
    VALID_EDIT_MODES = {"STEP", "TABLE", "FREE"}
    VALID_AUTOMATABILITY = {"可直接自动化", "弱断言可自动化", "需改造后自动化", "人工保留"}

    # A7-a 模型自语泄漏（severity=high）：模型的犹豫/自我怀疑过程泄漏进正式产物。
    # 实测铁证：裁决页「多对1」用例前置条件含「…整体重叠？或者合并后FirstData有2条？哦…？不管…」。
    SELF_TALK_PATTERNS = [
        r"？或者", r"？哦", r"？不管", r"让我想想", r"重新组织", r"嗯[，,]",
        r"不确定[，,]", r"我理解为", r"应该是吧", r"可能吧", r"不对[，,]", r"等一下",
    ]
    # A7-b 模糊预期（severity=medium）：无法转成断言的表述（模型自己都不知道预期是什么）
    VAGUE_PATTERNS = [
        r"根据实际(展示|情况|页面|结果)", r"无明显(异常|错误|错位)", r"布局正常",
        r"显示正常", r"展示合理", r"不破坏.{0,6}布局", r"页面美观", r"体验良好",
    ]

    # 硬伤扣分：high=30 / medium=10 / low=5
    SEVERITY_SCORE = {"high": 30, "medium": 10, "low": 5}

    # 引擎 B 维度权重（总和 100）
    DIM_WEIGHTS = {"B1": 25, "B2": 30, "B3": 25, "B4": 20}

    def __init__(self):
        # max_retry=1：引擎 B 单片失败由 score_cases 自己做「失败片标注」，
        # 不需要基类再整片重试太多次（每次重试都是一次完整 LLM 调用）
        super().__init__(skill_name="case-quality-scorer", timeout=180, max_retry=1)
        self.vector_db = ChromaDBSingleton.get_instance()

    # ==================================================================
    # BaseSkill 抽象方法实现（本 skill 主入口是 score_cases，不走 run() 主路；
    # 这两个方法服务于引擎 B 的单片调用，保持与基类契约一致）
    # ==================================================================
    def get_prompt(self, input_context: Dict[str, Any]) -> str:
        return self._build_segment_prompt(
            input_context.get("cases", []),
            input_context.get("rules_full", ""),
        )

    def parse_output(self, raw_response: str) -> Dict[str, Any]:
        return {"scores": self._parse_segment_json(raw_response)}

    # ==================================================================
    # 引擎 A：代码硬指标（零模型调用）
    # ==================================================================
    @staticmethod
    def _extract_step_ids(text: str) -> set:
        return set(re.findall(r"\[(\d+)\]", text or ""))

    @staticmethod
    def _extract_rule_ids(text: str) -> set:
        # (?<![A-Z0-9]) 防止 "SR1" 里的 R1 被误当规则编号
        return set(re.findall(r"(?<![A-Z0-9])([RSF]\d+)", text or ""))

    def run_engine_a(
        self,
        cases: List[Dict[str, Any]],
        module: str = "",
        page: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """
        对结构化用例跑 7 项硬指标。

        :return: {norm_用例名: {"name", "hard_issues": [{metric, severity, detail, excerpt}],
                                "deduction": int, "has_high": bool}}
        """
        results: Dict[str, Dict[str, Any]] = {}

        # 预备 1：规则全文与编号集合（A3 用；取不到则 A3 整体跳过并告警）
        rule_ids: set = set()
        if module:
            try:
                rules_full = self.vector_db.get_module_rules_full(module, page=page or None) or ""
                rule_ids = self._extract_rule_ids(rules_full)
                if not rule_ids:
                    logger.warning(
                        f"⚠️ 引擎A：未取到模块规则编号，A3 编号引用校验跳过 | "
                        f"module={module} | page={page or '模块层'}"
                    )
            except Exception as e:
                logger.warning(f"⚠️ 引擎A：规则全文获取失败，A3 跳过 | module={module} | {str(e)}")

        # 预备 2：同批重名统计（A4）
        names = [str(c.get("用例名称", "")).strip() for c in cases]
        name_counts = Counter(_norm_case_name(n) for n in names if n)

        for case in cases:
            name = str(case.get("用例名称", "")).strip()
            norm = _norm_case_name(name) or f"__unnamed_{id(case)}"
            issues: List[Dict[str, str]] = []

            def add(metric, severity, detail, excerpt=""):
                issues.append({
                    "metric": metric, "severity": severity,
                    "detail": detail, "excerpt": (excerpt or "")[:80],
                })

            # ---- A1：9 列完整性（键齐全 + 用例名称非空）----
            missing_cols = [h for h in CASE_HEADERS if h not in case]
            if missing_cols:
                add("A1", "high", f"用例行缺少 {len(missing_cols)} 列：{'、'.join(missing_cols)}")
            if not name:
                add("A1", "high", "用例名称为空")

            # ---- A2：步骤-预期 [n] 配对 ----
            steps_text = str(case.get("步骤描述", "") or "")
            expect_text = str(case.get("预期结果", "") or "")
            step_ids = self._extract_step_ids(steps_text)
            expect_ids = self._extract_step_ids(expect_text)
            if step_ids or expect_ids:
                if step_ids != expect_ids:
                    only_step = sorted(step_ids - expect_ids)
                    only_expect = sorted(expect_ids - step_ids)
                    parts = []
                    if only_step:
                        parts.append(f"步骤有[{']['.join(only_step)}]但预期无对应编号")
                    if only_expect:
                        parts.append(f"预期有[{']['.join(only_expect)}]但步骤无对应编号")
                    add("A2", "medium", "步骤与预期的[n]编号不配对：" + "；".join(parts),
                        excerpt=steps_text[:60])

            # ---- A3：编号引用有效性（备注列引用的 R/S/F 编号须真实存在于规则文件）----
            if rule_ids:
                remark = str(case.get("备注", "") or "")
                cited = self._extract_rule_ids(remark)
                invalid = sorted(cited - rule_ids)
                if invalid:
                    add("A3", "medium",
                        f"备注引用了规则文件中不存在的编号：{'、'.join(invalid)}", excerpt=remark[:60])

            # ---- A4：用例名重复（同版本内）----
            if name and name_counts.get(_norm_case_name(name), 0) > 1:
                add("A4", "high",
                    f"同批内用例名重复（归一化后 {name_counts[_norm_case_name(name)]} 条同名）"
                    f"——按名归并/删除会产生歧义", excerpt=name)

            # ---- A6：枚举合法性 ----
            tag_text = str(case.get("标签", "") or "").strip()
            tags = [t.strip() for t in re.split(r"[,，、/]", tag_text) if t.strip()]
            invalid_tags = [t for t in tags if t not in self.VALID_TAGS]
            if invalid_tags:
                add("A6", "medium", f"非法标签：{'、'.join(invalid_tags)}"
                                    f"（合法值：{'、'.join(sorted(self.VALID_TAGS))}）", excerpt=tag_text)
            level = str(case.get("用例等级", "") or "").strip().upper()
            if level and level not in self.VALID_LEVELS:
                add("A6", "medium", f"非法用例等级：{level}（合法值：P0/P1/P2）")
            edit_mode = str(case.get("编辑模式", "") or "").strip().upper()
            if edit_mode and edit_mode not in self.VALID_EDIT_MODES:
                add("A6", "medium", f"非法编辑模式：{edit_mode}（合法值：STEP/TABLE/FREE）")

            # ---- A7：模型自语泄漏（high）+ 模糊预期（medium）----
            check_text = "。".join([
                str(case.get("前置条件", "") or ""),
                steps_text, expect_text,
            ])
            for pat in self.SELF_TALK_PATTERNS:
                m = re.search(pat, check_text)
                if m:
                    start = max(0, m.start() - 20)
                    add("A7", "high",
                        f"模型自语泄漏：命中 /{pat}/ —— 模型的犹豫过程混入了正式产物，必须重写",
                        excerpt=check_text[start:m.end() + 20])
                    break  # 一条用例报一次即可
            for pat in self.VAGUE_PATTERNS:
                m = re.search(pat, expect_text)
                if m:
                    start = max(0, m.start() - 15)
                    add("A7", "medium",
                        f"模糊预期：命中 /{pat}/ —— 无法转成断言，需人工补明确预期",
                        excerpt=expect_text[start:m.end() + 25])
                    break

            deduction = sum(self.SEVERITY_SCORE.get(i["severity"], 0) for i in issues)
            results[norm] = {
                "name": name,
                "hard_issues": issues,
                "deduction": min(deduction, 100),
                "has_high": any(i["severity"] == "high" for i in issues),
            }

        # ---- A5：历史语义重复（向量距离，仅提示 low，不参与删留决策）----
        # 排除与自身同名的命中（同名由 A4 管）；存量补评时基线自身就在向量库里，
        # 同名排除后找的是「不同名但语义近似」的用例。
        if module:
            for case in cases:
                name = str(case.get("用例名称", "")).strip()
                if not name:
                    continue
                norm = _norm_case_name(name)
                try:
                    query = f"{name} {str(case.get('步骤描述', '') or '')[:120]}"
                    res = self.vector_db.search_by_module(
                        query=query, module_name=module,
                        n_results=3, asset_type="test_cases", page=page or None,
                    )
                    hits = self._parse_search_hits(res, exclude_norm=norm)
                    if hits and hits[0][1] < self.DUP_DISTANCE:
                        results[norm]["hard_issues"].append({
                            "metric": "A5", "severity": "low",
                            "detail": f"与历史用例语义近似（L2 距离 {hits[0][1]:.3f} < "
                                      f"{self.DUP_DISTANCE}）：{hits[0][0][:50]}",
                            "excerpt": "",
                        })
                        results[norm]["deduction"] = min(
                            results[norm]["deduction"] + self.SEVERITY_SCORE["low"], 100)
                except Exception as e:
                    logger.warning(f"⚠️ 引擎A：A5 语义重复检索失败（跳过本条）| {name[:30]} | {str(e)}")
                    break  # 检索服务异常时整批跳过，不逐条刷告警

        total_issues = sum(len(r["hard_issues"]) for r in results.values())
        logger.info(
            f"🧪 引擎A完成 | 用例={len(cases)}条 | 命中硬伤={total_issues}处 | "
            f"high={sum(1 for r in results.values() if r['has_high'])}条"
        )
        return results

    def _parse_search_hits(self, res, exclude_norm: str) -> List[Tuple[str, float]]:
        """把 search_by_module 的返回规整为 [(文档摘要, distance)]，排除与自身同名的命中"""
        hits = []
        try:
            docs = (res.get("documents") or [[]])[0]
            distances = (res.get("distances") or [[]])[0]
            for doc, dist in zip(docs, distances):
                first_line = str(doc).splitlines()[0] if doc else ""
                # 从文档首行/内容里提取用例名做同名排除（文档是 md 片段，首列即用例名）
                doc_names = re.findall(r"\|\s*([^|]+?)\s*\|", str(doc)[:2000])
                if any(_norm_case_name(n) == exclude_norm for n in doc_names):
                    continue
                hits.append((first_line, float(dist)))
        except Exception:
            pass
        hits.sort(key=lambda x: x[1])
        return hits

    # ==================================================================
    # 引擎 B：LLM rubric 锚定打分（分片调用）
    # ==================================================================
    def _cases_to_markdown(self, cases: List[Dict[str, Any]]) -> str:
        """把结构化用例还原成 9 列 Markdown 表格（喂给模型；单元格重新转义竖线）"""
        lines = ["| " + " | ".join(CASE_HEADERS) + " |",
                 "|" + "---|" * len(CASE_HEADERS)]
        for c in cases:
            cells = [escape_md_cell(str(c.get(h, "") or "")) for h in CASE_HEADERS]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    def _build_segment_prompt(self, cases: List[Dict[str, Any]], rules_full: str) -> str:
        cases_md = self._cases_to_markdown(cases)
        rules_section = rules_full.strip() if rules_full.strip() else \
            "（未取到模块业务规则：B3 一律给 3 分并在 reason 标注「规则缺失-待人工确认」）"
        return f"""你是测试用例质量评审专家。请严格按以下 rubric 对给出的每条测试用例打分。

【评分维度与锚点】（每维 1~5 的整数）
B1 步骤可执行性：
  5分=每步单一动作、控件指称唯一（如"BIN号输入框"而非"某个输入框"）、无歧义分支
  4分=基本可执行，偶有合并步但可机械拆分
  3分=有合并步（如"输入X并点击Y"）或轻微歧义，需少量人工解读
  2分=多处依赖上下文才能理解
  1分=依赖人类上下文才能懂（如"按刚才的方式再来一遍"）
B2 预期可验证性：
  5分=每个预期都是值断言/状态断言（回显X、仅展示Y的记录、按钮置灰）
  4分=大部分是明确断言，个别弱断言
  3分=含弱断言（列表刷新、无报错）
  2分=多处不可断言表述
  1分=含不可断言表述（"根据实际展示""布局正常"）
B3 业务正确性（依据下方【业务规则全文】逐条比对）：
  5分=预期与业务规则完全一致
  3分=规则未覆盖该场景且无出处（reason 标"待人工确认"）
  1分=预期与业务规则矛盾（reason 必须写明与哪条规则矛盾）
B4 覆盖价值：
  5分=覆盖 P0/P1 规则条目且不与其他用例冗余
  3分=覆盖一般场景，或与其他用例部分冗余
  1分=重复覆盖低价值场景

【业务规则全文】（B3 的评判依据）
{rules_section}

【待评审用例】（共 {len(cases)} 条，9 列 Markdown 表格）
{cases_md}

【输出要求】
1. 只输出一个 json 代码块，不要输出任何其他文字、解释或 markdown 标题。
2. name 必须与输入表格中的「用例名称」逐字一致，不得改写、不得增删字。
3. automatability 取值限定：可直接自动化 / 弱断言可自动化 / 需改造后自动化 / 人工保留
   （判定：步骤可执行≥4 且预期可验证≥4 且前置可用接口构造 → 可直接自动化；
    含布局类/观察型弱断言 → 弱断言可自动化；模糊预期待补或前置需组合构造 → 需改造后自动化；
    视觉美观、感官判断类 → 人工保留）
4. fixture_pattern：把前置条件归纳成构造模式，如 ensure_bin_pool_record(bin_start="123456") /
   ensure_bin_pool_count(n=30) / 空池 / 需组合fixture（无法单接口构造时）。
5. 每条用例都要评，不得遗漏。

```json
{{"scores": [{{"name": "<用例名称>", "B1": 5, "B2": 5, "B3": 5, "B4": 4,
  "automatability": "可直接自动化", "fixture_pattern": "ensure_bin_pool_record",
  "reason": "一句话理由"}}]}}
```"""

    def _parse_segment_json(self, raw: str) -> List[Dict[str, Any]]:
        """解析引擎 B 单片输出：提取 JSON → 校验 scores 结构 → 维度分 clamp 到 1~5"""
        text = (raw or "").strip()
        candidate = ""
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            candidate = m.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                candidate = text[start:end + 1]
        if not candidate:
            raise ValueError("引擎B输出中未找到 JSON 块")

        data = json.loads(candidate)
        scores = data.get("scores")
        if not isinstance(scores, list) or not scores:
            raise ValueError("引擎B输出缺少 scores 数组")

        cleaned = []
        for item in scores:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            entry = {"name": str(item["name"]).strip()}
            for dim in ("B1", "B2", "B3", "B4"):
                try:
                    v = int(item.get(dim, 3))
                except (TypeError, ValueError):
                    v = 3
                entry[dim] = max(1, min(5, v))
            entry["automatability"] = str(item.get("automatability", "")).strip()
            entry["fixture_pattern"] = str(item.get("fixture_pattern", "")).strip()
            entry["reason"] = str(item.get("reason", "")).strip()
            cleaned.append(entry)
        if not cleaned:
            raise ValueError("引擎B输出 scores 数组无有效条目")
        return cleaned

    def _run_engine_b(
        self,
        cases: List[Dict[str, Any]],
        rules_full: str = "",
        rule_rows: Optional[Dict[str, List[int]]] = None,
    ) -> Tuple[Dict[str, Dict[str, Any]], List[int], List[str], Dict[str, Any]]:
        """
        分片调用 LLM 打分。

        :param rule_rows: 产物 segment_map 的 {编号: [用例行号]}；给了就「按编号归属装箱」，
                          让同一编号的用例落在同一片（消除 B3/B4 打分的片间漂移）。
        :return: (b_results, failed_segments, unmapped_names, packing)
          b_results: {norm_用例名: {B1..B4, automatability, fixture_pattern, reason}}
          failed_segments: 评审失败的片号列表（重试一次后仍失败）
          unmapped_names: 模型返回了但与输入对不上的用例名（模型改写名称的证据）
          packing: 本次分片方式与装箱统计（进报告，便于排查分数漂移）
        """
        b_results: Dict[str, Dict[str, Any]] = {}
        failed_segments: List[int] = []
        unmapped: List[str] = []

        segments, packing = self._pack_segments(cases, rule_rows)
        logger.info(
            f"🧩 引擎B分片 | 方式={packing['mode']} | 片数={packing['segments']} | "
            f"用例={len(cases)}条 | 上限={self.SEGMENT_SIZE}条/片 | "
            f"跨片编号={packing['split_rules'] or '无'}"
        )
        for seg_idx, seg in enumerate(segments):
            prompt = self._build_segment_prompt(seg, rules_full)
            parsed = None
            for attempt in (1, 2):  # 解析失败重试一次（方案 2.4 第 3 条）
                try:
                    raw = self.call_with_guard(prompt)
                    parsed = self._parse_segment_json(raw)
                    break
                except Exception as e:
                    logger.warning(
                        f"⚠️ 引擎B第{seg_idx + 1}片第{attempt}次失败: {str(e)[:120]}"
                    )
            if parsed is None:
                failed_segments.append(seg_idx + 1)
                logger.error(f"❌ 引擎B第{seg_idx + 1}片评审失败（{len(seg)}条将只有引擎A分数）")
                continue

            seg_norms = {_norm_case_name(str(c.get("用例名称", ""))) for c in seg}
            for item in parsed:
                norm = _norm_case_name(item["name"])
                if norm in seg_norms:
                    b_results[norm] = item
                else:
                    unmapped.append(item["name"])
            logger.info(
                f"🧠 引擎B第{seg_idx + 1}/{len(segments)}片完成 | 命中={len(parsed)}条"
            )

        if unmapped:
            logger.warning(
                f"⚠️ 引擎B返回了 {len(unmapped)} 个与输入对不上的用例名（已丢弃）: "
                f"{unmapped[:3]}"
            )
        return b_results, failed_segments, unmapped, packing

    # ==================================================================
    # 分片装箱：按规则编号归属，避免同一编号的用例被拆到两片
    # ==================================================================
    @classmethod
    def _pack_segments(cls, cases: List[Dict[str, Any]],
                       rule_rows: Optional[Dict[str, List[int]]]
                       ) -> Tuple[List[List[Dict[str, Any]]], Dict[str, Any]]:
        """
        把用例切成引擎 B 的评审片。

        背景（2026-09-30 查明）：生成线按**规则编号**分片（每片 6 个编号），
        评审线按**用例条数**切（每片 12 条），两者不对齐 ⇒ 同一编号的用例被拆到两片，
        B3（业务正确性）/B4（覆盖价值）在不同片里的参照系不同，产生片间漂移。

        这里不做「对齐生成分片」（不可行，也不必要：生成怕输出超限、评审怕输入超限，
        约束不同），而是解决漂移的**根因** —— **同一编号的用例绝不跨片**：

        - 顺序遍历用例，若当前用例与「当前片已收编号」有交集 ⇒ 必须并入当前片（即使已满）；
        - 否则当前片满 `SEGMENT_SIZE` 条就开新片；
        - 硬并会让片超过 `PACK_MAX_SIZE` 时强制切，并把被拆的编号如实记入 `split_rules`
          （不静默：报告里会提示这些编号的分数可能漂移）。
        - 无 `rule_rows`（旧产物 / 注释行已失效）⇒ 退回顺序切分，并如实标注 mode=sequential。

        :return: (segments, packing_info)
        """
        n = len(cases)
        if not rule_rows:
            segs = [cases[i:i + cls.SEGMENT_SIZE] for i in range(0, n, cls.SEGMENT_SIZE)]
            return segs, {"mode": "sequential", "segments": len(segs), "split_rules": [],
                          "pack_max": cls.PACK_MAX_SIZE, "rule_count": 0,
                          "rows_without_rule": n}

        row_ids = segment_map.rule_ids_by_row(rule_rows, case_count=n)
        segments: List[List[Dict[str, Any]]] = []
        cur: List[Dict[str, Any]] = []
        cur_rules: set = set()
        split_rules: List[str] = []

        for idx, case in enumerate(cases):
            rids = set(row_ids.get(idx + 1, []))
            if cur:
                need_join = bool(rids & cur_rules)          # 同编号 ⇒ 必须同片
                if need_join and len(cur) >= cls.PACK_MAX_SIZE:
                    # 同编号的用例在产物里相隔太远，硬并会撑爆单片输入预算：
                    # 强制切，并如实记录被拆的编号（打分可能漂移，需人工知悉）
                    segments.append(cur)
                    split_rules.extend(sorted(rids & cur_rules))
                    cur, cur_rules = [], set()
                elif not need_join and len(cur) >= cls.SEGMENT_SIZE:
                    segments.append(cur)
                    cur, cur_rules = [], set()
            cur.append(case)
            cur_rules |= rids

        if cur:
            segments.append(cur)

        packing = {
            "mode": "by_rule",
            "segments": len(segments),
            "split_rules": sorted(set(split_rules)),
            "pack_max": cls.PACK_MAX_SIZE,
            "rule_count": len(rule_rows),
            # ⚠️ 语义务必准确：这里统计的是「不含编号归属的用例」，主要是**通用交互类**
            # （设计上本就不归属任何 R/S 编号），只有少数是「专属用例漏标注编号」。
            # 真正的「专属用例归属不明」条数由 segment_map 的 unassigned_rows 单独回报。
            "rows_without_rule": n - len(row_ids),
        }
        return segments, packing

    # ==================================================================
    # 主入口：双引擎合成
    # ==================================================================
    def score_cases(
        self,
        cases: List[Dict[str, Any]],
        module: str = "",
        page: str = "",
        use_engine_b: bool = True,
        rules_full: str = "",
        prev_scores: Optional[Dict[str, Any]] = None,
        rule_rows: Optional[Dict[str, List[int]]] = None,
    ) -> Dict[str, Any]:
        """
        对结构化用例做量化评审。

        :param cases: parse_markdown_cases 的结构化用例
        :param use_engine_b: False=只跑引擎 A（纯代码，零模型调用）
        :param rules_full: 模块规则全文（B3 依据）；空则由内部按 (module,page) 自取
        :param prev_scores: 上一次同版本评分（分数漂移检测用），{norm: {"total": int}}
        :param rule_rows: 产物 segment_map 的 {编号: [用例行号]}，用于引擎 B「按编号归属装箱」；
                          不传/为空 ⇒ 退回按条数顺序分片（旧行为）
        :return: {"cases": [逐条结果], "failed_segments", "unmapped_names", "packing",
                  "engine_b": bool, "summary": {...}}
        """
        if not cases:
            return {"cases": [], "failed_segments": [], "unmapped_names": [],
                    "packing": {"mode": "none", "segments": 0, "split_rules": []},
                    "engine_b": bool(use_engine_b), "summary": self._build_summary([])}

        # ---- 引擎 A（必跑）----
        a_results = self.run_engine_a(cases, module, page)

        # ---- 引擎 B（可跳过）----
        b_results: Dict[str, Dict[str, Any]] = {}
        failed_segments: List[int] = []
        unmapped: List[str] = []
        packing: Dict[str, Any] = {"mode": "skipped", "segments": 0, "split_rules": []}
        if use_engine_b:
            if not rules_full.strip() and module:
                try:
                    rules_full = self.vector_db.get_module_rules_full(
                        module, page=page or None) or ""
                except Exception as e:
                    logger.warning(f"⚠️ 引擎B：规则全文获取失败（B3 将按锚点降级）| {str(e)}")
            b_results, failed_segments, unmapped, packing = self._run_engine_b(
                cases, rules_full, rule_rows=rule_rows)

        # ---- 逐条合成 ----
        prev_cases = (prev_scores or {}).get("cases") or {}
        results = []
        for case in cases:
            name = str(case.get("用例名称", "")).strip()
            norm = _norm_case_name(name) or f"__unnamed_{id(case)}"
            a = a_results.get(norm, {"hard_issues": [], "deduction": 0, "has_high": False})
            b = b_results.get(norm)

            if b:
                dim_total = round(sum(
                    b[d] / 5 * w for d, w in self.DIM_WEIGHTS.items()))
            else:
                dim_total = None  # 引擎 B 未覆盖（跳过或该片失败）
            base = dim_total if dim_total is not None else 100
            total = max(0, min(100, base - a["deduction"]))

            if a["has_high"] or total < self.DELETE_THRESHOLD:
                decision = "suggest_delete"
            elif total >= self.KEEP_THRESHOLD:
                decision = "keep"
            else:
                decision = "improve"

            # 可自动化性标签
            if b and b.get("automatability") in self.VALID_AUTOMATABILITY:
                automatability = b["automatability"]
            elif a["has_high"]:
                automatability = "不可自动化（硬伤）"
            elif any(i["severity"] == "medium" and i["metric"] == "A7"
                     for i in a["hard_issues"]):
                automatability = "需改造后自动化"
            elif use_engine_b:
                automatability = "未评（引擎B本片失败）"
            else:
                automatability = "未评（引擎B跳过）"

            # 分数漂移检测（跨批次重评）
            drift = False
            prev_total = (prev_cases.get(norm) or {}).get("total")
            if isinstance(prev_total, (int, float)) and abs(total - prev_total) > self.DRIFT_DELTA:
                drift = True

            results.append({
                "name": name,
                "norm": norm,
                "total": total,
                "dims": {d: b[d] for d in self.DIM_WEIGHTS} if b else {},
                "a_deduction": a["deduction"],
                "hard_issues": a["hard_issues"],
                "automatability": automatability,
                "fixture_pattern": (b or {}).get("fixture_pattern", ""),
                "decision": decision,
                "reason": (b or {}).get("reason", ""),
                "engine_b_scored": bool(b),
                "score_drift": drift,
                "prev_total": prev_total if isinstance(prev_total, (int, float)) else None,
            })

        # 按总分升序排（最差的排前面，方便人工先看）
        results.sort(key=lambda r: r["total"])
        summary = self._build_summary(results, failed_segments)
        logger.info(
            f"✅ 用例评审完成 | 共{len(results)}条 | 引擎B={'开' if use_engine_b else '关'} | "
            f"保留={summary['keep']} | 待改进={summary['improve']} | 建议删除={summary['suggest_delete']} | "
            f"均分={summary['avg']}"
        )
        return {
            "cases": results,
            "failed_segments": failed_segments,
            "unmapped_names": unmapped,
            "packing": packing,
            "engine_b": bool(use_engine_b),
            "summary": summary,
        }

    def _build_summary(self, results: List[Dict[str, Any]],
                       failed_segments: List[int] = None) -> Dict[str, Any]:
        counter = Counter(r["decision"] for r in results)
        totals = [r["total"] for r in results]
        return {
            "total_cases": len(results),
            "keep": counter.get("keep", 0),
            "improve": counter.get("improve", 0),
            "suggest_delete": counter.get("suggest_delete", 0),
            "avg": round(sum(totals) / len(totals), 1) if totals else 0,
            "hard_issue_count": sum(len(r["hard_issues"]) for r in results),
            "high_issue_cases": sum(1 for r in results if any(
                i["severity"] == "high" for i in r["hard_issues"])),
            "failed_segments": failed_segments or [],
            "drift_cases": sum(1 for r in results if r.get("score_drift")),
        }

    # ==================================================================
    # 评审报告渲染（Markdown，落 report/ 目录）
    # ==================================================================
    def render_report(
        self,
        score_result: Dict[str, Any],
        module: str = "",
        page: str = "",
        version: str = "",
        source_desc: str = "",
    ) -> str:
        """把 score_cases 的结果渲染成 Markdown 评审报告"""
        cases = score_result.get("cases", [])
        summary = score_result.get("summary", {})
        engine_b = score_result.get("engine_b")
        packing = score_result.get("packing") or {}
        seg_size = self.SEGMENT_SIZE
        if packing.get("mode") == "by_rule":
            pack_desc = (
                f"按编号归属装箱（{packing.get('segments', 0)} 片，每片 ≤ {seg_size} 条，"
                f"硬上限 {packing.get('pack_max')} 条；同编号用例不跨片，"
                f"共 {packing.get('rule_count', 0)} 个编号，"
                f"其余 {packing.get('rows_without_rule', 0)} 条为通用交互类（不含编号归属，不参与装箱））"
            )
        elif engine_b:
            pack_desc = (
                f"按产物顺序切分（{packing.get('segments', 0)} 片，每片 ≤ {seg_size} 条）"
                f"—— 产物无有效 segment_map，同一编号的用例可能被拆到不同片，B3/B4 存在片间漂移风险"
            )
        else:
            pack_desc = "未分片（引擎 B 跳过）"
        lines = [
            f"# 用例质量评审报告",
            "",
            f"- 评审对象：{source_desc or '未说明'}",
            f"- 业务模块：{module or '未指定'}　页面：{page or '模块层'}　版本：{version or '-'}",
            f"- 评审引擎：A（代码硬指标，必跑）"
            f"{' + B（LLM rubric 打分）' if engine_b else '（引擎 B 跳过，总分=100−硬伤扣分）'}",
            f"- 引擎B分片方式：{pack_desc}",
            f"- 分档阈值：≥{self.KEEP_THRESHOLD} 保留 / {self.DELETE_THRESHOLD}~{self.KEEP_THRESHOLD - 1} 待改进 / "
            f"<{self.DELETE_THRESHOLD} 或命中高危硬伤 → 建议删除",
            "",
            f"## 一、总览",
            "",
            f"| 指标 | 值 |",
            f"|---|---|",
            f"| 用例总数 | {summary.get('total_cases', 0)} |",
            f"| 平均分 | {summary.get('avg', 0)} |",
            f"| ✅ 保留（≥{self.KEEP_THRESHOLD}） | {summary.get('keep', 0)} |",
            f"| ⚠️ 待改进（{self.DELETE_THRESHOLD}~{self.KEEP_THRESHOLD - 1}） | {summary.get('improve', 0)} |",
            f"| ❌ 建议删除（<{self.DELETE_THRESHOLD} 或高危硬伤） | {summary.get('suggest_delete', 0)} |",
            f"| 硬伤总数 | {summary.get('hard_issue_count', 0)}（含高危用例 {summary.get('high_issue_cases', 0)} 条） |",
            f"| 分数漂移（与上次评审差 >{self.DRIFT_DELTA} 分） | {summary.get('drift_cases', 0)} |",
        ]
        split_rules = packing.get("split_rules") or []
        if split_rules:
            lines += [
                "",
                f"> ⚠️ 以下编号的用例跨度超过单片硬上限（{packing.get('pack_max')} 条），已被强制拆到不同片，"
                f"其 B3/B4 分数可能与同编号其他用例不可比，请人工复核：**{'、'.join(split_rules)}**",
            ]
        failed = score_result.get("failed_segments") or []
        if failed:
            lines += [
                f"| 评审失败的引擎B分片 | 第 {'、'.join(str(s) for s in failed)} 片"
                f"（这些用例只有引擎A分数，建议重评） |",
            ]
        unmapped = score_result.get("unmapped_names") or []
        if unmapped:
            lines += [
                "",
                f"> ⚠️ 引擎B返回了 {len(unmapped)} 个与输入对不上的用例名（已丢弃不计分）："
                + "、".join(unmapped[:10]),
            ]

        def _case_table(rows, with_issues=True):
            out = [
                "| 用例名称 | 总分 | B1步骤 | B2预期 | B3业务 | B4覆盖 | A扣分 | 可自动化性 | 理由 |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for r in rows:
                d = r.get("dims") or {}
                reason = (r.get("reason") or "").replace("|", "\\|")
                nm = (r.get("name") or "").replace("|", "\\|")
                drift_mark = f"（上次 {r['prev_total']}，漂移⚠️）" if r.get("score_drift") else ""
                out.append(
                    f"| {nm} | {r['total']}{drift_mark} | {d.get('B1', '-')} | {d.get('B2', '-')} | "
                    f"{d.get('B3', '-')} | {d.get('B4', '-')} | {r.get('a_deduction', 0)} | "
                    f"{r.get('automatability', '-')} | {reason} |"
                )
                if with_issues:
                    for issue in r.get("hard_issues", []):
                        excerpt = (issue.get("excerpt") or "").replace("|", "\\|")
                        out.append(
                            f"| ↳ 硬伤[{issue['metric']}/{issue['severity']}] "
                            f"{issue['detail'].replace('|', chr(92) + '|')} | | | | | | | | {excerpt} |"
                        )
            return out

        deletes = [r for r in cases if r["decision"] == "suggest_delete"]
        improves = [r for r in cases if r["decision"] == "improve"]
        keeps = [r for r in cases if r["decision"] == "keep"]

        lines += ["", f"## 二、建议删除清单（{len(deletes)} 条，需人工勾选确认后才生效）", ""]
        lines += _case_table(deletes) if deletes else ["（无）"]
        lines += ["", f"## 三、待改进清单（{len(improves)} 条）", ""]
        lines += _case_table(improves) if improves else ["（无）"]
        lines += ["", f"## 四、保留清单（{len(keeps)} 条）", ""]
        lines += _case_table(keeps, with_issues=False) if keeps else ["（无）"]
        lines += [
            "",
            "---",
            "> 删留说明：本报告中的「建议删除」不会自动生效。人工确认后，差评用例将从**新版本基线**中移除"
            "（旧版本仍可追溯），删除原因记录于评分旁挂文件。",
        ]
        return "\n".join(lines)


# ==================================================================
# 模块级工具：从 Markdown 用例表中剔除指定用例行（评审删除 / 沉淀剔除共用）
# ==================================================================
def remove_cases_from_markdown(md_content: str, removed_names: List[str]) -> Tuple[str, int]:
    """
    按「用例名称」从 9 列 Markdown 用例表中剔除指定行（归一化匹配，与归并同一套键）。

    只剔除数据行：表头行（首列含「用例名称」）与分隔行（全 -、:、空格）永远保留；
    自检表等首列不在删除清单的行不受影响。

    :return: (剔除后的 markdown, 实际剔除行数)
    """
    removed_norms = {_norm_case_name(n) for n in (removed_names or []) if str(n).strip()}
    if not removed_norms:
        return md_content, 0

    kept_lines = []
    removed_count = 0
    for line in (md_content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = split_md_row(stripped)
            first = cells[0].strip() if cells else ""
            # 表头/分隔行直接保留
            is_header = "用例名称" in first
            is_sep = bool(re.fullmatch(r"[-:\s|]+", stripped))
            if not is_header and not is_sep and first:
                if _norm_case_name(first) in removed_norms:
                    removed_count += 1
                    continue
        kept_lines.append(line)

    new_md = "\n".join(kept_lines)

    # 行号已失效 ⇒ segment_map 必须重算：
    # 不重算而沿用旧注释行，评审线会按**错的**行号装箱（比没有映射更糟）；
    # 直接删掉也不行 —— 之后每次评审都退回顺序分片，同编号漂移问题复现。
    if removed_count and segment_map.find_marker(new_md):
        try:
            rows = segment_map.collect_case_rows(new_md)
            payload = segment_map.build(rows, source="recompute")
            new_md = segment_map.upsert_marker(new_md, payload)
            logger.info(
                f"🧩 用例行已变动 {removed_count} 条，segment_map 已重算 | "
                f"归属编号={len((payload or {}).get('rule_rows') or {})}个 | "
                f"用例={len(rows)}条"
                + ("" if payload else "（无备注标注，映射已移除，评审将退回顺序分片）")
            )
        except Exception as e:
            logger.warning(f"⚠️ segment_map 重算失败，已移除旧注释行（评审将退回顺序分片）: {str(e)}")
            new_md = segment_map.upsert_marker(new_md, None)

    return new_md, removed_count
