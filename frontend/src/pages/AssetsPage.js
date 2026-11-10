import { Button, Input, Card, message, Space, Select, Tag, Switch, Alert, Table, Empty, Typography, Modal } from 'antd';
const { Text } = Typography;
const { TextArea } = Input;
const { Option } = Select;
import { ReloadOutlined, SaveOutlined, SafetyCertificateOutlined, DeleteOutlined, EditOutlined } from '@ant-design/icons';
import { getAssetFile } from '../api';
import { labelOfPage, scopeLabel } from '../config/scopes';
import React from 'react';
import MarkdownView from '../components/MarkdownView';
import CaseReviewPanel from '../components/CaseReviewPanel';

// 批2a：从 App.js 机械拆分而出（纯搬运，逻辑未改）。
// 下方解构列表由脚本按「本文件实际用到的 App.js 作用域标识符」自动推导；
// 状态与业务逻辑仍集中在 App.js（控制器），本文件只负责展示。
// ⚠️ 新增 state/函数后，需同步补进 App.js 的 C 对象。
export default function AssetsPage({ C }) {
  const {
    assetEdit,
    assetEditContent,
    assetPreview,
    assetSaving,
    caseReviewLoading,
    caseReviewResult,
    caseReviewUseEngineB,
    caseScores,
    closeAssetEdit,
    handleDeleteAsset,
    handleHistoryPrecipitate,
    handleOpenAssetEdit,
    handleSaveAssetContent,
    handleScoreCases,
    historyAssetModule,
    historyAssetNextVersion,
    historyAssetPage,
    historyAssetSlug,
    historyAssetType,
    historyAssetVersion,
    historyPrecipitateType,
    historyPrecipitating,
    loadCaseScores,
    loadOutputFiles,
    loading,
    outputFiles,
    outputFilesEmpty,
    outputFilesLoading,
    selectedModule,
    selectedPage,
    setAssetEditContent,
    setAssetPreview,
    setCaseReviewUseEngineB,
    setHistoryAssetSlug,
    setHistoryAssetType,
    setHistoryAssetVersion,
    setHistoryPrecipitateType,
    slug,
  } = C;

  // 删除 / 编辑后统一刷新本域（清单 + 评分 sidecar）
  const reloadAssetDomain = () => {
    loadOutputFiles(historyAssetModule, historyAssetPage);
    loadCaseScores(selectedModule);
  };


  // 产物行 → 评分记录 / 均分 / 建议删除数（读 sidecar，不重跑评审）
  const scoreRecordOf = (f) => {
    if (!caseScores || !f.version) return null;
    return caseScores[`version::${f.page || 'all'}::${f.version}`] || null;
  };
  const scoreAvgOf = (rec) => {
    const cs = Object.values((rec && rec.cases) || {});
    if (!cs.length) return null;
    return (cs.reduce((s, c) => s + (c.total || 0), 0) / cs.length).toFixed(1);
  };
  const scoreDelCountOf = (rec) =>
    Object.values((rec && rec.cases) || {}).filter(c => c.decision === 'suggest_delete').length;

  // 按 slug 读产物内容（「用例资产」域用；生成域的 handleReloadAsset 绑定的是当前生成 slug）
  const handleReloadAssetBySlug = async (targetSlug, assetType = 'test_cases') => {
    if (!targetSlug) return message.warning('请先选择产物');
    try {
      const res = await getAssetFile(targetSlug, assetType);
      const data = res.data || {};
      if (data.code === 0 && data.data) {
        setAssetPreview(data.data);
        message.success('已加载产物最新内容');
      } else {
        message.error(data.message || '读取产物失败');
      }
    } catch (e) {
      message.error('读取产物失败');
    }
  };

  const renderAssetDomain = () => (
    <>
      <Card
        title={`用例资产${selectedModule ? '（' + scopeLabel(selectedModule, selectedPage) + '）' : ''}`}
        extra={
          <Button size="small" icon={<ReloadOutlined />}
            onClick={() => { loadOutputFiles(historyAssetModule, historyAssetPage); loadCaseScores(selectedModule); }}
            disabled={!selectedModule}>
            刷新清单
          </Button>
        }
        style={{ marginBottom: 20, borderRadius: 8 }}
      >
        <Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 12 }}>
          选一行产物 → 在下方评审 / 勾选删除项 → 沉淀时自动剔除。归属取自落盘时记录的产物索引（不靠文件名语义猜）；
          报告类产物不在此列（见「报告中心」）。操作列的
          <Text strong>编辑</Text> 直接改落盘文件、
          <Text strong>删除</Text> 只删本地产物文件（<Text type="warning">不动已沉淀的基线</Text>）。
        </Text>
        {!selectedModule ? (
          <Empty description="请先在顶部选择业务模块" />
        ) : (
          <Table
            size="small"
            rowKey="filename"
            loading={outputFilesLoading}
            dataSource={outputFiles}
            scroll={{ x: 1300 }}
            pagination={false}
            locale={{ emptyText: `「${scopeLabel(selectedModule, selectedPage)}」下没有可沉淀的本地产物，请确认页面是否选对` }}
            onRow={(r) => ({
              onClick: () => {
                setHistoryAssetType(r.asset_type);
                setHistoryAssetSlug(r.slug);
                if (r.version) setHistoryAssetVersion(r.version);
                setAssetPreview('');
              },
              style: {
                cursor: 'pointer',
                background: (historyAssetSlug === r.slug && historyAssetType === r.asset_type) ? '#e6f4ff' : undefined,
              },
            })}
            columns={[
              { title: '产物文件', dataIndex: 'filename', width: 340, ellipsis: true },
              { title: '页面', width: 130, render: (_, r) => (r.page ? labelOfPage(r.page) : '未知') },
              { title: '版本', dataIndex: 'version', width: 100, render: (v) => v || '未记录' },
              { title: '生成时间', dataIndex: 'mtime', width: 160 },
              { title: '大小', width: 90, render: (_, r) => `${(r.size / 1024).toFixed(1)}KB` },
              {
                title: '质量均分', width: 100,
                render: (_, r) => {
                  const rec = scoreRecordOf(r);
                  const avg = rec ? scoreAvgOf(rec) : null;
                  if (avg === null) return <Text type="secondary">未评审</Text>;
                  const n = Number(avg);
                  return (
                    <Text strong style={{ color: n >= 85 ? '#52c41a' : n >= 60 ? '#fa8c16' : '#f5222d' }}>
                      {avg}
                    </Text>
                  );
                },
              },
              {
                title: '建议删除', width: 100,
                render: (_, r) => {
                  const rec = scoreRecordOf(r);
                  if (!rec) return <Text type="secondary">-</Text>;
                  const n = scoreDelCountOf(rec);
                  return n > 0 ? <Tag color="error">{n}</Tag> : <Tag color="success">0</Tag>;
                },
              },
              {
                title: '操作', width: 210, fixed: 'right',
                render: (_, r) => (
                  <Space size={0}>
                    <Button size="small" type="link"
                      onClick={(e) => { e.stopPropagation(); handleScoreCases(r.slug, 'asset'); }}>
                      评审
                    </Button>
                    <Button size="small" type="link"
                      onClick={(e) => { e.stopPropagation(); handleReloadAssetBySlug(r.slug, r.asset_type); }}>
                      预览
                    </Button>
                    <Button size="small" type="link" icon={<EditOutlined />}
                      onClick={(e) => { e.stopPropagation(); handleOpenAssetEdit(r, reloadAssetDomain); }}>
                      编辑
                    </Button>
                    <Button size="small" type="link" danger icon={<DeleteOutlined />}
                      onClick={(e) => { e.stopPropagation(); handleDeleteAsset(r, reloadAssetDomain); }}>
                      删除
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        )}
        {outputFilesEmpty && (
          <Alert style={{ marginTop: 12 }} type="warning" showIcon
            message={`找不到相关页面的用例产物：「${scopeLabel(selectedModule, selectedPage)}」下没有可沉淀的本地产物。请确认页面是否选对，或先执行一次用例生成。`} />
        )}
      </Card>

      {/* 评审：选中产物后就地展开，全宽列宽充足，不再截断 */}
      <Card size="small" title="用例评审" style={{ marginBottom: 20, borderRadius: 8 }}>
        <Space wrap style={{ marginBottom: 12 }}>
          <Button icon={<SafetyCertificateOutlined />} loading={caseReviewLoading}
            onClick={() => handleScoreCases(historyAssetSlug, 'asset')}
            disabled={!historyAssetSlug || historyAssetType !== 'test_cases'}>
            评审选中产物
          </Button>
          <Switch size="small" checked={caseReviewUseEngineB} onChange={setCaseReviewUseEngineB} />
          <Text type="secondary" style={{ fontSize: 12 }}>深度评分（调LLM，取消则只做代码硬指标体检）</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {historyAssetSlug ? `已选：${historyAssetSlug}` : '请在上方清单点选一个用例产物'}
          </Text>
        </Space>
        {caseReviewResult && caseReviewResult._owner === 'asset' && caseReviewResult.slug === historyAssetSlug
          ? <CaseReviewPanel C={C} />
          : <Text type="secondary" style={{ fontSize: 12 }}>评审结果将展示在这里（勾选的删除项会在沉淀时自动剔除）</Text>}
      </Card>

      {/* 沉淀：产物 = 基线（剔除后同步覆盖产物文件，保持两者一致） */}
      <Card size="small" title="沉淀到基线" style={{ marginBottom: 20, borderRadius: 8 }}>
        <Space wrap>
          <Select placeholder="沉淀类型" style={{ width: 220 }}
            value={historyPrecipitateType || undefined} onChange={setHistoryPrecipitateType}>
            <Option value="project">沉淀到项目历史库</Option>
            <Option value="general">沉淀为通用模板</Option>
          </Select>
          <Input placeholder={historyAssetNextVersion ? `下一可用：${historyAssetNextVersion}` : '版本号，如 v1.0'}
            style={{ width: 170 }} value={historyAssetVersion}
            onChange={e => setHistoryAssetVersion(e.target.value)} />
          <Button type="primary" icon={<SaveOutlined />} loading={historyPrecipitating}
            onClick={handleHistoryPrecipitate}
            disabled={!historyAssetSlug || !historyPrecipitateType}>
            确认沉淀
          </Button>
        </Space>
        <Text type="secondary" style={{ display: 'block', marginTop: 8, fontSize: 12 }}>
          沉淀前会按上方评审勾选自动剔除差评用例；产物文件与结构化基线同步覆盖，旧版本仍可追溯。
        </Text>
      </Card>

      {/* 产物预览（折叠只读，改磁盘文件后点「重新加载」刷新） */}
      {assetPreview && (
        <Card size="small" title="产物预览" style={{ borderRadius: 8 }}
          extra={
            <Button size="small" icon={<ReloadOutlined />}
              onClick={() => handleReloadAssetBySlug(historyAssetSlug, historyAssetType)}>
              重新加载
            </Button>
          }>
          <div style={{ padding: 16, background: '#fafafa', borderRadius: 6, maxHeight: 560, overflow: 'auto' }}>
            <MarkdownView>{assetPreview}</MarkdownView>
          </div>
        </Card>
      )}
    </>
  );

  return (
    <>
      {renderAssetDomain()}

      {/* ==================== 产物编辑 ==================== */}
      <Modal
        title={assetEdit ? `编辑产物：${assetEdit.filename}` : ''}
        open={Boolean(assetEdit)}
        onCancel={closeAssetEdit}
        width={1080}
        footer={
          <Space>
            <Button onClick={closeAssetEdit}>放弃修改</Button>
            <Button type="primary" icon={<SaveOutlined />} loading={assetSaving}
              onClick={handleSaveAssetContent}>
              保存产物
            </Button>
          </Space>
        }
      >
        {assetEdit && (
          <>
            <Alert
              type="warning" showIcon style={{ marginBottom: 10 }}
              message="编辑产物 ≠ 修改已沉淀的基线"
              description={`归属（模块 ${assetEdit.module || '-'} / 页面 ${assetEdit.page ? labelOfPage(assetEdit.page) : '模块层'} / 版本 ${assetEdit.version || '未记录'}）会从产物索引原样沿用。产物改了但基线没改时，两者会不一致 —— 需要重新沉淀一次才会同步。`}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              编辑的是落盘文件（9 列用例表格的 markdown）。保存后「预览」与「补沉淀」都会用新内容。
            </Text>
            <TextArea
              value={assetEditContent}
              onChange={e => setAssetEditContent(e.target.value)}
              autoSize={{ minRows: 18, maxRows: 30 }}
              style={{ marginTop: 8, fontFamily: 'Consolas, Monaco, monospace', fontSize: 12 }}
            />
          </>
        )}
      </Modal>
    </>
  );
}
