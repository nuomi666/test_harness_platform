import axios from 'axios';

// 创建请求实例
const request = axios.create({
  baseURL: 'http://127.0.0.1:8000',
  timeout: 300000, // 5分钟超时，适配大模型生成
});

// ========== 1. 上传业务文档/截图 ==========
export const uploadDoc = (file) => {
  const formData = new FormData();
  formData.append('file', file);
  return request.post('/upload', formData);
};

// ========== 2. 需求评审 ==========
// uploadContent：/upload 已解析出的文本（与 /review、/generate-direct 同一通道）
// 历史坑：曾把这段文本塞进 upload_file 字段，后端声明的是 UploadFile ⇒ 必 422
// historyVersion：指定带入评审的历史用例版本，空=后端自动取该 (module,page) 最新版本
// historyMode：auto=按模块/页面召回历史用例（后端精简后注入）；off=当全新需求评审
export const generateReview = (
  requirement, bizModule, page = '', uploadContent = '',
  historyVersion = '', historyMode = 'auto'
) => {
  const formData = new FormData();
  formData.append("biz_module", bizModule || "");
  formData.append("requirement_desc", requirement || "");
  if (page) formData.append("page", page);
  if (uploadContent) formData.append("upload_content", uploadContent);
  if (historyVersion) formData.append("history_version", historyVersion);
  formData.append("history_mode", historyMode || "auto");
  return request.post("/generate-review", formData);
};

// ========== 3. 基于评审结果生成用例（完整模式）==========
export const generateCases = (slug, bizModule = '', page = '') => {
  const formData = new FormData();
  formData.append('slug', slug);
  if (bizModule) formData.append('biz_module', bizModule);
  if (page) formData.append('page', page);
  return request.post('/generate-cases', formData);
};

// ========== 4. 快速模式：直接生成用例 ==========
export const generateDirect = (requirement, bizModule = '', uploadContent = '', page = '') => {
  const formData = new FormData();
  formData.append('requirement', requirement);
  if (bizModule) formData.append('biz_module', bizModule);
  if (uploadContent) formData.append('upload_content', uploadContent);
  if (page) formData.append('page', page);
  return request.post('/generate-direct', formData);
};

// ========== 5. 补充规则重生成用例 ==========
// 传入 flowId 精确定位流程，避免多任务并发时串到别人的流程上
export const reGenCase = (supplement, flowId = '') => {
  const formData = new FormData();
  formData.append('supplement', supplement);
  if (flowId) formData.append('flow_id', flowId);
  return request.post('/regenerate', formData);
};

// ========== 6. 导出文件（修复Excel导出乱码，强制blob格式）==========
export const exportCase = (content, format) => {
  const formData = new FormData();
  formData.append('content', content);
  formData.append('format', format);
  return request.post('/export', formData, {
    responseType: 'blob' // 关键：二进制流格式，解决Excel乱码
  });
};

// ========== 7. 沉淀资产 ==========
// removedCaseNames：用例评审后人工确认删除的用例名（仅 test_cases 生效），
// 后端沉淀前剔除这些用例行并同步覆盖产物文件；removedCaseReasons 为配套的删除原因
export const precipitateAsset = (params) => {
  const formData = new FormData();
  formData.append('slug', params.slug);
  formData.append('asset_type', params.asset_type);
  formData.append('precipitate_type', params.precipitate_type);
  formData.append('module_name', params.module_name || '');
  formData.append('skill_name', params.skill_name || 'req-test-cases');
  if (params.page) formData.append('page', params.page);
  if (params.version) formData.append('version', params.version);
  if (params.removedCaseNames && params.removedCaseNames.length) {
    formData.append('removed_case_names', JSON.stringify(params.removedCaseNames));
    formData.append('removed_case_reasons', JSON.stringify(params.removedCaseReasons || {}));
  }
  return request.post('/api/precipitate', formData);
};

// ========== 8. 增量迭代生成用例（第二阶段，携带确认后的影响面分析）==========
export const generateIncremental = (moduleName, changeDescription, historyVersion = '', page = '', impactAnalysis = '') => {
  const formData = new FormData();
  formData.append('module_name', moduleName);
  formData.append('change_description', changeDescription);
  if (historyVersion) formData.append('history_version', historyVersion);
  if (page) formData.append('page', page);
  if (impactAnalysis) formData.append('impact_analysis', impactAnalysis);
  return request.post('/generate-incremental', formData);
};

// ========== 8.1 增量模式【第一阶段】影响面分析 ==========
// historyVersion：选定的历史基线版本；传入后按 (module, page, version) 精确取基线全文
export const analyzeImpact = (moduleName, changeDescription, page = '', historyVersion = '') => {
  const formData = new FormData();
  formData.append('module_name', moduleName);
  formData.append('change_description', changeDescription);
  if (page) formData.append('page', page);
  if (historyVersion) formData.append('history_version', historyVersion);
  return request.post('/api/analyze-impact', formData);
};

