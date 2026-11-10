import { Upload, Button, Input, Card, message, Spin, Space, Select, Divider, Steps, Tabs, Tag, Checkbox, Switch, Alert, Segmented, Empty, Typography } from 'antd';
const { Text } = Typography;
const { TextArea } = Input;
const { Option } = Select;
const { Step } = Steps;
const { TabPane } = Tabs;
import { UploadOutlined, PlayCircleOutlined, ReloadOutlined, DownloadOutlined, SaveOutlined, SafetyCertificateOutlined, FolderOpenOutlined } from '@ant-design/icons';
import { getAssetFile } from '../api';
import { PAGE_LIST } from '../config/scopes';
import React from 'react';
import MarkdownView from '../components/MarkdownView';
import CaseReviewPanel from '../components/CaseReviewPanel';

// 生成模式配置（label 给下拉用，short 给 Segmented 用）
const MODE_LIST = [
  { value: 'quick', label: '快速模式（直接生成用例）', short: '快速' },
  { value: 'review', label: '评审模式（仅生成评审报告）', short: '评审' },
  { value: 'full', label: '完整模式（评审 + 生成用例全流程）', short: '完整' },
  { value: 'incremental', label: '增量迭代模式（仅生成变更部分）', short: '增量迭代' },
];

// Skill执行状态 → Tag颜色（对应后端 skill_state_manager.SKILL_STATUS）
const SKILL_STATUS_COLOR = {
  pending: 'default',
  running: 'processing',
  retrying: 'warning',
  success: 'success',
  failed: 'error',
  timeout: 'error',
};

