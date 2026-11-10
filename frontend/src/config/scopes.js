/**
 * @File    : scopes.js
 * @Desc    : 业务模块 / 页面清单 —— 全平台作用域的**唯一配置源**
 *
 * ⚠️ 新增模块或页面的操作契约（务必遵守，否则会出现「某处下拉没有新模块」的静默不一致）：
 *
 *   ① 新模块：在 MODULE_LIST 加一行即可。
 *   ② 新页面：在 PAGE_LIST 加一行（module 填所属模块 value，value 必须与规则文件
 *      front matter 的 page 字段**逐字一致**）。
 *
 *   **不需要**改动任何组件代码 —— 一级导航、顶部全局作用域条、用例资产、
 *   报告中心、各模式内的作用域选择器全部从这里读取。
 *
 * 新增页面时后端还需配套一件事（仅此一件）：
 *   在 backend/app/skills/req-test-cases/references/<任意子目录>/ 下放一个规则文件，
 *   front matter 声明 biz_module: <模块 value> 与 page: <页面 value>，然后重启后端。
 *   ⚠️ 目录名不参与匹配（plugin_loader 递归扫描 references/**\/*.md，靠 front matter 等值匹配），
 *   所以放哪个子目录都行，但 front matter 的 page 必须与这里 PAGE_LIST 的 value 完全一致。
 *
 *   忘记放规则文件不会报错，但该页面会静默退化成「无语义精确注入」（get_module_rules_full 返回空）
 *   —— 这正是「选错页面不会被描述纠正、唯一线索只有日志」那类坑，务必成对增改。
 */

// 业务模块清单
export const MODULE_LIST = [
  { value: 'merchant', label: '商户模块' },
  { value: 'secret-key', label: '密钥模块' },
  { value: 'bin', label: '卡BIN模块' },
];

// 页面清单（页面级隔离）；value 与 skill front matter 的 page 字段一致
export const PAGE_LIST = [
  { module: 'bin', value: 'original', label: '卡BIN原数据文件历史' },
  { module: 'bin', value: 'conflict_resolution', label: '卡BIN裁决页面' },
  { module: 'bin', value: 'pool', label: '卡BIN池管理' },
  { module: 'bin', value: 'file_history', label: '卡BIN文件历史' },
];

// 根据模块取可选页面（无页面配置时返回空数组 = 模块层）
export const getPagesForModule = (module) => PAGE_LIST.filter((p) => p.module === module);

// 模块 / 页面 → 中文标签（归属提示、回显统一走这里，避免各处各写一份）
export const labelOfModule = (v) => (MODULE_LIST.find((m) => m.value === v) || {}).label || v || '';

export const labelOfPage = (v) =>
  v ? (PAGE_LIST.find((p) => p.value === v) || {}).label || v : '模块层';

// 一行式作用域描述，如「卡BIN模块 / 卡BIN池管理」
export const scopeLabel = (m, p) => `${labelOfModule(m) || '未选模块'} / ${labelOfPage(p)}`;

// 报告类资产类型（落 report/ 目录，与用例类产物分开）
export const REPORT_ASSET_TYPES = ['review_report', 'case_review_report'];
