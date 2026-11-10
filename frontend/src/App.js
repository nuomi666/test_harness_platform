import React, { useState, useEffect, useRef } from 'react';
import { message, Modal, Layout, Menu } from 'antd';
const { Sider, Content } = Layout;
import { AppstoreOutlined, FolderOpenOutlined, FileSearchOutlined, SettingOutlined, RobotOutlined, ApiOutlined } from '@ant-design/icons';
import { uploadDoc, generateReview, generateCases, generateDirect, reGenCase, exportCase, precipitateAsset, generateIncremental, analyzeImpact, getSkillStatus, listOutputFiles, getModuleVersions, saveClarifications, listRuleFiles, proposeRulePatch, applyRulePatch, confirmDeletions, scoreCases, getCaseScores, listAllRuleFiles, getRuleFileTarget, getRuleFileContent, saveRuleFile, uploadRuleFile, deleteAsset, saveAssetContent, getAssetFile, getRuleTemplate } from './api';
import { MODULE_LIST, PAGE_LIST, labelOfModule, labelOfPage, scopeLabel, REPORT_ASSET_TYPES } from './config/scopes';
// 功能域与公共组件（批2a 从本文件拆出）
import ErrorBoundary from './components/ErrorBoundary';
import ScopeBar from './components/ScopeBar';
import GeneratePage from './pages/GeneratePage';
import AssetsPage from './pages/AssetsPage';
import ReportsPage from './pages/ReportsPage';
import RulesPage from './pages/RulesPage';

// 业务模块 / 页面清单已抽到 config/scopes.js（唯一配置源）：
// 新增模块或页面 = 改那一个文件的一行，全平台所有下拉/导航/作用域条自动跟随。

// /api/module-versions 返回 { data: 版本号字符串数组, items: [{page,version,case_total,create_time}] }
// 统一按 items 结构消费，避免某些调用点误用 data（那是字符串数组，读 .version 会是 undefined，
// 提示就会显示成「最新 -，约 ? 条用例」）
const normalizeVersionItems = (payload) => {
  const d = payload || {};
  if (Array.isArray(d.items) && d.items.length) return d.items;
  return (Array.isArray(d.data) ? d.data : []).map(v => ({ version: v }));
};


// ==================== 一级导航（按功能域，不按动作）====================
// 维度从「怎么做」（生成模式）改为「管什么」（资产/能力域）：
// 后续新增 UI 自动化、接口自动化是**加一个域**，而不是往已有模式里再塞一块。
const NAV_ITEMS = [
  { key: 'generate', icon: <AppstoreOutlined />, label: '用例生成' },
  { key: 'assets', icon: <FolderOpenOutlined />, label: '用例资产' },
  { key: 'reports', icon: <FileSearchOutlined />, label: '报告中心' },
  { key: 'rules', icon: <SettingOutlined />, label: '规则管理' },
  { type: 'divider' },
  { key: 'ui-auto', icon: <RobotOutlined />, label: 'UI 自动化（待建）', disabled: true },
  { key: 'api-auto', icon: <ApiOutlined />, label: '接口自动化（待建）', disabled: true },
];

// hash 可落到的一级域（刷新/回退不丢当前域）
const NAV_KEYS = ['generate', 'assets', 'reports', 'rules'];