// 批2a：从 App.js 机械拆分而出（纯搬运，逻辑未改）。
// 下方解构列表由脚本按「本文件实际用到的 App.js 作用域标识符」自动推导；
// 状态与业务逻辑仍集中在 App.js（控制器），本文件只负责展示。
// ⚠️ 新增 state/函数后，需同步补进 App.js 的 C 对象。
export default function GeneratePage({ C }) {
  const {
    caseFilePath,
    casePrecipitateType,
    caseReviewLoading,
    caseReviewResult,
    caseReviewUseEngineB,
    changeDescription,
    clarificationAnswers,
    currentMode,
    currentStep,
    deletionApplying,
    deletionChecked,
    deletionResolved,
    exportFormat,
    handleAnalyzeImpact,
    handleConfirmDeletions,
    handleDirectGenerate,
    handleExport,
    handleGenerateFull,
    handleIncrementalGenerate,
    handlePrecipitate,
    handleRegen,
    handleReview,
    handleSaveClarifications,
    handleScoreCases,
    handleUpload,
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
    loadHistoryVersions,
    loading,
    mergeMeta,
    mergedFullCases,
    onModeChange,
    pendingDeletions,
    pendingQuestions,
    reportPrecipitateType,
    requirement,
    reviewHistoryInfo,
    reviewReport,
    reviewWithHistory,
    savingClarifications,
    selectedModule,
    setActiveNav,
    setCasePrecipitateType,
    setCaseReviewUseEngineB,
    setChangeDescription,
    setClarificationAnswers,
    setDeletionChecked,
    setDeletionResolved,
    setExportFormat,
    setHistoryAssetSlug,
    setHistoryAssetType,
    setHistoryVersion,
    setImpactAnalysis,
    setMergedFullCases,
    setReportPrecipitateType,
    setRequirement,
    setReviewWithHistory,
    setSupplement,
    setTestCases,
    skillStates,
    slug,
    supplement,
    testCases,
    uploadFileName,
  } = C;


  // ========== Skill执行状态展示（耗时/重试/成败，便于定位慢在哪一步）==========
  const renderSkillStatus = () => {
    const entries = Object.entries(skillStates || {});
    if (entries.length === 0) return null;
    return (
      <div style={{
        marginBottom: 16,
        padding: '12px 16px',
        background: '#fafafa',
        borderRadius: 6,
        border: '1px solid #f0f0f0'
      }}>
        <Text strong style={{ display: 'block', marginBottom: 8, fontSize: 13 }}>
          Skill 执行状态
        </Text>
        {entries.map(([name, st]) => (
          <div key={name} style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            padding: '4px 0',
            fontSize: 13
          }}>
            <Text code style={{ fontSize: 12 }}>{name}</Text>
            <Space size={8}>
              {st.retry_count > 0 && (
                <Text type="warning" style={{ fontSize: 12 }}>重试{st.retry_count}次</Text>
              )}
              {st.cost_time > 0 && (
                <Text type="secondary" style={{ fontSize: 12 }}>{st.cost_time}s</Text>
              )}
              <Tag color={SKILL_STATUS_COLOR[st.status] || 'default'}>{st.status_text}</Tag>
            </Space>
          </div>
        ))}
      </div>
    );
  };


  // 评审模式：历史用例带入情况（如实展示「带了多少 / 精简到什么程度 / 裁掉了什么 / 谁没产出」）
  const renderReviewHistoryInfo = () => {
    const info = reviewHistoryInfo;
    if (!info) return null;
    const rows = [];
    if (info.mode === 'off') {
      rows.push(<div key="off">已按「不带历史用例」提交，本次按全新需求评审。</div>);
    } else if (info.count > 0) {
      rows.push(
        <div key="cnt">
          已带入历史用例 <Text strong>{info.count}</Text> 条
          {`（${info.scope || '未标注'}${info.version ? ' / ' + info.version : ''}）`}
          ，精简级别 <Text strong>{info.level || '-'}</Text> 后约{' '}
          <Text strong>{info.chars}</Text> 字；来源：
          {info.source === 'metadata' ? '结构化基线（精确取最新版本）'
            : info.source === 'vector' ? '向量库兜底（该页面无结构化基线）' : info.source}。
        </div>
      );
    } else {
      rows.push(<div key="none">该模块/页面暂无可召回的历史用例基线，本次按全新需求评审。</div>);
    }
    (info.trimmed || []).forEach((t, i) => rows.push(
      <div key={`t${i}`}>
        ✂️ 输入超预算已裁剪「{t.block}」：{t.before} 字 → {t.after} 字（{t.action}）
      </div>
    ));
    if (info.docTruncated) {
      rows.push(<div key="dt">⚠️ 需求/文档超预算被截断，部分内容未进入本次评审，建议拆分文档后重评。</div>);
    }
    if ((info.degraded || []).length) {
      rows.push(<div key="dg">⚠️ 子Skill未产出结果：{info.degraded.join('、')}，本报告缺少对应章节，建议重试。</div>);
    }
    const hasWarn = info.docTruncated || (info.trimmed || []).length > 0 || (info.degraded || []).length > 0;
    return (
      <Alert
        type={hasWarn ? 'warning' : 'info'}
        showIcon
        style={{ marginBottom: 16 }}
        message="本次评审的上下文带入情况"
        description={<div style={{ fontSize: 13, lineHeight: 1.9 }}>{rows}</div>}
      />
    );
  };


  // 「建议删除」确认卡片（默认折叠为一行状态；有清单且未处理时展开）
  const renderDeletionGate = () => {
    if (!pendingDeletions.length) {
      return mergeMeta && (mergeMeta.unmatched_names || []).length ? (
        <div style={{
          marginBottom: 16, padding: '10px 12px', background: '#fffbe6',
          border: '1px solid #ffe58f', borderRadius: 6
        }}>
          <Text style={{ fontSize: 12 }}>
            以下「修改用例」的名称与基线对不上，也无法自动定位，已降级为新增追加（内容未丢失，但可能与原用例重复）：
            <b>{(mergeMeta.unmatched_names || []).join('、')}</b>
          </Text>
        </div>
      ) : null;
    }
    if (deletionResolved) {
      return (
        <div style={{
          marginBottom: 16, padding: '8px 12px', background: '#f6ffed',
          border: '1px solid #b7eb8f', borderRadius: 6, display: 'flex',
          alignItems: 'center', justifyContent: 'space-between'
        }}>
          <Text style={{ fontSize: 12 }}>
            {deletionResolved === 'applied'
              ? `已确认删除，产物已按确认结果重新归并（原建议删除 ${pendingDeletions.length} 条）`
              : `已选择全部保留，产物未做任何删除（原建议删除 ${pendingDeletions.length} 条）`}
          </Text>
          <Button size="small" type="link" onClick={() => setDeletionResolved('')}>重新处理</Button>
        </div>
      );
    }
    return (
      <div style={{
        marginBottom: 16, padding: 12, background: '#fff7f0',
        border: '1px solid #ffbb96', borderRadius: 6
      }}>
        <div style={{ marginBottom: 8 }}>
          <Text strong style={{ fontSize: 13 }}>
            模型建议删除 {pendingDeletions.length} 条用例（默认未执行，需你确认）
          </Text>
          <div style={{ marginTop: 4 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              这些用例当前<b>仍然保留</b>在产物里。勾选后点「确认删除」才会剔除并重新归并覆盖产物；不勾选则不受影响。
            </Text>
          </div>
        </div>
        <div style={{
          maxHeight: 220, overflow: 'auto', background: '#fff',
          border: '1px solid #f0f0f0', borderRadius: 6, padding: '4px 8px'
        }}>
          {pendingDeletions.map((it, i) => (
            <div key={i} style={{ padding: '6px 0', borderBottom: '1px solid #fafafa' }}>
              <Checkbox
                checked={!!deletionChecked[it.name]}
                onChange={e => setDeletionChecked(prev => ({ ...prev, [it.name]: e.target.checked }))}
              >
                <Text style={{ fontSize: 12 }}>{it.name}</Text>
              </Checkbox>
              <div style={{ paddingLeft: 24, marginTop: 2 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {it.reason ? `原因：${it.reason}` : '模型未给出删除原因'}
                  {it.matched === false ? '（注意：基线中找不到同名用例，删除不会生效）' : ''}
                </Text>
              </div>
            </div>
          ))}
        </div>
        <Space style={{ marginTop: 10 }}>
          <Button
            danger
            size="small"
            loading={deletionApplying}
            onClick={() => handleConfirmDeletions(true)}
          >
            确认删除勾选项
          </Button>
          <Button size="small" onClick={() => handleConfirmDeletions(false)}>全部保留</Button>
          <Button
            size="small"
            type="link"
            onClick={() => {
              const all = {};
              pendingDeletions.forEach(p => { all[p.name] = true; });
              setDeletionChecked(all);
            }}
          >
            全选
          </Button>
        </Space>
      </div>
    );
  };


  // ========== 「用例资产」域：产物清单 + 评审 + 沉淀（全宽页面，不再是弹窗）==========
  // 立这一个域解决了三件事：
  //   ① 10 列评审表格不再塞进 780px 弹窗被压扁（原先只能 ellipsis 截断）；
  //   ② 不再与「生成模式」同屏叠加出两套作用域选择器（作用域已全局唯一）；
  //   ③ 同一份产物 UI 原先挂 4 处（补沉淀条）+ 2 处（评审面板），靠 slug 相等守卫防串味；
  //      现在只有一处，靠 _owner 标记从「守卫式防错」变「结构上不会错」。

  // 生成结果区只留一个跳转按钮：入口在，逻辑只在一处
  const jumpToAssets = (targetSlug, assetType = 'test_cases') => {
    if (targetSlug) {
      setHistoryAssetSlug(targetSlug);
      setHistoryAssetType(assetType);
    }
    setActiveNav('assets');
  };

  const renderAssetsJump = (assetType = 'test_cases') => (
    <div style={{ marginBottom: 16, padding: '10px 12px', background: '#e6f4ff', borderRadius: 6 }}>
      <Space wrap>
        <Button icon={<FolderOpenOutlined />} onClick={() => jumpToAssets(slug, assetType)}>
          去「用例资产」沉淀 / 评审 →
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          产物清单、质量评分、删除勾选与沉淀已统一收进该功能域
        </Text>
      </Space>
    </div>
  );


  // ========== 重新加载落盘产物（改磁盘文件后刷新预览）==========
  const handleReloadAsset = async (assetType = 'test_cases') => {
    if (!slug) return message.warning('尚未生成用例');
    try {
      const res = await getAssetFile(slug, assetType);
      const data = res.data;
      if (data && data.code === 0 && data.data) {
        if (assetType === 'test_cases') {
          if (currentMode === 'incremental') setMergedFullCases(data.data);
          else setTestCases(data.data);
        }
        message.success('已加载磁盘产物最新内容');
      } else {
        message.warning(data && data.message ? data.message : '产物文件不存在或为空');
      }
    } catch (e) {
      message.error('重新加载产物失败');
    }
  };

  // ========== 产物文件路径 + 重新加载入口（三处结果卡片复用）==========
  const renderAssetPathBar = (assetType = 'test_cases') => {
    if (!caseFilePath) return null;
    return (
      <div style={{
        marginBottom: 12,
        padding: '8px 12px',
        background: '#fffbe6',
        border: '1px solid #ffe58f',
        borderRadius: 6,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: 8
      }}>
        <Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
          本地产物：<Text code style={{ fontSize: 12 }}>{caseFilePath}</Text>
          （可用编辑器打开手动修改，改完点右侧重新加载再沉淀）
        </Text>
        <Button size="small" icon={<ReloadOutlined />} onClick={() => handleReloadAsset(assetType)}>
          重新加载最新内容
        </Button>
      </div>
    );
  };

  return (
    <>
      {/* 本组件只在 activeNav === 'generate' 时被 App 挂载，因此域内**不再**重复判
           activeNav === 'generate' —— 当天真踩过：一旦从上下文取不到 activeNav，
           所有区块条件全为 false，整页静默空白（连报错都没有）。域内只判 currentMode。 */}
          {/* ========== 用例生成域：二级模式（Segmented，原一级下拉降级到域内）========== */}
      {(
        <Card size="small" style={{ marginBottom: 20, borderRadius: 8 }}>
          <Space wrap size={12}>
            <Text strong>生成模式</Text>
            <Segmented
              value={currentMode || undefined}
              onChange={onModeChange}
              options={MODE_LIST.map(m => ({ value: m.value, label: m.short }))}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              作用域已在顶部统一选择；「沉淀已有用例」独立为「用例资产」域
            </Text>
          </Space>
        </Card>
      )}

      {/* 未选模式时的引导（避免生成域一片空白） */}
      {!currentMode && (
        <Card style={{ borderRadius: 8 }}>
          <Empty description="请选择上方的生成模式开始" />
        </Card>
      )}

      {/* ========== 2. 快速模式 ========== */}
      {currentMode === 'quick' && (
        <>
          <Card
            title="需求信息"
            style={{ marginBottom: 24, borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
          >
            <div style={{ marginBottom: 16 }}>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>上传资料（可选）</Text>
              <Upload
                beforeUpload={handleUpload}
                showUploadList={false}
                accept=".txt,.md,.pdf,.doc,.docx,.png,.jpg,.jpeg,.gif"
              >
                <Button icon={<UploadOutlined />} type="primary" ghost>
                  上传业务文档 / 页面截图
                </Button>
              </Upload>
              {uploadFileName && <Text type="secondary" style={{ marginLeft: 12 }}>已上传：{uploadFileName}</Text>}
            </div>

            {/* 作用域（模块/页面）已提升到顶部全局作用域条，此处不再重复选择器 */}

            <div style={{ marginBottom: 20 }}>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>需求描述</Text>
              <TextArea
                rows={5}
                value={requirement}
                onChange={e => setRequirement(e.target.value)}
                placeholder="请详细描述需要生成用例的业务需求，包含功能规则、校验逻辑、交互场景等"
              />
            </div>

            <Button
              onClick={handleDirectGenerate}
              loading={loading}
              type="primary"
              size="large"
              icon={<PlayCircleOutlined />}
            >
              生成测试用例
            </Button>
          </Card>

          {/* 生成用例后展示 */}
          {testCases && (
            <>
              <Card
                title="补充规则优化"
                style={{ marginBottom: 24, borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
              >
                <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>补充业务规则 / 纠错说明</Text>
                <TextArea
                  rows={3}
                  value={supplement}
                  onChange={e => setSupplement(e.target.value)}
                  placeholder="例如：必须校验验证码格式、密码错误3次锁定账号、弹窗提示文案为xxx"
                />
                <Button
                  onClick={handleRegen}
                  loading={loading}
                  type="default"
                  icon={<ReloadOutlined />}
                  style={{ marginTop: 12 }}
                >
                  重新生成用例
                </Button>
              </Card>

              <Card
                title="测试用例结果"
                style={{ borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
                extra={
                  <Space>
                    <Select value={exportFormat} onChange={setExportFormat} style={{ width: 140 }}>
                      <Option value="markdown">Markdown</Option>
                      <Option value="excel">Excel</Option>
                      <Option value="word">Word</Option>
                      <Option value="txt">TXT</Option>
                    </Select>
                    <Button icon={<DownloadOutlined />} type="primary" onClick={() => handleExport(testCases, '测试用例')}>
                      下载用例
                    </Button>
                  </Space>
                }
              >
                {historyUsed && (
                  <div style={{
                    marginBottom: 16,
                    padding: '10px 14px',
                    background: '#e6f4ff',
                    borderRadius: 6,
                    color: '#1677ff'
                  }}>
                    ✅ 本次已自动复用历史沉淀资产
                  </div>
                )}

                <div style={{ marginBottom: 16, padding: '12px 0', borderBottom: '1px solid #f0f0f0' }}>
                  <Text strong>沉淀管理</Text>
                  <Space style={{ marginLeft: 16 }} wrap>
                    <Select
                      placeholder="选择沉淀类型"
                      style={{ width: 260 }}
                      value={casePrecipitateType}
                      onChange={setCasePrecipitateType}
                    >
                      <Option value="project">沉淀到项目历史库（本模块复用）</Option>
                      <Option value="general">沉淀为通用模板（全团队复用）</Option>
                    </Select>
                    <Button
                      icon={<SaveOutlined />}
                      type="primary"
                      ghost
                      onClick={() => handlePrecipitate('test_cases', casePrecipitateType)}
                      disabled={!casePrecipitateType}
                    >
                      确认沉淀
                    </Button>
                    <Divider type="vertical" />
                    <Button
                      icon={<SafetyCertificateOutlined />}
                      onClick={() => handleScoreCases()}
                      loading={caseReviewLoading}
                    >
                      用例评审
                    </Button>
                    <Checkbox
                      checked={caseReviewUseEngineB}
                      onChange={e => setCaseReviewUseEngineB(e.target.checked)}
                    >
                      深度评分（调LLM，取消则只做代码硬指标体检）
                    </Checkbox>
                  </Space>
                </div>

                {/* 评审面板只在自己的域渲染（_owner 结构式隔离，不再靠 slug 相等守卫） */}
                {caseReviewResult && caseReviewResult._owner === 'generate' && <CaseReviewPanel C={C} />}

                {renderAssetsJump('test_cases')}

                {renderSkillStatus()}

                {renderAssetPathBar('test_cases')}

                {loading ? (
                  <div style={{ textAlign: 'center', padding: '60px 0' }}>
                    <Spin size="large" tip="正在生成用例..." />
                  </div>
                ) : (
                  <div style={{
                    padding: 20,
                    background: '#fafafa',
                    borderRadius: 6,
                    maxHeight: 700,
                    overflow: 'auto'
                  }}>
                    <MarkdownView>{testCases}</MarkdownView>
                  </div>
                )}
              </Card>
            </>
          )}
        </>
      )}

      {/* ========== 3. 评审模式 ========== */}
      {currentMode === 'review' && (
        <>
          <Card
            title="需求信息"
            style={{ marginBottom: 24, borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
          >
            <div style={{ marginBottom: 16 }}>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>上传资料（可选）</Text>
              <Upload
                beforeUpload={handleUpload}
                showUploadList={false}
                accept=".txt,.md,.pdf,.doc,.docx,.png,.jpg,.jpeg,.gif"
              >
                <Button icon={<UploadOutlined />} type="primary" ghost>
                  上传业务文档 / 页面截图
                </Button>
              </Upload>
              {uploadFileName && <Text type="secondary" style={{ marginLeft: 12 }}>已上传：{uploadFileName}</Text>}
            </div>

            {/* 作用域（模块/页面）已提升到顶部全局作用域条 */}

            <div style={{ marginBottom: 16 }}>
              <Space align="center">
                <Switch
                  checked={reviewWithHistory}
                  onChange={setReviewWithHistory}
                  disabled={!selectedModule}
                />
                <Text>带入该模块/页面的历史用例一起评审</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {selectedModule
                    ? '（后端自动取最新基线并精简后再注入，防止 token 超限）'
                    : '（未选模块：按全新需求直接评审，不召回历史用例）'}
                </Text>
              </Space>
            </div>

            <div style={{ marginBottom: 20 }}>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>需求描述（与上传文档至少填一项）</Text>
              <TextArea
                rows={5}
                value={requirement}
                onChange={e => setRequirement(e.target.value)}
                placeholder="请详细描述需要评审的业务需求；也可以只上传需求文档，让模型按文档评审"
              />
            </div>

            <Button
              onClick={handleReview}
              loading={loading}
              type="primary"
              size="large"
            >
              生成评审报告
            </Button>
          </Card>

          {/* 生成报告后展示 */}
          {reviewReport && (
            <Card
              title="评审报告结果"
              style={{ borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
              extra={
                <Space>
                  <Select value={exportFormat} onChange={setExportFormat} style={{ width: 140 }}>
                    <Option value="markdown">Markdown</Option>
                    <Option value="word">Word</Option>
                    <Option value="txt">TXT</Option>
                  </Select>
                  <Button icon={<DownloadOutlined />} type="primary" onClick={() => handleExport(reviewReport, '评审报告')}>
                    导出报告
                  </Button>
                </Space>
              }
            >
              {historyUsed && (
                <div style={{
                  marginBottom: 16,
                  padding: '10px 14px',
                  background: '#e6f4ff',
                  borderRadius: 6,
                  color: '#1677ff'
                }}>
                  ✅ 本次已自动复用历史沉淀资产
                </div>
              )}

                {renderReviewHistoryInfo()}

                <div style={{ marginBottom: 16, padding: '12px 0', borderBottom: '1px solid #f0f0f0' }}>
                <Text strong>沉淀管理</Text>
                <Space style={{ marginLeft: 16 }}>
                  <Select
                    placeholder="选择沉淀类型"
                    style={{ width: 260 }}
                    value={reportPrecipitateType}
                    onChange={setReportPrecipitateType}
                  >
                    <Option value="project">沉淀到项目历史库（本模块复用）</Option>
                    <Option value="general">沉淀为通用评审模板</Option>
                  </Select>
                  <Button
                    icon={<SaveOutlined />}
                    type="primary"
                    ghost
                    onClick={() => handlePrecipitate('review_report', reportPrecipitateType)}
                    disabled={!reportPrecipitateType}
                  >
                    确认沉淀
                  </Button>
                </Space>
              </div>

              {renderSkillStatus()}

              <div style={{
                padding: 20,
                background: '#fafafa',
                borderRadius: 6,
                maxHeight: 600,
                overflow: 'auto'
              }}>
                <MarkdownView>{reviewReport}</MarkdownView>
              </div>
            </Card>
          )}
        </>
      )}

      {/* ========== 4. 增量迭代模式 ========== */}
      {currentMode === 'incremental' && (
        <>
          <Card
            title="迭代变更信息（第一步：分析影响面 → 第二步：生成增量用例）"
            style={{ marginBottom: 24, borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
          >
            {/* 作用域（模块/页面）已提升到顶部全局作用域条；
                本域只保留「历史版本号」——它在各域语义不同（生成侧=建议版本，资产侧=筛选条件），
                强行进全局作用域反而制造歧义，故留在域内。 */}

            <div style={{ marginBottom: 16 }}>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
                历史版本号（可选，仅列出已沉淀的真实版本）
              </Text>
              <Space.Compact style={{ width: '100%' }}>
                <Select
                  style={{ width: '100%' }}
                  placeholder={
                    incrementalModule
                      ? (historyVersionOptions.length
                        ? '不选=默认召回最新版本'
                        : '该模块/页面暂无已沉淀版本，请先沉淀一版基线')
                      : '请先选择业务模块'
                  }
                  value={historyVersion || undefined}
                  onChange={v => setHistoryVersion(v || '')}
                  allowClear
                  showSearch
                  loading={historyVersionLoading}
                  disabled={!incrementalModule}
                  notFoundContent={historyVersionLoading ? '加载中…' : '暂无已沉淀版本'}
                  onDropdownVisibleChange={(open) => { if (open) loadHistoryVersions(incrementalModule, incrementalPage); }}
                >
                  {historyVersionOptions.map((it, idx) => (
                    <Option
                      key={`${it.page || 'all'}-${it.version}-${idx}`}
                      value={it.version}
                      label={it.version}
                    >
                      {it.version}
                      {it.case_total ? `（${it.case_total}条` : '（'}
                      {!incrementalPage && it.page ? ` · ${(PAGE_LIST.find(p => p.value === it.page) || {}).label || it.page}` : ''}
                      {it.create_time ? ` · ${it.create_time}` : ''}）
                    </Option>
                  ))}
                </Select>
                <Button
                  icon={<ReloadOutlined />}
                  onClick={() => loadHistoryVersions(incrementalModule, incrementalPage)}
                  disabled={!incrementalModule}
                  title="刷新已沉淀版本列表"
                />
              </Space.Compact>
            </div>

            <div style={{ marginBottom: 20 }}>
              <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>变更点描述 *</Text>
              <TextArea
                rows={5}
                value={changeDescription}
                onChange={e => setChangeDescription(e.target.value)}
                placeholder="请详细描述本次迭代的变更内容，例如：&#10;1. 新增导出功能，仅管理员可见&#10;2. 列表新增操作人列&#10;3. 移除文件类型筛选条件"
              />
            </div>

            <Space>
              <Button
                onClick={handleAnalyzeImpact}
                loading={impactLoading}
                size="large"
                icon={<PlayCircleOutlined />}
              >
                第一步：分析影响面
              </Button>
              <Button
                onClick={handleIncrementalGenerate}
                loading={incrementalLoading}
                type="primary"
                size="large"
                icon={<PlayCircleOutlined />}
                disabled={!impactAnalysis.trim()}
              >
                第二步：生成增量用例
              </Button>
            </Space>
          </Card>

          {/* 影响面分析结果（用户确认闸门） */}
          {impactAnalysis && (
            <Card
              title="影响面分析结果（请确认或修改后，再执行第二步）"
              style={{ marginBottom: 24, borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
            >
              <TextArea
                rows={14}
                value={impactAnalysis}
                onChange={e => setImpactAnalysis(e.target.value)}
              />
            </Card>
          )}

          {/* 待确认疑点：逐条答复 → 持久化为该页面澄清知识，下次分析自动复用（不再重复提问） */}
          {pendingQuestions.length > 0 && (
            <Card
              title={`待确认疑点（${pendingQuestions.length} 项）— 答复后会被记住，下次同页面分析不再重复提问`}
              style={{ marginBottom: 24, borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
            >
              {pendingQuestions.map(q => (
                <div key={q.id} style={{ marginBottom: 12 }}>
                  <Text strong>{q.id}：{q.question}</Text>
                  {q.default ? (
                    <Text type="secondary" style={{ marginLeft: 8 }}>（建议：{q.default}）</Text>
                  ) : null}
                  <Input
                    style={{ marginTop: 6 }}
                    placeholder="填写你的确认结论（可留空表示暂不确认）"
                    value={clarificationAnswers[q.id] || ''}
                    onChange={e => setClarificationAnswers(prev => ({ ...prev, [q.id]: e.target.value }))}
                  />
                </div>
              ))}
              <Space style={{ marginTop: 8 }}>
                <Button
                  type="primary"
                  icon={<SaveOutlined />}
                  loading={savingClarifications}
                  onClick={handleSaveClarifications}
                >
                  保存澄清答复并并入影响面
                </Button>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  保存后答复会随第二步的「影响面分析」一起带入生成
                </Text>
              </Space>
            </Card>
          )}

          {/* ========== 方案C：规则补丁 ==========
              批2b 已搬到「规则管理」域（规则补丁改的是规则文件，归属规则域更合直觉；
              且补丁落盘后会在规则 front matter 标注「需求变更改动」）。此处不再渲染。 */}

          {/* 生成结果展示 */}
          {incrementalCases && (
            <Card
              title="生成结果"
              style={{ borderRadius: 8, boxShadow: '0 2px 8px rgba(0,0,0,0.06)' }}
            >
              {renderDeletionGate()}
              <Tabs defaultActiveKey="incremental">
                <TabPane tab="增量用例（仅变更部分）" key="incremental">
                  <Space style={{ marginBottom: 16 }} wrap>
                    <Select value={exportFormat} onChange={setExportFormat} style={{ width: 140 }}>
                      <Option value="markdown">Markdown</Option>
                      <Option value="excel">Excel</Option>
                      <Option value="word">Word</Option>
                      <Option value="txt">TXT</Option>
                    </Select>
                    <Button
                      icon={<DownloadOutlined />}
                      type="primary"
                      onClick={() => handleExport(incrementalCases, '增量测试用例')}
                    >
                      导出增量用例
                    </Button>
                  </Space>
                  <div style={{
                    padding: 20,
                    background: '#fafafa',
                    borderRadius: 6,
                    maxHeight: 600,
                    overflow: 'auto'
                  }}>
                    <MarkdownView>{incrementalCases}</MarkdownView>
                  </div>
                </TabPane>

                <TabPane tab="合并后全量用例" key="merged">
                  {pendingDeletions.length > 0 && !deletionResolved && (
                    <div style={{
                      marginBottom: 12, padding: '8px 12px', background: '#fffbe6',
                      border: '1px solid #ffe58f', borderRadius: 6
                    }}>
                      <Text style={{ fontSize: 12 }}>
                        还有 {pendingDeletions.length} 条「建议删除」用例未确认，当前产物仍然包含它们。
                        若现在沉淀，这些用例会一并进入新版本基线。
                      </Text>
                    </div>
                  )}
                  <Space style={{ marginBottom: 16 }} wrap>
                    <Select value={exportFormat} onChange={setExportFormat} style={{ width: 140 }}>
                      <Option value="markdown">Markdown</Option>
                      <Option value="excel">Excel</Option>
                      <Option value="word">Word</Option>
                      <Option value="txt">TXT</Option>
                    </Select>
                    <Button
                      icon={<DownloadOutlined />}
                      type="primary"
                      onClick={() => handleExport(mergedFullCases, '合并后全量测试用例')}
                    >
                      导出全量用例
                    </Button>

                    <Divider type="vertical" />

                    <Select
                      placeholder="选择沉淀类型"
                      style={{ width: 260 }}
                      value={casePrecipitateType}
                      onChange={setCasePrecipitateType}
                    >
                      <Option value="project">沉淀到项目历史库（作为新版本）</Option>
                      <Option value="general">沉淀为通用模板</Option>
                    </Select>
                    <Button
                      icon={<SaveOutlined />}
                      type="primary"
                      ghost
                      onClick={() => handlePrecipitate('test_cases', casePrecipitateType)}
                      disabled={!casePrecipitateType}
                    >
                      确认沉淀新版本
                    </Button>
                  </Space>
                  {renderAssetsJump('test_cases')}
                  {renderAssetPathBar('test_cases')}
                  <div style={{
                    padding: 20,
                    background: '#fafafa',
                    borderRadius: 6,
                    maxHeight: 600,
                    overflow: 'auto'
                  }}>
                    <MarkdownView>{mergedFullCases}</MarkdownView>
                  </div>
                </TabPane>
              </Tabs>
            </Card>
          )}
        </>
      )}

      {/* ========== 5. 完整模式（保留原全流程）========== */}
      {currentMode === 'full' && (
        <div style={{ marginBottom: 30 }}>
          <Steps current={currentStep}>
            <Step title="上传需求" description="上传文档，选择模块" />
            <Step title="需求评审" description="生成评审报告" />
            <Step title="生成用例" description="输出用例并沉淀" />
          </Steps>
        </div>
      )}

      {currentMode === 'full' && (
        <>
          <Card title="1. 上传需求资料" style={{ marginBottom: 20, borderRadius: 8 }}>
            <Space wrap>
              <Upload beforeUpload={handleUpload} showUploadList={false}>
                <Button icon={<UploadOutlined />} type="primary">上传业务文档</Button>
              </Upload>
              {uploadFileName && <Text type="secondary">已上传：{uploadFileName}</Text>}
              <Text type="secondary" style={{ fontSize: 12 }}>
                作用域（模块 / 页面）已提升到顶部全局作用域条
              </Text>
            </Space>
          </Card>

          <Card title="2. 输入需求描述" style={{ marginBottom: 20, borderRadius: 8 }}>
            <TextArea rows={3} value={requirement} onChange={e => setRequirement(e.target.value)} />
            <Button
              onClick={handleReview}
              loading={loading}
              type="primary"
              style={{ marginTop: 12 }}
              disabled={currentStep >= 1}
            >
              开始需求评审
            </Button>
          </Card>

          {currentStep >= 1 && (
            <Card title="3. 需求评审报告" style={{ marginBottom: 20, borderRadius: 8 }}>
              <Space style={{ marginBottom: 16 }} wrap>
                <Select value={exportFormat} onChange={setExportFormat} style={{ width: 140 }}>
                  <Option value="markdown">Markdown</Option>
                  <Option value="word">Word</Option>
                  <Option value="txt">TXT</Option>
                </Select>
                <Button icon={<DownloadOutlined />} onClick={() => handleExport(reviewReport, '评审报告')}>
                  导出报告
                </Button>
                <Button
                  onClick={handleGenerateFull}
                  loading={loading}
                  type="primary"
                  disabled={currentStep >= 2}
                >
                  生成测试用例
                </Button>
              </Space>
              {renderReviewHistoryInfo()}
              <div style={{ padding: 16, background: '#fafafa', borderRadius: 6, maxHeight: 400, overflow: 'auto' }}>
                <MarkdownView>{reviewReport}</MarkdownView>
              </div>
            </Card>
          )}

          {currentStep >= 2 && (
            <>
              <Card title="4. 补充业务规则" style={{ marginBottom: 20, borderRadius: 8 }}>
                <TextArea rows={2} value={supplement} onChange={e => setSupplement(e.target.value)} />
                <Button onClick={handleRegen} loading={loading} icon={<ReloadOutlined />} style={{ marginTop: 10 }}>
                  重新生成用例
                </Button>
              </Card>

              <Card title="5. 测试用例结果" style={{ borderRadius: 8 }}>
                <Space style={{ marginBottom: 16 }} wrap>
                  <Select value={exportFormat} onChange={setExportFormat} style={{ width: 140 }}>
                    <Option value="markdown">Markdown</Option>
                    <Option value="excel">Excel</Option>
                    <Option value="word">Word</Option>
                    <Option value="txt">TXT</Option>
                  </Select>
                  <Button icon={<DownloadOutlined />} type="primary" onClick={() => handleExport(testCases, '测试用例')}>
                    下载用例
                  </Button>
                  <Divider type="vertical" />
                  <Select
                    placeholder="选择沉淀类型"
                    style={{ width: 260 }}
                    value={casePrecipitateType}
                    onChange={setCasePrecipitateType}
                  >
                    <Option value="project">沉淀到项目历史库</Option>
                    <Option value="general">沉淀为通用模板</Option>
                  </Select>
                  <Button
                    icon={<SaveOutlined />}
                    onClick={() => handlePrecipitate('test_cases', casePrecipitateType)}
                    disabled={!casePrecipitateType}
                  >
                    确认沉淀
                  </Button>
                </Space>
                {renderAssetsJump('test_cases')}
                {renderAssetPathBar('test_cases')}
                <div style={{ padding: 16, background: '#fafafa', borderRadius: 6 }}>
                  <MarkdownView>{testCases}</MarkdownView>
                </div>
              </Card>
            </>
          )}
        </>
      )}
    </>
  );
}