// ========== 8.2 保存疑点澄清答复（增量影响面闭环：答复持久化，下次不再重复提问）==========
// items: [{"id","question","answer"}]，按 (module, page) 持久化
export const saveClarifications = (moduleName, page = '', items = []) => {
  const formData = new FormData();
  formData.append('module_name', moduleName);
  if (page) formData.append('page', page);
  formData.append('items', JSON.stringify(items));
  return request.post('/api/save-clarifications', formData);
};

// ========== 8.3 方案C：规则补丁（补丁式 skill 更新 + 人工确认）==========
// 列出该模块/页面下的规则文件与 R/S/F 编号清单
export const listRuleFiles = (moduleName, page = '') => {
  return request.get('/api/rule-files', { params: { module_name: moduleName, page } });
};

// 生成规则补丁（不落盘，供人工确认）
export const proposeRulePatch = (moduleName, page, changeDescription, impactAnalysis = '') => {
  const formData = new FormData();
  formData.append('module_name', moduleName);
  if (page) formData.append('page', page);
  formData.append('change_description', changeDescription);
  if (impactAnalysis) formData.append('impact_analysis', impactAnalysis);
  return request.post('/api/propose-rule-patch', formData);
};

// 应用人工确认后的补丁（备份 → 定点替换 → 打「需求变更改动」标记 → 热重载知识库）
// changeNote：本次需求变更描述，会写进规则 front matter（last_change_note）供追溯
export const applyRulePatch = (moduleName, page, patches, selectedIds = null, changeNote = '') => {
  const formData = new FormData();
  formData.append('module_name', moduleName);
  if (page) formData.append('page', page);
  formData.append('patches', JSON.stringify(patches || []));
  if (selectedIds && selectedIds.length) formData.append('selected_ids', JSON.stringify(selectedIds));
  if (changeNote) formData.append('change_note', changeNote);
  return request.post('/api/apply-rule-patch', formData);
};

// ========== 8.4 增量「建议删除」确认（hold → 人工勾选 → 二次归并覆盖产物）==========
// 生成增量用例时，模型「建议删除」的用例默认不会被执行删除（全部保留在产物里）。
// 本接口用于人工勾选确认：只剔除确认过的用例，其余用例逐字不变。传空数组 = 全部保留。
export const confirmDeletions = (slug, deletedNames = [], moduleName = '', page = '', changeDescription = '', historyVersion = '') => {
  const formData = new FormData();
  formData.append('slug', slug);
  formData.append('deleted_names', JSON.stringify(deletedNames || []));
  if (moduleName) formData.append('module_name', moduleName);
  if (page) formData.append('page', page);
  if (changeDescription) formData.append('change_description', changeDescription);
  if (historyVersion) formData.append('history_version', historyVersion);
  return request.post('/api/confirm-deletions', formData);
};

// ========== 8.5 规则管理：业务规则文件（= 页面规则）的浏览 / 上传 / 替换 / 编辑 ==========
// 概念：本框架里「业务 skill 规则」= skills/req-test-cases/references/ 下的页面规则文件。
// 「同页面是否已存在」判据 = front matter 的 (biz_module, page) 等值匹配，不看文件名/目录名。

// 列出 references 下全部规则文件（不按作用域过滤）
export const listAllRuleFiles = () => request.get('/api/rule-files-all');

// 落盘前预检：会替换哪个文件/新建到哪，以及 front matter 冲突（冲突只报差异，由前端弹窗二选一）
export const getRuleFileTarget = ({ moduleName, page = '', filename = '', content = '', description = '' }) => {
  const params = { module_name: moduleName };
  if (page) params.page = page;
  if (filename) params.filename = filename;
  if (content) params.content = content;
  if (description) params.description = description;
  return request.get('/api/rule-file-target', { params });
};

// 读取单个规则文件全文（查看/编辑用）；filename 与 module+page 二选一
export const getRuleFileContent = ({ moduleName = '', page = '', filename = '' }) => {
  const params = {};
  if (moduleName) params.module_name = moduleName;
  if (page) params.page = page;
  if (filename) params.filename = filename;
  return request.get('/api/rule-file-content', { params });
};

// 保存规则文件（新建或覆盖）：front matter 处理 → 备份 → 写盘 → 重建索引 → 热重载（无需重启后端）
// frontMatterMode：override=以页面选择为准补正（默认）；keep=原样保留
// targetRel：仅「编辑既有文件」用，指定 references 内相对路径，避免 FM 写错时误新建一份
export const saveRuleFile = ({ moduleName = '', page = '', filename = '', content, frontMatterMode = 'override', description = '', targetRel = '' }) => {
  const formData = new FormData();
  formData.append('module_name', moduleName || '');
  formData.append('page', page || '');
  formData.append('filename', filename || '');
  formData.append('content', content || '');
  formData.append('front_matter_mode', frontMatterMode);
  formData.append('description', description || '');
  formData.append('target_rel', targetRel || '');
  return request.post('/api/rule-file-save', formData);
};