function App() {
  // ========== 全局基础状态 ==========
  const [loading, setLoading] = useState(false);
  const [currentMode, setCurrentMode] = useState('');
  const [requirement, setRequirement] = useState('');
  // ===== 作用域（模块 + 页面）**全局唯一** =====
  // 所有产物（用例、报告、基线、评分）都以 (module, page) 为归属锚点，它天然唯一，
  // 不存在「生成用 A、沉淀用 B」的正当场景 ⇒ 提升为全局唯一作用域：
  //   ① 切域不用重选；② 三套并存 state（生成/增量/资产）合并为一套，状态串味根因消失。
  // ⚠️ 风险缓解：顶部常显当前作用域 + 危险动作按钮回显目标作用域 + 沉淀保留二次确认。
  const [selectedModule, setSelectedModule] = useState('');
  const [selectedPage, setSelectedPage] = useState('');
  // 一级导航（功能域）；初值从 hash 恢复，刷新不丢当前域
  const [activeNav, setActiveNav] = useState(() => {
    const h = (window.location.hash || '').replace('#', '');
    return NAV_KEYS.includes(h) ? h : 'generate';
  });
  // 派生别名：增量域 / 资产域原先是各自独立的 module+page state，现统一指向全局作用域。
  // 保留旧变量名 = 存量使用点一行不用改，且结构上不可能再出现「两套作用域不一致」。
  const incrementalModule = selectedModule;
  const incrementalPage = selectedPage;
  const historyAssetModule = selectedModule;
  const historyAssetPage = selectedPage;
  const [exportFormat, setExportFormat] = useState('markdown');
  const [historyUsed, setHistoryUsed] = useState(false);

  // 上传文件内容联动
  const [uploadContent, setUploadContent] = useState('');
  const [uploadFileName, setUploadFileName] = useState('');

  // 流程公共数据
  const [slug, setSlug] = useState('');
  const [flowId, setFlowId] = useState('');
  const [reviewReport, setReviewReport] = useState('');
  const [testCases, setTestCases] = useState('');
  const [caseFilePath, setCaseFilePath] = useState('');
  const [supplement, setSupplement] = useState('');

  // 完整模式专用步骤
  const [currentStep, setCurrentStep] = useState(0);

  // 沉淀选择状态（选类型不点按钮不触发）
  const [casePrecipitateType, setCasePrecipitateType] = useState('');
  const [reportPrecipitateType, setReportPrecipitateType] = useState('');

  // ========== 历史产物「补沉淀」状态（忘了沉淀时事后补，无需重新生成）==========
  const [outputFiles, setOutputFiles] = useState([]);
  const [outputFilesLoading, setOutputFilesLoading] = useState(false);
  // 按「模块+页面」筛完后没有任何产物 → 明确提示「找不到相关页面的用例产物」
  const [outputFilesEmpty, setOutputFilesEmpty] = useState(false);
  const [historyAssetSlug, setHistoryAssetSlug] = useState('');
  const [historyAssetType, setHistoryAssetType] = useState('');
  const [historyAssetVersion, setHistoryAssetVersion] = useState('');
  const [historyAssetNextVersion, setHistoryAssetNextVersion] = useState('');
  const [historyPrecipitateType, setHistoryPrecipitateType] = useState('');
  const [historyPrecipitating, setHistoryPrecipitating] = useState(false);
  // 「用例资产」域：选中产物的预览内容（改磁盘文件后可重新加载刷新）
  const [assetPreview, setAssetPreview] = useState('');
  // sidecar 评分（{module}_case_scores.json）：一次拉全模块，按版本键映射到产物行，
  // 让「哪份产物有硬伤」不用重跑评审就能看见
  const [caseScores, setCaseScores] = useState(null);

  // ========== 报告中心（report/ 目录下的报告类资产）==========
  const [reportFiles, setReportFiles] = useState([]);
  const [reportFilesLoading, setReportFilesLoading] = useState(false);
  const [reportPreview, setReportPreview] = useState(null);   // {slug, asset_type, content}

  // ========== 用例质量评审（case-quality-scorer）==========
  // caseReviewResult：最近一次评审结果（含逐条分数与 suggest_delete 清单）
  // caseReviewChecked：人工勾选确认删除的用例名（默认全勾 suggest_delete，沉淀时随请求剔除）
  // caseReviewUseEngineB：false=纯代码硬指标体检（零模型调用，秒回）
  const [caseReviewResult, setCaseReviewResult] = useState(null);
  const [caseReviewLoading, setCaseReviewLoading] = useState(false);
  const [caseReviewChecked, setCaseReviewChecked] = useState([]);
  const [caseReviewUseEngineB, setCaseReviewUseEngineB] = useState(true);
  // 完整模式：评审时锁定的「模块+页面」。第二步若与评审时不一致会提醒，
  // 避免「评审记在 A 页面、用例记在 B 页面」的归属错位
  const [reviewScope, setReviewScope] = useState(null);

  // ========== 评审模式：历史用例带入 ==========
  // reviewWithHistory=true → history_mode=auto（选了模块/页面就召回该模块/页面的历史用例，
  // 由后端精简后再注入，防 token 超限）；false → history_mode=off（当全新需求评审）
  const [reviewWithHistory, setReviewWithHistory] = useState(true);
  // 本次评审的历史用例带入结果（由接口回传，供报告卡顶部如实展示）
  const [reviewHistoryInfo, setReviewHistoryInfo] = useState(null);

  // ========== 增量迭代模式专用状态 ==========
  // 注意：module/page 不再在此维护（已并入全局作用域，见上方派生别名）
  const [historyVersion, setHistoryVersion] = useState('');
  const [historyVersionOptions, setHistoryVersionOptions] = useState([]);
  const [historyVersionLoading, setHistoryVersionLoading] = useState(false);
  const [changeDescription, setChangeDescription] = useState('');
  const [incrementalCases, setIncrementalCases] = useState('');
  const [mergedFullCases, setMergedFullCases] = useState('');
  const [incrementalLoading, setIncrementalLoading] = useState(false);
  const [impactAnalysis, setImpactAnalysis] = useState('');
  const [impactLoading, setImpactLoading] = useState(false);
  // 疑点闭环：影响面分析产出的「待确认疑点」+ 用户逐条答复 + 保存中标志
  const [pendingQuestions, setPendingQuestions] = useState([]);
  const [clarificationAnswers, setClarificationAnswers] = useState({});
  const [savingClarifications, setSavingClarifications] = useState(false);
  // 删除确认闸门：模型「建议删除」的用例默认不删（全部保留在产物里），等人工勾选
  const [mergeMeta, setMergeMeta] = useState(null);            // 后端归并元信息（含 pending_deletions）
  const [deletionChecked, setDeletionChecked] = useState({});  // {用例名: true/false}
  const [deletionApplying, setDeletionApplying] = useState(false);
  const [deletionResolved, setDeletionResolved] = useState(''); // '' | 'kept' | 'applied'

  // 方案C：规则补丁（生成 → 人工确认 → 定点落盘）
  const [ruleFiles, setRuleFiles] = useState([]);
  const [rulePatches, setRulePatches] = useState([]);
  const [patchChecked, setPatchChecked] = useState({});     // {补丁id: true/false}
  const [patchLoading, setPatchLoading] = useState(false);
  const [patchApplying, setPatchApplying] = useState(false);
  const [patchResult, setPatchResult] = useState(null);
  const [patchSummary, setPatchSummary] = useState('');
  // 规则补丁搬到「规则管理」域后，变更描述/影响面分析可在本域独立输入
  //（不填时回退用「用例生成 → 增量迭代」域里已填的值，保持旧路径可用）
  const [rulePatchChangeDesc, setRulePatchChangeDesc] = useState('');
  const [rulePatchImpact, setRulePatchImpact] = useState('');

  // ========== 规则管理域：规则文件浏览 / 上传 / 替换 / 编辑 ==========
  const [ruleFilesAll, setRuleFilesAll] = useState([]);              // references 下全部规则文件
  const [ruleFilesAllLoading, setRuleFilesAllLoading] = useState(false);
  const [ruleModal, setRuleModal] = useState(null);                  // {filename, rel, biz_module, page, content...}
  const [ruleModalMode, setRuleModalMode] = useState('view');         // view | edit
  const [ruleModalContent, setRuleModalContent] = useState('');
  const [ruleSaving, setRuleSaving] = useState(false);
  const [rulePrecheck, setRulePrecheck] = useState(null);            // 上传预检结果（弹窗二选一用）

  // ========== 产物编辑（用例资产域）==========
  const [assetEdit, setAssetEdit] = useState(null);                  // {slug, asset_type, filename}
  const [assetEditContent, setAssetEditContent] = useState('');
  const [assetSaving, setAssetSaving] = useState(false);

  // ========== Skill执行状态轮询（展示子Skill执行进度）==========
  const [skillStates, setSkillStates] = useState({});
  const pollTimerRef = useRef(null);

  // 当前生效的业务模块 / 页面（适配不同模式）
  const currentModule = currentMode === 'incremental' ? incrementalModule : selectedModule;
  const currentPage = currentMode === 'incremental' ? incrementalPage : selectedPage;

  // ---------- Skill状态轮询控制 ----------
  const stopSkillPolling = () => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  const startSkillPolling = (fid) => {
    if (!fid) return;
    stopSkillPolling();
    setSkillStates({});

    const poll = async () => {
      try {
        const res = await getSkillStatus(fid);
        const data = res?.data?.data;
        if (data && Object.keys(data).length > 0) {
          setSkillStates(data);
        }
      } catch (e) {
        // 轮询失败不打断主生成流程，静默忽略
      }
    };

    poll(); // 立即拉一次，避免首屏空白
    pollTimerRef.current = setInterval(poll, 1500);
  };

  // 生成结束后拉取一次最终状态（展示各Skill耗时/重试次数/成败）
  // 说明：生成接口为同步返回，无法做到生成中的实时进度，
  //       此处展示每个子Skill的执行结果，便于定位慢在哪一步、是否有重试
  const fetchSkillStates = async (fid) => {
    if (!fid) return;
    try {
      const res = await getSkillStatus(fid);
      const data = res?.data?.data;
      setSkillStates(data && Object.keys(data).length > 0 ? data : {});
    } catch (e) {
      // 状态拉取失败不影响主流程结果展示，静默忽略
    }
  };

  // 组件卸载时清理定时器，防止内存泄漏
  useEffect(() => stopSkillPolling, []);

  // 启动时拉一次历史产物清单（供「补沉淀」下拉使用）
  useEffect(() => { loadOutputFiles(); }, []);

  // 作用域（模块 / 页面）变更 —— 全局唯一，各域共用这一处 ==========
  // 重置所有以 (module, page) 为键的下游选择，避免残留旧作用域的数据：
  // 历史版本 / 规则补丁 / 选中产物 / 产物版本号
  const resetScopeDependents = () => {
    setHistoryVersion('');          // 换作用域后旧版本不再适用
    setRulePatches([]);             // 换作用域后旧补丁不再适用
    setPatchResult(null);
    setRulePatchChangeDesc('');     // 变更描述是「针对某页面」的，换作用域后不应残留
    setRulePatchImpact('');
    setRulePrecheck(null);          // 上传预检结果绑定旧作用域，作废
    setHistoryAssetSlug('');        // 换作用域后旧产物不再属于当前归属
    setHistoryAssetType('');
    setHistoryAssetVersion('');
    setAssetPreview('');
  };

  // 切换模块：清空页面（避免残留不属于新模块的 page）+ 重置下游
  const handleModuleChange = (v) => {
    setSelectedModule(v || '');
    setSelectedPage('');
    resetScopeDependents();
  };

  // 切换页面：只重置下游，模块不动
  const handleScopePageChange = (v) => {
    setSelectedPage(v || '');
    resetScopeDependents();
  };

  // 作用域（或当前域）变化 → 按当前域刷新依赖数据。
  // 取代原先「各域 onChange 里各调一遍加载函数」的写法：作用域只有一处，加载也只在这里。
  // 依赖只列 4 个作用域/域相关值；各 load* 是本组件内的稳定函数，无须进依赖表
  //（本项目的 eslint 配置未启用 react-hooks 插件，故不写 disable 注释）。
  useEffect(() => {
    const mod = selectedModule;
    if (!mod) return;
    const pg = selectedPage || '';
    if (activeNav === 'assets') {
      loadOutputFiles(mod, pg);
      loadHistoryAssetNextVersion(mod, pg);
      loadCaseScores(mod);
    }
    if (activeNav === 'reports') {
      loadReportFiles(mod, pg);
    }
    if (activeNav === 'generate' && currentMode === 'incremental') {
      loadHistoryVersions(mod, pg);
      loadRuleFiles(mod, pg);
    }
    if (activeNav === 'rules') {
      // 规则域：① 当前作用域精确命中的规则（生成时实际注入的）② references 下全部规则
      loadRuleFiles(mod, pg);
      loadAllRuleFiles();
    }
  }, [selectedModule, selectedPage, activeNav, currentMode]);

  // 一级导航 hash 同步：刷新/分享链接不丢当前域
  useEffect(() => {
    window.location.hash = activeNav;
  }, [activeNav]);

  useEffect(() => {
    const onHash = () => {
      const h = (window.location.hash || '').replace('#', '');
      if (NAV_KEYS.includes(h)) setActiveNav(h);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // ========== 拉取「已沉淀版本」列表：只列出真实存在基线的版本 ==========
  // 之前是自由输入框，写错版本号会静默回退向量检索（用户无感知）；
  // 现改为从后端已沉淀数据拉取，选不到 = 没有基线，一目了然。
  const loadHistoryVersions = async (module, page) => {
    if (!module) {
      setHistoryVersionOptions([]);
      return;
    }
    setHistoryVersionLoading(true);
    try {
      const res = await getModuleVersions(module, page || '');
      const data = res.data;
      const items = (data && data.items) || [];
      setHistoryVersionOptions(items);
    } catch (e) {
      setHistoryVersionOptions([]);
    } finally {
      setHistoryVersionLoading(false);
    }
  };

  // ========== 方案C：生成规则补丁（只提议、不落盘，交人工确认）==========
  // 已搬到「规则管理」域：变更描述/影响面分析优先取本域输入，为空则回退「增量迭代」域的值
  const handleProposeRulePatch = async () => {
    if (!incrementalModule) return message.warning('请先选择业务模块');
    const changeDesc = (rulePatchChangeDesc || changeDescription || '').trim();
    const impact = (rulePatchImpact || impactAnalysis || '').trim();
    if (!changeDesc) return message.warning('请输入变更描述');
    setPatchLoading(true);
    try {
      const res = await proposeRulePatch(
        incrementalModule, incrementalPage, changeDesc, impact
      );
      const data = res.data || {};
      setPatchResult(null);
      if (data.code !== 0) {
        setRulePatches([]);
        setRuleFiles([]);
        message.error(data.message || '生成规则补丁失败');
      } else {
        const patches = (data.patches || []).map((p, i) => ({ ...p, _id: `p${i + 1}` }));
        setRulePatches(patches);
        setRuleFiles(data.rule_files || []);
        setPatchSummary(data.summary || '');
        // 默认全选，人工取消不认可的即可（这就是「人工确认闸门」）
        const init = {};
        patches.forEach(p => { init[p._id] = true; });
        setPatchChecked(init);
        if (!patches.length) {
          message.info('模型判断本次变更无需调整规则');
        } else {
          message.success(`生成 ${patches.length} 条规则补丁，请逐条确认后应用`);
        }
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `生成规则补丁失败：${detail}` : '生成规则补丁失败');
    }
    setPatchLoading(false);
  };

  // ========== 方案C：应用勾选的补丁（备份 → 定点替换 → 打「需求变更改动」标记 → 热重载）==========
  const handleApplyRulePatch = async () => {
    const selected = rulePatches.filter(p => patchChecked[p._id]);
    if (!selected.length) return message.warning('请至少勾选一条要应用的补丁');
    const changeDesc = (rulePatchChangeDesc || changeDescription || '').trim();
    setPatchApplying(true);
    try {
      const res = await applyRulePatch(
        incrementalModule, incrementalPage, selected, selected.map(p => p._id), changeDesc
      );
      const data = res.data || {};
      setPatchResult(data);
      if (data.code === 0) {
        message.success(data.message || '规则补丁已应用');
        // 补丁已落盘，清空勾选避免误重复应用；重新拉一次规则编号便于核对
        loadRuleFiles(incrementalModule, incrementalPage);
        loadAllRuleFiles();
        const init = {};
        rulePatches.forEach(p => { init[p._id] = false; });
        setPatchChecked(init);
      } else {
        message.error(data.message || '规则补丁应用失败');
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `应用规则补丁失败：${detail}` : '应用规则补丁失败');
    } finally {
      setPatchApplying(false);
    }
  };

  // ========== 拉取规则文件与编号清单（补丁确认时的人工核对依据）==========
  const loadRuleFiles = async (module, page) => {
    if (!module) {
      setRuleFiles([]);
      return;
    }
    try {
      const res = await listRuleFiles(module, page || '');
      const data = res.data || {};
      setRuleFiles(data.code === 0 ? (data.data || []) : []);
    } catch (e) {
      setRuleFiles([]);
    }
  };

  // ==================== 规则管理域（规则文件的浏览 / 上传 / 替换 / 编辑）====================
  // 概念：「上传业务 skill」= 往 skills/req-test-cases/references/ 放页面规则文件。
  //       「同页面是否已存在」判据 = front matter 的 (biz_module, page) 等值匹配（不看文件名）。
  // 保存后后端会 load_all_skills() 热重载 ⇒ 无需重启后端即生效（配套重载锁见 plugin_loader）。

  const loadAllRuleFiles = async () => {
    setRuleFilesAllLoading(true);
    try {
      const res = await listAllRuleFiles();
      const d = res.data || {};
      setRuleFilesAll(d.code === 0 ? (d.data || []) : []);
    } catch (e) {
      setRuleFilesAll([]);
    } finally {
      setRuleFilesAllLoading(false);
    }
  };

  // 打开规则文件（查看 / 编辑）：用 rel 精确定位，避免同名文件串到别的模块
  const handleOpenRuleFile = async (rec, mode = 'view') => {
    try {
      const res = await getRuleFileContent({ filename: rec.rel || rec.filename });
      const d = res.data || {};
      if (!d.ok) return message.error(d.message || '读取规则文件失败');
      setRuleModal(d.data);
      setRuleModalContent(d.data.content || '');
      setRuleModalMode(mode);
    } catch (e) {
      message.error(e?.response?.data?.detail || '读取规则文件失败');
    }
  };

  // 上传前的落盘预检：命中哪个文件 / 会新建到哪 / front matter 有没有冲突
  //（冲突不静默改写：结果交给规则域的弹窗让用户二选一）
  const handlePrecheckRuleTarget = async (payload) => {
    if (!selectedModule) {
      message.warning('请先在顶部选择业务模块（module 是规则归属的锚点）');
      return null;
    }
    try {
      const res = await getRuleFileTarget({
        moduleName: selectedModule,
        page: selectedPage || '',
        filename: payload.filename || '',
        content: payload.content || '',
        description: payload.description || '',
      });
      const t = res.data || {};
      if (!t.ok) {
        message.error(t.message || '预检失败');
        return null;
      }
      setRulePrecheck(t);
      return t;
    } catch (e) {
      message.error(e?.response?.data?.detail || '预检失败');
      return null;
    }
  };

  const clearRulePrecheck = () => setRulePrecheck(null);

  // 保存上传的规则文件（弹窗二次确认后调用）：写盘 → 备份 → 重建索引 → 热重载
  const handleUploadRuleFile = async ({ filename, content, description, frontMatterMode }) => {
    if (!selectedModule) return message.warning('请先选择业务模块');
    setRuleSaving(true);
    try {
      const res = await uploadRuleFile({
        moduleName: selectedModule,
        page: selectedPage || '',
        filename: filename || '',
        content,
        frontMatterMode,
        description: description || '',
      });
      const d = res.data || {};
      if (!d.ok) {
        message.error(d.message || '上传失败');
        return false;
      }
      message.success(
        d.message || `规则已保存（${d.action === 'replace' ? '已替换同页面规则' : '已新建'}），已自动热重载，无需重启后端`
      );
      if (d.warning) message.warning(d.warning, 10);
      setRulePrecheck(null);
      loadRuleFiles(selectedModule, selectedPage);
      loadAllRuleFiles();
      return true;
    } catch (e) {
      message.error(e?.response?.data?.detail || '上传失败');
      return false;
    } finally {
      setRuleSaving(false);
    }
  };

  // 保存「编辑」结果：target_rel 精确定位被编辑的那份文件（FM 写错时也不会误新建一份）
  // frontMatterMode='override' = 以「当前作用域」补正 front matter（用于文件缺 FM/写错模块时救回来）
  const handleSaveRuleContent = async (frontMatterMode = 'keep') => {
    if (!ruleModal) return false;
    if (!ruleModalContent.trim()) {
      message.warning('规则内容不能为空');
      return false;
    }
    const useScope = frontMatterMode === 'override';
    setRuleSaving(true);
    try {
      const res = await saveRuleFile({
        moduleName: useScope ? (selectedModule || ruleModal.biz_module || '') : (ruleModal.biz_module || ''),
        page: useScope ? (selectedPage || '') : (ruleModal.page || ''),
        filename: ruleModal.filename,
        content: ruleModalContent,
        frontMatterMode,
        targetRel: ruleModal.rel,
      });
      const d = res.data || {};
      if (!d.ok) {
        message.error(d.message || '保存失败');
        return false;
      }
      message.success(d.message || '已保存并自动热重载知识库（下次生成即刻生效，无需重启后端）');
      if (d.warning) message.warning(d.warning, 10);
      setRuleModal(prev => (prev ? { ...prev, content: d.content || ruleModalContent } : prev));
      setRuleModalMode('view');
      loadRuleFiles(selectedModule, selectedPage);
      loadAllRuleFiles();
      return true;
    } catch (e) {
      message.error(e?.response?.data?.detail || '保存失败');
      return false;
    } finally {
      setRuleSaving(false);
    }
  };

  const closeRuleModal = () => {
    setRuleModal(null);
    setRuleModalContent('');
    setRuleModalMode('view');
  };

  // ==================== 规则管理：下载业务 skill 骨架模板 ====================
  // 模板文件固定放 app/metadata/modules/业务skill模板.md（后端只读下发，可直接手工改）
  // 用途：新建一个页面的规则时，先下骨架 → 填 front matter + R/F/S 编号表 → 再走「上传规则」。
  const handleDownloadRuleTemplate = async () => {
    try {
      const res = await getRuleTemplate();
      const d = res.data || {};
      if (d.code !== 0 || !d.content) {
        message.error(d.message || '下载模板失败');
        return;
      }
      const blob = new Blob([d.content], { type: 'text/markdown;charset=utf-8' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = d.filename || '业务skill模板.md';
      a.click();
      window.URL.revokeObjectURL(url);
      message.success(`模板已下载（${d.char_count || d.content.length} 字）：填好后点「上传规则」导入`);
    } catch (e) {
      message.error(e?.response?.data?.detail || '下载模板失败');
    }
  };

  // ==================== 产物删除 / 编辑 ====================
  // ⚠️ 删除只删本地产物文件，**不动沉淀基线**（基线是权威召回源，删掉会让增量迭代静默退化成「无历史」）
  const handleDeleteAsset = (rec, afterReload) => {
    if (!rec || !rec.slug) return message.warning('请先选择要删除的文件');
    const isReport = rec.asset_type !== 'test_cases';
    Modal.confirm({
      title: isReport ? '确认删除这份报告？' : '确认删除这份用例产物？',
      content: (
        <div style={{ fontSize: 13 }}>
          文件：<b>{rec.filename}</b>
          <br />
          归属：{(MODULE_LIST.find(m => m.value === rec.module) || {}).label || rec.module || '未记录'}
          {' / '}{rec.page ? ((PAGE_LIST.find(p => p.value === rec.page) || {}).label || rec.page) : '模块层'}
          <br />
          <span style={{ color: '#cf1322' }}>
            只删本地产物文件，<b>不会</b>动已沉淀的基线（向量库 + 结构化元数据）。
            产物索引与快照由惰性 GC 自动回收。此操作不可撤销。
          </span>
        </div>
      ),
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await deleteAsset(rec.slug, rec.asset_type);
          const d = res.data || {};
          if (d.code === 0) {
            message.success(d.message || '产物已删除');
            if (rec.slug === historyAssetSlug) {
              setHistoryAssetSlug('');
              setHistoryAssetType('');
              setAssetPreview('');
            }
            if (reportPreview && reportPreview.slug === rec.slug) setReportPreview(null);
            if (typeof afterReload === 'function') afterReload();
            loadOutputFiles();
          } else {
            message.error(d.message || '删除失败');
          }
        } catch (e) {
          message.error(e?.response?.data?.detail || '删除失败');
        }
      },
    });
  };

  // 打开产物编辑弹窗（内容按 slug 从磁盘读回，保证编辑的是最新落盘内容）
  const handleOpenAssetEdit = async (rec, afterReload) => {
    if (!rec || !rec.slug) return message.warning('请先选择产物');
    try {
      const res = await getAssetFile(rec.slug, rec.asset_type);
      const d = res.data || {};
      if (d.code !== 0) return message.error(d.message || '读取产物失败');
      setAssetEdit({ ...rec, _afterReload: afterReload });
      setAssetEditContent(d.data || '');
    } catch (e) {
      message.error(e?.response?.data?.detail || '读取产物失败');
    }
  };

  const closeAssetEdit = () => {
    setAssetEdit(null);
    setAssetEditContent('');
  };

  const handleSaveAssetContent = async () => {
    if (!assetEdit) return false;
    if (!assetEditContent.trim()) {
      message.warning('产物内容不能为空');
      return false;
    }
    setAssetSaving(true);
    try {
      const res = await saveAssetContent(assetEdit.slug, assetEdit.asset_type, assetEditContent);
      const d = res.data || {};
      if (d.code !== 0) {
        message.error(d.message || '保存失败');
        return false;
      }
      message.success('产物已保存（归属沿用原记录；如已沉淀，请重新沉淀以同步基线）');
      if (historyAssetSlug === assetEdit.slug) setAssetPreview(assetEditContent);
      const cb = assetEdit._afterReload;
      closeAssetEdit();
      if (typeof cb === 'function') cb();
      loadOutputFiles();
      return true;
    } catch (e) {
      message.error(e?.response?.data?.detail || '保存失败');
      return false;
    } finally {
      setAssetSaving(false);
    }
  };

  // ========== 上传文档 ==========
  const handleUpload = (file) => {
    uploadDoc(file).then((res) => {
      const data = res.data || {};
      const used = (data.file_text || '').length;
      message.success('文档上传成功，已纳入知识库');
      // 明确告知实际带入多少字：原先后端静默截断到 5000 字，长文档会被悄悄砍掉大半
      if (data.truncated) {
        message.warning(
          `文档较长：共提取 ${data.file_text_length || 0} 字，本次仅带入前 ${used} 字（超出部分未进入生成上下文）`
        );
      } else if (used) {
        message.info(`已带入 ${used} 字文档内容，评审与用例生成时都会用到`);
      }
      // 保存上传的文本内容，后续生成/评审时带入
      setUploadContent(data.file_text || '');
      setUploadFileName(data.filename || '');

      // 图片类文件提示补充文字
      if (/\.(png|jpg|jpeg|gif|bmp)$/i.test(file.name)) {
        message.warning('图片文件建议补充文字描述到需求框中，提升生成精准度');
      }
    }).catch(() => {
      message.error('上传失败');
    });
    return false;
  };

  // 页面/模块 中文标签（评审归属提示复用）
  const labelOfModule = (m) => (MODULE_LIST.find(x => x.value === m) || {}).label || m || '未选模块';
  const labelOfPage = (p) => (p ? ((PAGE_LIST.find(x => x.value === p) || {}).label || p) : '模块层');

  // ========== 生成评审报告 ==========
  const handleReview = async () => {
    // 需求描述与上传文档至少有一个：只上传需求文档也应能评审（新需求场景）
    if (!requirement.trim() && !uploadContent.trim()) {
      return message.warning('请输入需求描述，或上传需求文档');
    }
    setLoading(true);
    try {
      // 评审也要带页面：报告归属（ctx / 落盘 / 历史召回）与第二步保持一致
      // historyMode：auto=按模块/页面召回历史用例（后端精简后注入）；off=当全新需求评审
      const res = await generateReview(
        requirement, selectedModule, selectedPage, uploadContent, '',
        reviewWithHistory ? 'auto' : 'off'
      );
      const d = res.data || {};
      setSlug(d.slug);
      setFlowId(d.flow_id);
      setReviewReport(d.review_report);
      setHistoryUsed(d.history_used);
      // 历史用例带入情况（条数/来源/版本/精简级别/字数 + 裁剪与降级明细）
      setReviewHistoryInfo({
        count: d.history_cases_count || 0,
        source: d.history_cases_source || 'none',
        version: d.history_cases_version || '',
        scope: d.history_cases_scope || '',
        level: d.history_cases_level || '',
        chars: d.history_cases_condensed_chars || 0,
        trimmed: d.trimmed || [],
        docTruncated: !!d.doc_truncated,
        degraded: d.degraded_skills || [],
        inputChars: d.input_chars || 0,
        inputBudget: d.input_budget || 0,
        mode: d.history_mode || 'auto',
      });
      setReviewScope({
        module: selectedModule || '',
        page: selectedPage || '',
        label: `${labelOfModule(selectedModule)} / ${labelOfPage(selectedPage)}`
      });
      await fetchSkillStates(d.flow_id);
      if (currentMode === 'full') setCurrentStep(1);
      const caseNote = (d.history_cases_count || 0) > 0
        ? `，已带入 ${d.history_cases_count} 条历史用例（精简 ${d.history_cases_level || ''} 后约 ${(d.history_cases_condensed_chars || 0)} 字）`
        : '，未带入历史用例';
      // 有子Skill未产出（或编排链降级直出）时，报告缺少对应章节 —— 不能用「成功」口径提示，
      // 否则用户会把一份残缺报告当成完整评审结论使用
      const degraded = d.degraded_skills || [];
      if (degraded.length > 0) {
        message.warning(
          `评审报告已生成，但缺少部分章节（未产出：${degraded.join('、')}；归属：${labelOfPage(selectedPage)}${caseNote}），建议重试`
        );
      } else {
        message.success(`需求评审生成完成（归属：${labelOfPage(selectedPage)}${caseNote}）`);
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `需求评审生成失败：${detail}` : '需求评审生成失败');
    }
    setLoading(false);
  };

  // ========== 完整模式：基于评审生成用例 ==========
  const handleGenerateFull = async () => {
    if (!slug) return message.warning('请先生成需求评审');
    // ① 评审后改过模块/页面 → 提醒归属错位（不阻断，但要说清楚）
    if (reviewScope && (reviewScope.module !== (selectedModule || '') || reviewScope.page !== (selectedPage || ''))) {
      const nowLabel = `${(MODULE_LIST.find(m => m.value === selectedModule) || {}).label || selectedModule || '未选模块'} / ${selectedPage ? ((PAGE_LIST.find(p => p.value === selectedPage) || {}).label || selectedPage) : '模块层'}`;
      const ok = await new Promise(resolve => Modal.confirm({
        title: '模块/页面与评审时不一致',
        content: `评审报告归属：${reviewScope.label}；当前选择：${nowLabel}。本次用例会按【当前选择】生成与归属，评审报告仍留在原页面。确定继续吗？`,
        okText: '继续生成',
        cancelText: '返回调整',
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      }));
      if (!ok) return;
    }
    // ② 该页面已有基线 → 提醒「完整模式是从零全量生成，建议走增量模式」
    let versions = [];
    if (selectedModule) {
      try {
        const vr = await getModuleVersions(selectedModule, selectedPage || '');
        versions = normalizeVersionItems(vr.data);
      } catch (e) { versions = []; }
    }
    if (versions.length) {
      const latest = versions[0] || {};
      const ok = await new Promise(resolve => Modal.confirm({
        title: '该页面已沉淀用例基线',
        content: `模块/页面已沉淀 ${versions.length} 个版本（最新 ${latest.version || '-'}，约 ${latest.case_total || '?'} 条用例）。完整模式会从零全量生成，不会基于既有基线做定点归并——若只是小改动，建议改用「增量迭代模式」（只产出变更清单，人工确认后归并）。确定继续全量生成吗？`,
        okText: '继续全量生成',
        cancelText: '取消，改用增量模式',
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      }));
      if (!ok) return;
    }

    setLoading(true);
    try {
      const res = await generateCases(slug, selectedModule, selectedPage);
      setFlowId(res.data.flow_id);
      setTestCases(res.data.test_cases);
      setCaseFilePath(res.data.file_path || '');
      setHistoryUsed(res.data.history_used);
      await fetchSkillStates(res.data.flow_id);
      setCurrentStep(2);
      message.success(
        `测试用例生成完成（模块/页面：${(MODULE_LIST.find(m => m.value === selectedModule) || {}).label || selectedModule || '未选'} / ${selectedPage ? ((PAGE_LIST.find(p => p.value === selectedPage) || {}).label || selectedPage) : '模块层'}）`
      );
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `测试用例生成失败：${detail}` : '测试用例生成失败');
    }
    setLoading(false);
  };

  // ========== 快速模式：直接生成用例 ==========
  const handleDirectGenerate = async () => {
    if (!requirement.trim()) return message.warning('请输入需求描述');
    setLoading(true);
    try {
      const res = await generateDirect(requirement, selectedModule, uploadContent, selectedPage);
      setSlug(res.data.slug);
      setFlowId(res.data.flow_id);
      setTestCases(res.data.test_cases);
      setCaseFilePath(res.data.file_path || '');
      setHistoryUsed(res.data.history_used);
      await fetchSkillStates(res.data.flow_id);
      message.success('测试用例生成完成');
    } catch (e) {
      message.error('测试用例生成失败');
    }
    setLoading(false);
  };

  // ========== 补充规则重生成 ==========
  const handleRegen = async () => {
    if (!testCases) return message.warning('请先生成用例');
    setLoading(true);
    try {
      // 传入flowId精确定位流程，避免多任务并发时串流程
      const res = await reGenCase(supplement, flowId);
      setFlowId(res.data.flow_id);
      setTestCases(res.data.test_cases);
      await fetchSkillStates(res.data.flow_id);
      message.success('用例重生成完成');
    } catch (e) {
      message.error('用例重生成失败');
    }
    setLoading(false);
  };

  // ========== 增量模式【第一阶段】：影响面分析 ==========
  const handleAnalyzeImpact = async () => {
    if (!incrementalModule) return message.warning('请选择业务模块');
    if (!changeDescription.trim()) return message.warning('请输入变更描述');
    setImpactLoading(true);
    try {
      // 带上选定的历史基线版本：走元数据精确取全文，避免「选了版本却仍用向量兜底拿旧版」
      const res = await analyzeImpact(incrementalModule, changeDescription, incrementalPage, historyVersion);
      setImpactAnalysis(res.data.impact_analysis || '');
      setIncrementalCases('');
      setMergedFullCases('');
      // 解析出的结构化疑点：逐条答复后会被持久化，下次同页面分析不再重复提问
      const qs = (res.data && res.data.pending_questions) || [];
      setPendingQuestions(qs);
      setClarificationAnswers({});
      // 基线来源可观测：向量兜底时提示「未命中选定版本」，避免误以为用的是自己选的那版
      const usedVer = (res.data && res.data.history_version) || '';
      const baselineHint = res.data && res.data.has_history
        ? `（基线：${usedVer || '未指定版本'} ${res.data.history_len || 0} 字）`
        : '（未找到历史基线，按全新模块分析）';
      if (qs.length) {
        message.warning(`影响面分析完成，发现 ${qs.length} 个待确认疑点，请逐条确认${baselineHint}`);
      } else {
        message.success(`影响面分析完成，请确认后生成增量用例${baselineHint}`);
      }
      if (historyVersion && usedVer && usedVer !== historyVersion) {
        message.warning(`选定版本 ${historyVersion} 未命中，实际使用 ${usedVer}`);
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `影响面分析失败：${detail}` : '影响面分析失败');
    }
    setImpactLoading(false);
  };

  // ========== 疑点澄清保存（答复持久化到该 module+page，形成「分析→澄清→复用」闭环）==========
  const handleSaveClarifications = async () => {
    if (!incrementalModule) return message.warning('请先选择业务模块');
    const items = pendingQuestions
      .map(q => ({
        id: q.id,
        question: q.question,
        answer: (clarificationAnswers[q.id] || '').trim(),
      }))
      .filter(it => it.answer);
    if (!items.length) return message.warning('请至少填写一条疑点答复');
    setSavingClarifications(true);
    try {
      const res = await saveClarifications(incrementalModule, incrementalPage, items);
      const total = (res.data && res.data.total) || items.length;
      // 同步把答复并入「影响面分析」，让第二步生成时把这些确认结论作为约束带入
      const clarifyBlock = '\n\n【用户对疑点的确认答复】\n' +
        items.map(it => `- ${it.id} ${it.question} → ${it.answer}`).join('\n');
      setImpactAnalysis(prev => (prev.includes('【用户对疑点的确认答复】') ? prev : prev + clarifyBlock));
      message.success(`✅ 已保存 ${items.length} 条澄清答复（累计 ${total} 条），并已并入影响面分析，下次分析将自动复用`);
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `保存澄清失败：${detail}` : '保存澄清失败');
    } finally {
      setSavingClarifications(false);
    }
  };

  // ========== 增量模式【第二阶段】：基于确认后的影响面生成变更用例 ==========
  const handleIncrementalGenerate = async () => {
    if (!incrementalModule) return message.warning('请选择业务模块');
    if (!changeDescription.trim()) return message.warning('请输入变更描述');
    if (!impactAnalysis.trim()) return message.warning('请先完成并确认影响面分析');
    setIncrementalLoading(true);
    try {
      const res = await generateIncremental(
        incrementalModule, changeDescription, historyVersion, incrementalPage, impactAnalysis
      );
      setIncrementalCases(res.data.incremental_cases);
      setMergedFullCases(res.data.merged_full_cases);
      // 归并元信息：新增/修改/删除账 + 待确认删除清单（hold 模式：模型建议删除的用例未被执行）
      setMergeMeta(res.data.merge_meta || null);
      setDeletionChecked({});          // 删除项默认全部不勾选（安全默认：不勾选就不会删）
      setDeletionResolved('');
      // 使用后端返回的真实 slug/flowId，确保后续「沉淀新版本」能正确定位上下文
      setSlug(res.data.slug || `incremental_${Date.now()}`);
      setFlowId(res.data.flow_id || '');
      setCaseFilePath(res.data.file_path || '');
      const meta = res.data.merge_meta || {};
      const pending = (meta.pending_deletions || []).length;
      message.success(
        pending
          ? `增量用例生成完成：新增 ${meta.added || 0} / 修改 ${meta.modified || 0} / 建议删除 ${pending} 条（待你确认，未执行）`
          : `增量用例生成完成：新增 ${meta.added || 0} / 修改 ${meta.modified || 0}`
      );
    } catch (e) {
      message.error('增量用例生成失败');
    }
    setIncrementalLoading(false);
  };

  // ========== 增量「建议删除」确认闸门 ==========
  // 生成增量用例时，模型「建议删除」的用例默认不会被执行删除（全部保留在产物里）。
  // 这里是人工闸门：勾选确认后才剔除，产物按同一 slug 重新归并覆盖，其余用例逐字不变。
  const pendingDeletions = (mergeMeta && mergeMeta.pending_deletions) || [];

  const handleConfirmDeletions = (applyDelete) => {
    const names = applyDelete
      ? pendingDeletions.map(p => p.name).filter(n => deletionChecked[n])
      : [];
    if (applyDelete && !names.length) {
      return message.warning('请先勾选要删除的用例；若一条都不删，请点「全部保留」');
    }
    Modal.confirm({
      title: applyDelete ? `确认删除 ${names.length} 条用例？` : '确认全部保留？',
      content: applyDelete
        ? `将剔除：${names.join('、')}。产物会按同一 slug 重新归并覆盖，未被删除的用例逐字不变。此操作不可撤销。`
        : '不执行任何删除，产物保持当前的全量内容。',
      okText: applyDelete ? '确认删除' : '全部保留',
      okButtonProps: applyDelete ? { danger: true } : {},
      cancelText: '取消',
      onOk: async () => {
        if (!applyDelete) {
          setDeletionResolved('kept');
          message.success('已选择全部保留，产物未改动');
          return;
        }
        setDeletionApplying(true);
        try {
          const res = await confirmDeletions(
            slug, names, incrementalModule, incrementalPage, changeDescription, historyVersion
          );
          setMergedFullCases(res.data.merged_full_cases || '');
          setMergeMeta(res.data.merge_meta || null);
          setDeletionResolved('applied');
          if ((res.data.rejected_deletions || []).length) {
            message.warning(
              `以下用例不在本次「建议删除」清单内，已忽略：${res.data.rejected_deletions.join('、')}`
            );
          }
          message.success(
            `已删除 ${(res.data.applied_deletions || []).length} 条用例，产物已重新归并覆盖`
          );
        } catch (e) {
          const detail = e?.response?.data?.detail || '';
          message.error(detail ? `删除确认失败：${detail}` : '删除确认失败');
        } finally {
          setDeletionApplying(false);
        }
      }
    });
  };

  // ========== 导出文件（兼容Excel正确后缀）==========
  const handleExport = async (content, type) => {
    if (!content) return message.warning('无可用内容');
    try {
      const res = await exportCase(content, exportFormat);
      const url = window.URL.createObjectURL(res.data);
      const a = document.createElement('a');
      // Excel自动修正为.xlsx后缀
      const suffix = exportFormat === 'excel' ? 'xlsx' : exportFormat;
      a.href = url;
      a.download = `${type}.${suffix}`;
      a.click();
      window.URL.revokeObjectURL(url);
      message.success('导出成功');
    } catch (e) {
      message.error('导出失败');
    }
  };

  // ========== 用例质量评审（引擎A必跑、引擎B可跳过）==========
  // targetSlug 缺省 = 当前生成产物的 slug；传入（历史产物）则评指定产物
  // owner：结果归属域（'generate' | 'asset'）—— 面板只在自己的域渲染，
  //        从「靠 slug 相等守卫防串味」变成「结构上不可能串味」
  const handleScoreCases = async (targetSlug, owner = 'generate') => {
    const s = targetSlug || slug;
    if (!s) return message.warning('请先生成用例（或选中历史产物）再评审');
    setCaseReviewLoading(true);
    try {
      const res = await scoreCases({ slug: s, useEngineB: caseReviewUseEngineB });
      const d = res.data || {};
      if (d.code !== 0) {
        message.error(d.message || '评审失败');
        return;
      }
      setCaseReviewResult({ ...d, _owner: owner });
      // 默认全勾「建议删除」的用例（人工可取消勾选）；improve/keep 默认不勾
      const defaults = (d.cases || [])
        .filter(c => c.decision === 'suggest_delete')
        .map(c => c.name);
      setCaseReviewChecked(defaults);
      const s2 = d.summary || {};
      if ((d.failed_segments || []).length > 0) {
        message.warning(`评审完成，但引擎B第 ${d.failed_segments.join('、')} 片失败（这些用例只有硬指标分），建议重试`);
      } else {
        message.success(`评审完成：保留 ${s2.keep || 0} / 待改进 ${s2.improve || 0} / 建议删除 ${s2.suggest_delete || 0}（均分 ${s2.avg || 0}）`);
      }
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `评审失败：${detail}` : '评审失败');
    } finally {
      setCaseReviewLoading(false);
    }
  };

  // ========== 统一沉淀功能（点击按钮才触发）==========
  const handlePrecipitate = async (assetType, precipitateType) => {
    if (!precipitateType) return message.warning('请先选择沉淀类型');
    if (!slug) return message.warning('请先生成内容再沉淀');
    try {
      // 版本号：界面未指定时按 (module, page) 取「下一个可用版本」。
      // 历史坑：这里曾硬编码 undefined → 后端默认 v1.0 → 无声覆盖同页已有基线。
      let version = (historyVersion || '').trim();
      let versions = [];
      if (currentModule) {
        try {
          const vr = await getModuleVersions(currentModule, currentPage || '');
          versions = normalizeVersionItems(vr.data);
          if (!version) version = (vr.data && vr.data.next_version) || '';
        } catch (e) { /* 取不到就让后端按默认处理 */ }
      }
      const scopeLabel = `${(MODULE_LIST.find(m => m.value === currentModule) || {}).label || currentModule || '未选模块'} / ${currentPage ? ((PAGE_LIST.find(p => p.value === currentPage) || {}).label || currentPage) : '模块层'}`;
      const latest = versions.length ? versions[0] : null;
      const assetLabel = assetType === 'review_report' ? '评审报告' : '测试用例';
      // 用例评审联动：仅 test_cases 且有人工勾选的删除项时，沉淀前剔除
      const removedNames = (assetType === 'test_cases' && caseReviewChecked.length) ? caseReviewChecked : [];
      const removedReasons = {};
      if (removedNames.length && caseReviewResult) {
        (caseReviewResult.cases || []).forEach(c => {
          if (removedNames.includes(c.name)) {
            removedReasons[c.name] = c.reason || `评审建议删除（总分${c.total}）`;
          }
        });
      }
      const ok = await new Promise(resolve => Modal.confirm({
        title: `确认沉淀${assetLabel}`,
        content: `归属：${scopeLabel}；版本：${version || '（后端默认 v1.0）'}。`
          + (latest
            ? `该页面已有最新版本 ${latest.version || '-'}，结构化基线会新增这个版本（旧版仍可查），但召回入口只保留最新一份。`
            : '该页面暂无基线，将新建基线。')
          + (removedNames.length
            ? `本次将按用例评审结果剔除 ${removedNames.length} 条已勾选删除的用例（产物文件同步覆盖）。`
            : ''),
        okText: '确认沉淀',
        cancelText: '取消',
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      }));
      if (!ok) return;
      await precipitateAsset({
        slug: slug,
        asset_type: assetType,
        precipitate_type: precipitateType,
        module_name: currentModule,
        skill_name: assetType === 'test_cases' ? 'req-test-cases' : 'req-review',
        version: version || undefined,
        page: currentPage,
        removedCaseNames: removedNames,
        removedCaseReasons: removedReasons
      });
      message.success(`✅ 沉淀成功（版本 ${version || 'v1.0'}${removedNames.length ? `，已剔除${removedNames.length}条评审删除用例` : ''}），后续生成将自动复用`);
      if (removedNames.length) {
        // 剔除后产物已变化，评审结果与勾选集不再适用，避免二次沉淀误删
        setCaseReviewChecked([]);
        setCaseReviewResult(null);
      }
      loadOutputFiles();
      loadHistoryVersions(incrementalModule, incrementalPage);  // 新沉淀的版本立即出现在下拉里
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `沉淀失败：${detail}` : '沉淀失败');
    }
  };

  // ========== 历史产物「补沉淀」：忘了沉淀时事后补，不需要重新调大模型 ==========
  // 按「模块（+页面）」筛选产物：归属来自落盘时记录的产物索引，不靠文件名语义猜
  const loadOutputFiles = async (module, page) => {
    const m = module === undefined ? historyAssetModule : module;
    const p = page === undefined ? historyAssetPage : page;
    setOutputFilesLoading(true);
    try {
      const res = await listOutputFiles(m || null, p || null);
      const data = res.data || {};
      // 补沉淀只面向「用例产物」，单点过滤（弹窗清单 + 内联下拉共用本函数）：
      // ① 评审类报告（review_report / case_review_report）不能在此出现，否则选模块+页面后
      //    下拉里混入报告产物，可能被误选沉淀；
      // ② incremental_cases 是变更清单而非全量用例，且后端 /api/precipitate 白名单不收
      //    该类型（选中必 400），一并排除。
      const list = (data.code === 0 ? (data.data || []) : [])
        .filter(f => f.asset_type === 'test_cases');
      setOutputFiles(list);
      // 已指定模块却一个都没匹配到 → 提示找不到该页面的用例产物
      setOutputFilesEmpty(Boolean(m) && list.length === 0);
      // 只匹配到一个产物时自动带出，省掉「选模块+页面 → 再挑产物」这一步
      if (list.length === 1 && !historyAssetSlug) {
        setHistoryAssetType(list[0].asset_type);
        setHistoryAssetSlug(list[0].slug);
      }
      // 产物索引里记了版本号时，顺带作为版本输入的建议值
      if (list.length === 1 && list[0].version && !historyAssetVersion) {
        setHistoryAssetVersion(list[0].version);
      }
    } catch (e) {
      setOutputFiles([]);
      setOutputFilesEmpty(false);
    } finally {
      setOutputFilesLoading(false);
    }
  };

  // ========== 补沉淀版本号：按 (模块, 页面) 拉下一可用版本，避免覆盖已有基线 ==========
  const loadHistoryAssetNextVersion = async (module, page) => {
    if (!module) {
      setHistoryAssetNextVersion('');
      return;
    }
    try {
      const res = await getModuleVersions(module, page || '');
      const nv = (res.data && res.data.next_version) || 'v1.0';
      setHistoryAssetNextVersion(nv);
      // 仅当用户尚未手填时自动带出，不覆盖用户已输入的值
      setHistoryAssetVersion(prev => (prev && prev.trim() ? prev : nv));
    } catch (e) {
      setHistoryAssetNextVersion('');
    }
  };

  // 作用域变更已统一由 handleModuleChange / handleScopePageChange 处理（全局唯一），
  // 资产域不再需要自己的模块/页面变更回调。

  const handleHistoryPrecipitate = async () => {
    if (!historyAssetSlug || !historyAssetType) return message.warning('请先选择历史产物文件');
    if (!historyPrecipitateType) return message.warning('请先选择沉淀类型');
    if (historyPrecipitateType === 'project' && !historyAssetModule) {
      return message.warning('沉淀到项目历史库需先选择业务模块');
    }
    setHistoryPrecipitating(true);
    try {
      // 用例评审联动：评审的正是这个产物且有勾选删除项时，沉淀前剔除
      const removedNames = (
        historyAssetType === 'test_cases' &&
        caseReviewResult && caseReviewResult.slug === historyAssetSlug &&
        caseReviewChecked.length
      ) ? caseReviewChecked : [];
      const removedReasons = {};
      if (removedNames.length) {
        (caseReviewResult.cases || []).forEach(c => {
          if (removedNames.includes(c.name)) {
            removedReasons[c.name] = c.reason || `评审建议删除（总分${c.total}）`;
          }
        });
      }
      const res = await precipitateAsset({
        slug: historyAssetSlug,
        asset_type: historyAssetType,
        precipitate_type: historyPrecipitateType,
        module_name: historyAssetModule,
        skill_name: historyAssetType === 'test_cases' ? 'req-test-cases' : 'req-review',
        page: historyAssetPage,
        version: historyAssetVersion || undefined,
        removedCaseNames: removedNames,
        removedCaseReasons: removedReasons
      });
      const data = res.data || {};
      message.success(
        `✅ 补沉淀成功（模块 ${data.biz_module || '-'} / 页面 ${data.page || '模块层'} / 版本 ${data.version || 'v1.0'}）`
        + (removedNames.length ? `，已剔除${removedNames.length}条评审删除用例` : '')
      );
      if (removedNames.length) {
        setCaseReviewChecked([]);
        setCaseReviewResult(null);
      }
      setHistoryAssetSlug('');
      setHistoryAssetType('');
      loadOutputFiles();
      // 批1把「独立沉淀」从弹窗改成页面域后，precipitateModalOpen 这个 state 已整体删除。
      // 这里曾残留一行对 setPrecipitateModalOpen 的调用：沉淀其实已成功（已弹绿色提示），
      // 但该行抛 ReferenceError 被下面 catch 捕获 ⇒ 又弹一条「补沉淀失败」，
      // 变成「一次点击、成功+失败两条提示」的假报错。不要再加回来。
    } catch (e) {
      const detail = e?.response?.data?.detail || '';
      message.error(detail ? `补沉淀失败：${detail}` : '补沉淀失败');
    } finally {
      setHistoryPrecipitating(false);
    }
  };

  // 拉取本模块的评分 sidecar（一次拿全版本，按版本键映射到产物行）
  const loadCaseScores = async (module) => {
    if (!module) {
      setCaseScores(null);
      return;
    }
    try {
      const res = await getCaseScores(module, '', '');
      const d = res.data || {};
      setCaseScores(d.code === 0 && d.data ? d.data : null);
    } catch (e) {
      setCaseScores(null);
    }
  };

  // ========== 「报告中心」域：只读查看 report/ 目录下的报告 ==========
  const loadReportFiles = async (module, page) => {
    const m = module === undefined ? historyAssetModule : module;
    const p = page === undefined ? historyAssetPage : page;
    if (!m) {
      setReportFiles([]);
      return;
    }
    setReportFilesLoading(true);
    try {
      const res = await listOutputFiles(m || null, p || null);
      const data = res.data || {};
      const list = data.code === 0 ? (data.data || []) : [];
      // 只留报告类（需求评审报告 + 用例评审报告），与用例产物分开
      setReportFiles(list.filter(f => REPORT_ASSET_TYPES.includes(f.asset_type)));
    } catch (e) {
      setReportFiles([]);
    } finally {
      setReportFilesLoading(false);
    }
  };

  // ========== 模式切换重置所有数据 ==========
  const onModeChange = (value) => {
    setCurrentMode(value);
    // 公共状态重置
    setCurrentStep(0);
    setReviewReport('');
    setTestCases('');
    setCaseFilePath('');
    setSupplement('');
    setSlug('');
    setFlowId('');
    setCasePrecipitateType('');
    setReportPrecipitateType('');
    setRequirement('');
    // ⚠️ 不再重置作用域（模块/页面已全局唯一）：切生成模式不该丢掉已选作用域
    setUploadContent('');
    setUploadFileName('');
    // 增量模式状态重置
    setHistoryVersion('');
    setHistoryVersionOptions([]);
    setChangeDescription('');
    setIncrementalCases('');
    setMergedFullCases('');
    setIncrementalLoading(false);
    setImpactAnalysis('');
    setImpactLoading(false);
  };

  // ==================== 传给各功能域的上下文 ====================
  // 状态与业务逻辑集中在本文件（控制器），功能域组件只负责展示。
  // 由脚本按「组件内全部顶层声明」自动生成；新增 state / 处理函数后需同步补进来。
  const C = {
    activeNav,
    assetEdit,
    assetEditContent,
    assetPreview,
    assetSaving,
    caseFilePath,
    casePrecipitateType,
    caseReviewChecked,
    caseReviewLoading,
    caseReviewResult,
    caseReviewUseEngineB,
    caseScores,
    changeDescription,
    clarificationAnswers,
    clearRulePrecheck,
    closeAssetEdit,
    closeRuleModal,
    currentMode,
    currentModule,
    currentPage,
    currentStep,
    deletionApplying,
    deletionChecked,
    deletionResolved,
    exportFormat,
    fetchSkillStates,
    flowId,
    handleAnalyzeImpact,
    handleApplyRulePatch,
    handleConfirmDeletions,
    handleDeleteAsset,
    handleDirectGenerate,
    handleDownloadRuleTemplate,
    handleExport,
    handleGenerateFull,
    handleHistoryPrecipitate,
    handleIncrementalGenerate,
    handleModuleChange,
    handleOpenAssetEdit,
    handleOpenRuleFile,
    handlePrecipitate,
    handlePrecheckRuleTarget,
    handleProposeRulePatch,
    handleRegen,
    handleReview,
    handleSaveAssetContent,
    handleSaveClarifications,
    handleSaveRuleContent,
    handleScopePageChange,
    handleScoreCases,
    handleUpload,
    handleUploadRuleFile,
    historyAssetModule,
    historyAssetNextVersion,
    historyAssetPage,
    historyAssetSlug,
    historyAssetType,
    historyAssetVersion,
    historyPrecipitateType,
    historyPrecipitating,
    historyUsed,
    historyVersion,
    historyVersionLoading,
    historyVersionOptions,
    impactAnalysis,
    impactLoading,
    incrementalCases,
    incrementalLoading,
    incrementalModule,
    incrementalPage,
    loadAllRuleFiles,
    loadCaseScores,
    loadHistoryAssetNextVersion,
    loadHistoryVersions,
    loadOutputFiles,
    loadReportFiles,
    loadRuleFiles,
    loading,
    mergeMeta,
    mergedFullCases,
    onModeChange,
    outputFiles,
    outputFilesEmpty,
    outputFilesLoading,
    patchApplying,
    patchChecked,
    patchLoading,
    patchResult,
    patchSummary,
    pendingDeletions,
    pendingQuestions,
    pollTimerRef,
    reportFiles,
    reportFilesLoading,
    reportPrecipitateType,
    reportPreview,
    requirement,
    resetScopeDependents,
    reviewHistoryInfo,
    reviewReport,
    reviewScope,
    reviewWithHistory,
    ruleFiles,
    ruleFilesAll,
    ruleFilesAllLoading,
    ruleModal,
    ruleModalContent,
    ruleModalMode,
    rulePatches,
    rulePatchChangeDesc,
    rulePatchImpact,
    rulePrecheck,
    ruleSaving,
    savingClarifications,
    selectedModule,
    selectedPage,
    setActiveNav,
    setAssetEdit,
    setAssetEditContent,
    setAssetPreview,
    setAssetSaving,
    setCaseFilePath,
    setCasePrecipitateType,
    setCaseReviewChecked,
    setCaseReviewLoading,
    setCaseReviewResult,
    setCaseReviewUseEngineB,
    setCaseScores,
    setChangeDescription,
    setClarificationAnswers,
    setCurrentMode,
    setCurrentStep,
    setDeletionApplying,
    setDeletionChecked,
    setDeletionResolved,
    setExportFormat,
    setFlowId,
    setHistoryAssetNextVersion,
    setHistoryAssetSlug,
    setHistoryAssetType,
    setHistoryAssetVersion,
    setHistoryPrecipitateType,
    setHistoryPrecipitating,
    setHistoryUsed,
    setHistoryVersion,
    setHistoryVersionLoading,
    setHistoryVersionOptions,
    setImpactAnalysis,
    setImpactLoading,
    setIncrementalCases,
    setIncrementalLoading,
    setLoading,
    setMergeMeta,
    setMergedFullCases,
    setOutputFiles,
    setOutputFilesEmpty,
    setOutputFilesLoading,
    setPatchApplying,
    setPatchChecked,
    setPatchLoading,
    setPatchResult,
    setPatchSummary,
    setPendingQuestions,
    setReportFiles,
    setReportFilesLoading,
    setReportPrecipitateType,
    setReportPreview,
    setRequirement,
    setReviewHistoryInfo,
    setReviewReport,
    setReviewScope,
    setReviewWithHistory,
    setRuleFiles,
    setRuleFilesAll,
    setRuleFilesAllLoading,
    setRuleModal,
    setRuleModalContent,
    setRuleModalMode,
    setRulePatches,
    setRulePatchChangeDesc,
    setRulePatchImpact,
    setRulePrecheck,
    setRuleSaving,
    setSavingClarifications,
    setSelectedModule,
    setSelectedPage,
    setSkillStates,
    setSlug,
    setSupplement,
    setTestCases,
    setUploadContent,
    setUploadFileName,
    skillStates,
    slug,
    startSkillPolling,
    stopSkillPolling,
    supplement,
    testCases,
    uploadContent,
    uploadFileName,
  };

  return (
    <Layout style={{ minHeight: '100vh' }}>
      {/* ========== 左侧一级导航（按功能域，不按动作）========== */}
      <Sider width={196} theme="light" style={{ borderRight: '1px solid #f0f0f0' }}>
        <div style={{
          height: 60,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 16,
          fontWeight: 600,
          color: '#1677ff',
          borderBottom: '1px solid #f0f0f0'
        }}>
          <AppstoreOutlined style={{ marginRight: 8 }} />
          测试管理平台
        </div>
        <Menu
          mode="inline"
          selectedKeys={[activeNav]}
          items={NAV_ITEMS}
          onClick={({ key }) => setActiveNav(key)}
          style={{ borderInlineEnd: 'none', paddingTop: 8 }}
        />
      </Sider>

      <Layout>
        {/* ========== 顶部全局作用域条（模块 + 页面，全局唯一）========== */}
        <ScopeBar
          selectedModule={selectedModule}
          selectedPage={selectedPage}
          handleModuleChange={handleModuleChange}
          handleScopePageChange={handleScopePageChange}
        />

        <Content style={{ padding: 24, background: '#f5f7fa' }}>
          <div style={{ maxWidth: 1440, margin: '0 auto' }}>

          {/* ========== 功能域（批2a 已拆成独立文件）==========
              每个域各自套 ErrorBoundary：单域渲染出错时只有该域变成错误卡片，
              不会让整个应用白屏（React 18 无边界时抛错会卸载整棵根树）。 */}
          {activeNav === 'generate' && (
            <ErrorBoundary name="用例生成" resetKey={`${selectedModule}|${selectedPage}`}>
              <GeneratePage C={C} />
            </ErrorBoundary>
          )}
          {activeNav === 'assets' && (
            <ErrorBoundary name="用例资产" resetKey={`${selectedModule}|${selectedPage}`}>
              <AssetsPage C={C} />
            </ErrorBoundary>
          )}
          {activeNav === 'reports' && (
            <ErrorBoundary name="报告中心" resetKey={`${selectedModule}|${selectedPage}`}>
              <ReportsPage C={C} />
            </ErrorBoundary>
          )}
          {activeNav === 'rules' && (
            <ErrorBoundary name="规则管理" resetKey={`${selectedModule}|${selectedPage}`}>
              <RulesPage C={C} />
            </ErrorBoundary>
          )}
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

export default App;