// 上传业务规则（= 页面规则文件，走文本通道；把文本塞进 UploadFile 字段会 422）
export const uploadRuleFile = ({ moduleName, page = '', filename = '', content, frontMatterMode = 'override', description = '' }) => {
  const formData = new FormData();
  formData.append('module_name', moduleName);
  formData.append('page', page || '');
  formData.append('filename', filename || '');
  formData.append('content', content || '');
  formData.append('front_matter_mode', frontMatterMode);
  formData.append('description', description || '');
  return request.post('/api/rule-file-upload', formData);
};

// ========== 8.6 产物删除 / 编辑（用例资产域、报告中心域）==========
// ⚠️ 删除只删本地产物文件，**不动沉淀基线**（基线是权威召回源，删掉会让增量迭代静默退化成「无历史」）
export const deleteAsset = (slug, assetType) => {
  const formData = new FormData();
  formData.append('slug', slug);
  formData.append('asset_type', assetType);
  return request.post('/api/delete-asset', formData);
};

// 覆盖保存产物内容（归属 module/page/version 从产物索引读回沿用）
// ⚠️ 编辑不会同步到已沉淀基线：产物 ≠ 基线时需重新沉淀一次
export const saveAssetContent = (slug, assetType, content) => {
  const formData = new FormData();
  formData.append('slug', slug);
  formData.append('asset_type', assetType);
  formData.append('content', content || '');
  return request.post('/api/save-asset-content', formData);
};

// 下发「业务 skill（页面规则）」骨架模板（backend/app/metadata/modules/业务skill模板.md）
// 只读接口：不落盘、不触发热重载；模板可直接手工改那个 md 文件
export const getRuleTemplate = () => {
  return request.get('/api/rule-template');
};

// ========== 9. 查询Skill执行状态（前端轮询展示执行进度）==========
export const getSkillStatus = (flowId) => {
  return request.get('/api/skill-status', { params: { flow_id: flowId } });
};

// ========== 13. 用例质量评审（引擎A必跑、引擎B可跳过）==========
// 两种模式：传 slug=评产物文件（生成后/存量补评）；传 moduleName+version=评结构化基线
// useEngineB=false 时纯代码硬指标体检（零模型调用，秒回）
export const scoreCases = ({ slug = '', moduleName = '', page = '', version = '', useEngineB = true }) => {
  const formData = new FormData();
  if (slug) formData.append('slug', slug);
  if (moduleName) formData.append('module_name', moduleName);
  if (page) formData.append('page', page);
  if (version) formData.append('version', version);
  formData.append('use_engine_b', useEngineB ? 'true' : 'false');
  return request.post('/api/score-cases', formData);
};

// 查询用例评分 sidecar（version 空 = 该模块全部版本评分）
export const getCaseScores = (moduleName, page = '', version = '') => {
  return request.get('/api/case-scores', {
    params: { module_name: moduleName, page, version }
  });
};

// 存量基线的评审处置：人工确认的差评用例从新版本基线移除（旧版本保留可追溯）
export const applyScoreDecisions = (moduleName, baseVersion, page = '', removedNames = [], reasons = {}) => {
  const formData = new FormData();
  formData.append('module_name', moduleName);
  formData.append('base_version', baseVersion);
  if (page) formData.append('page', page);
  formData.append('removed_names', JSON.stringify(removedNames || []));
  formData.append('reasons', JSON.stringify(reasons || {}));
  return request.post('/api/apply-score-decisions', formData);
};

// ========== 10. 查询模块历史版本列表（增量模式版本下拉用）==========
// 传入 page 时只返回该页面已沉淀的版本，避免跨页选到别的页面的版本号
export const getModuleVersions = (moduleName, page = '') => {
  return request.get('/api/module-versions', { params: { module_name: moduleName, page } });
};

// ========== 11. 读取落盘产物文件（改磁盘后重新加载预览）==========
export const getAssetFile = (slug, assetType = 'test_cases') => {
  return request.get('/api/asset-file', { params: { slug, asset_type: assetType } });
};

// ========== 12. 历史产物清单（忘了沉淀 → 事后补沉淀，无需重新生成）==========
// 传 module / page 时只返回该模块+页面下的产物（依据落盘时记录的产品索引，不靠文件名语义猜）
export const listOutputFiles = (module = null, page = null) => {
  const params = {};
  if (module !== null && module !== undefined) params.module = module;
  if (page !== null && page !== undefined) params.page = page;
  return request.get('/api/output-files', { params });
};

export default request;
