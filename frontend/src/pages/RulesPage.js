import React, { useState } from 'react';
import {
  Card, Button, Space, Tag, Typography, Empty, Table, Modal, Input,
  Alert, Radio, Tooltip, Descriptions, Upload, message, Checkbox
} from 'antd';
const { Text, Paragraph } = Typography;
const { TextArea } = Input;
import {
  EyeOutlined, EditOutlined, UploadOutlined, ReloadOutlined, SaveOutlined,
  PlayCircleOutlined, ImportOutlined, FileTextOutlined, HistoryOutlined, DownloadOutlined
} from '@ant-design/icons';
import { labelOfModule, labelOfPage, scopeLabel } from '../config/scopes';
import MarkdownView from '../components/MarkdownView';

// ==================== 规则管理域 ====================
// 概念对齐（很重要）：本框架里「业务 skill 规则」= skills/req-test-cases/references/ 下的页面规则文件。
//   · 上传一个「完整的新 skill 目录」不会被启用 —— skill 本体由流程代码写死；
//   · 「同一页面的规则是否已存在」的判据 = front matter 的 (biz_module, page) 等值匹配，
//     **不看文件名、不看目录名**（plugin_loader 用 rglob 递归扫描）；
//   · 保存后后端会 load_all_skills() 自动热重载 ⇒ **无需重启后端**即生效。
// 批2b：从「用例生成 → 增量迭代」域把「规则补丁」搬到这里（规则补丁改的是规则文件，归属规则域），
//       并新增：规则文件列表 / 查看 / 上传替换 / 页面内编辑。

const CHANGE_ORIGIN_REQUIREMENT = 'requirement_change';

const readFileAsText = (file) => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result || ''));
  reader.onerror = () => reject(new Error('读取文件失败'));
  reader.readAsText(file, 'utf-8');
});

export default function RulesPage({ C }) {
  const {
    selectedModule,
    selectedPage,
    // 规则文件（当前作用域精确命中）
    ruleFiles = [],
    loadRuleFiles,
    // 全部规则文件
    ruleFilesAll = [],
    ruleFilesAllLoading,
    loadAllRuleFiles,
    // 查看 / 编辑
    ruleModal,
    ruleModalContent,
    ruleModalMode,
    ruleSaving,
    setRuleModalContent,
    setRuleModalMode,
    closeRuleModal,
    handleOpenRuleFile,
    handleSaveRuleContent,
    // 上传 / 替换
    rulePrecheck,
    handlePrecheckRuleTarget,
    clearRulePrecheck,
    handleUploadRuleFile,
    // 规则补丁（需求变更改动）
    rulePatches = [],
    patchChecked = {},
    setPatchChecked,
    patchLoading,
    patchApplying,
    patchResult,
    patchSummary,
    handleProposeRulePatch,
    handleApplyRulePatch,
    handleDownloadRuleTemplate,
    rulePatchChangeDesc,
    setRulePatchChangeDesc,
    rulePatchImpact,
    setRulePatchImpact,
    changeDescription,
    impactAnalysis,
  } = C;

  // ---------- 本地 UI 状态（纯展示，不涉及业务数据）----------
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFilename, setUploadFilename] = useState('');
  const [uploadContent, setUploadContent] = useState('');
  const [uploadDescription, setUploadDescription] = useState('');
  const [uploadMode, setUploadMode] = useState('override');
  const [editCompensate, setEditCompensate] = useState(false);

  const scopeText = selectedModule
    ? scopeLabel(selectedModule, selectedPage)
    : '未选择模块';

  // 「需求变更改动」标注（规则补丁落盘时写入 front matter，见 rule_patch_applier.stamp_change_origin）
  const renderOriginTag = (rec) => {
    if (rec.last_change_origin !== CHANGE_ORIGIN_REQUIREMENT) {
      return <Text type="secondary" style={{ fontSize: 12 }}>人工维护</Text>;
    }
    const ids = rec.changed_rule_ids || [];
    const tip = (
      <div style={{ fontSize: 12 }}>
        <div>来源：需求变更（规则补丁）</div>
        {rec.last_change_at && <div>时间：{rec.last_change_at}</div>}
        {ids.length > 0 && <div>改动编号：{ids.join(', ')}</div>}
        {rec.last_change_note && <div>变更描述：{rec.last_change_note}</div>}
      </div>
    );
    return (
      <Tooltip title={tip}>
        <Tag color="gold" icon={<HistoryOutlined />}>需求变更改动{ids.length ? `（${ids.length}）` : ''}</Tag>
      </Tooltip>
    );
  };

  const ruleColumns = [
    { title: '规则文件', dataIndex: 'filename', width: 240, ellipsis: true },
    {
      title: '归属页面', width: 120,
      render: (_, r) => (r.page ? labelOfPage(r.page) : '模块层'),
    },
    {
      title: '作用域', width: 80,
      render: (_, r) => <Tag color={r.rule_scope === 'common' ? 'default' : 'blue'}>{r.rule_scope || 'module'}</Tag>,
    },
    {
      title: '编号', width: 190,
      render: (_, r) => (r.rule_ids && r.rule_ids.length
        ? <Text style={{ fontSize: 12 }}>{r.rule_ids.join(', ')}</Text>
        : <Text type="secondary" style={{ fontSize: 12 }}>无编号</Text>),
    },
    {
      title: '字数', width: 80,
      render: (_, r) => `${r.char_count || 0}`,
    },
    { title: '来源标注', width: 150, render: (_, r) => renderOriginTag(r) },
    {
      title: '操作', width: 130, fixed: 'right',
      render: (_, r) => (
        <Space size={0}>
          <Button size="small" type="link" icon={<EyeOutlined />}
            onClick={() => handleOpenRuleFile(r, 'view')}>查看</Button>
          <Button size="small" type="link" icon={<EditOutlined />}
            onClick={() => { setEditCompensate(false); handleOpenRuleFile(r, 'edit'); }}>编辑</Button>
        </Space>
      ),
    },
  ];

  // ==================== 1. 当前作用域规则 ====================
  const renderScopedRules = () => (
    <Card
      title={`当前作用域规则${selectedModule ? '（' + scopeText + '）' : ''}`}
      style={{ marginBottom: 20, borderRadius: 8 }}
      extra={
        <Button size="small" icon={<ReloadOutlined />} disabled={!selectedModule}
          onClick={() => loadRuleFiles(selectedModule, selectedPage)}>
          刷新
        </Button>
      }
    >
      <Paragraph type="secondary" style={{ fontSize: 12 }}>
        这里列出的就是<Text strong>生成时会被实际注入</Text>的规则文件 —— 判据是 front matter 的
        <Text code>biz_module</Text> + <Text code>page</Text> 与当前作用域<Text strong>等值匹配</Text>，
        与文件名、目录名无关。未选页面时只有「通用规则」会生效，页面级精确注入为空。
      </Paragraph>
      {!selectedModule ? (
        <Empty description="请先在顶部选择业务模块（module 是规则归属的锚点）" />
      ) : (
        <>
          <Table
            size="small"
            rowKey="rel"
            dataSource={ruleFiles}
            columns={ruleColumns}
            pagination={false}
            scroll={{ x: 1040 }}
            locale={{ emptyText: '当前作用域下没有专属规则文件（生成时只用通用规则 + 语义兜底）' }}
          />
          {ruleFiles.length === 0 && (
            <Alert
              style={{ marginTop: 12 }}
              type="warning"
              showIcon
              message="当前页面还没有专属规则文件"
              description="这不是报错：生成时仍会注入通用规则并走语义兜底，但页面级精确注入为空。可在下方「上传 / 新建业务规则」补一份。"
            />
          )}
        </>
      )}
    </Card>
  );

  // ==================== 2. 上传 / 新建业务规则 ====================
  const openUpload = () => {
    if (!selectedModule) return message.warning('请先在顶部选择业务模块');
    setUploadFilename('');
    setUploadContent('');
    setUploadDescription('');
    setUploadMode('override');
    clearRulePrecheck();
    setUploadOpen(true);
  };

  const handlePickFile = async (file) => {
    try {
      const text = await readFileAsText(file);
      setUploadFilename(file.name || '');
      setUploadContent(text);
      if (!(text || '').trim()) message.warning('该文件读出来是空内容（若为二进制/图片规则请改用粘贴）');
    } catch (e) {
      message.error('读取文件失败');
    }
    return false;   // 阻止 antd 自动上传
  };

  const doPrecheck = async () => {
    if (!(uploadContent || '').trim()) return message.warning('请先选择规则文件或粘贴规则内容');
    await handlePrecheckRuleTarget({
      filename: uploadFilename,
      content: uploadContent,
      description: uploadDescription,
    });
  };

  const doUpload = async () => {
    if (!rulePrecheck) return message.warning('请先点「预检落盘位置」');
    const ok = await handleUploadRuleFile({
      filename: uploadFilename,
      content: uploadContent,
      description: uploadDescription,
      frontMatterMode: uploadMode,
    });
    if (ok) {
      setUploadOpen(false);
      setUploadFilename('');
      setUploadContent('');
      clearRulePrecheck();
    }
  };

  const renderUploadCard = () => (
    <Card
      title="上传 / 新建业务规则（= 页面规则文件）"
      style={{ marginBottom: 20, borderRadius: 8 }}
      extra={
        <Space>
          <Button size="small" icon={<DownloadOutlined />} onClick={handleDownloadRuleTemplate}>
            下载业务规则模板
          </Button>
          <Button type="primary" size="small" icon={<ImportOutlined />}
            disabled={!selectedModule} onClick={openUpload}>
            上传规则
          </Button>
        </Space>
      }
    >
      <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
        落盘位置由模块决定：该模块已有规则文件 → <Text strong>沿用其所在目录</Text>；
        该模块完全没有规则文件 → 新建 <Text code>references/{selectedModule || '<模块>'}/</Text>。
        同页面已存在 →<Text strong> 覆盖前会弹窗二次确认</Text>，旧文件先备份到
        <Text code>references/_versions/</Text>。
      </Paragraph>
      <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
        新建一个页面的规则时，先点右上角<Text strong>「下载业务规则模板」</Text>拿一份骨架
        （含 front matter 字段说明、R/F/S 编号表、9 列输出契约），按骨架填完再上传。
        模板文件在 <Text code>backend/app/metadata/modules/业务skill模板.md</Text>，可直接手工改。
      </Paragraph>
      <Alert
        type="info"
        showIcon
        message="保存后自动热重载知识库，无需重启后端"
        description="保存会重建 SKILL.md 的「规则文件索引」并调用 load_all_skills() 热重载；下次生成即刻生效。热重载窗口期内（毫秒级）会拒绝新的生成请求并返回 409，避免读到空技能库。"
      />
    </Card>
  );

  // ==================== 3. 规则补丁（需求变更改动）====================
  const renderRulePatchCard = () => (
    <Card
      title={
        <Space wrap>
          <span>规则补丁</span>
          <Tag color="gold" icon={<HistoryOutlined />}>需求变更改动</Tag>
          <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            应用后会在这份规则文件的 front matter 打上「需求变更改动」标注（时间 / 变更描述 / 改动编号）
          </Text>
        </Space>
      }
      style={{ marginBottom: 20, borderRadius: 8 }}
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message="本卡片产出的规则改动 = 需求变更驱动"
        description="与「人工上传 / 页面内编辑」不同来源。应用后会写入 last_change_origin=requirement_change、last_change_at、last_change_note、changed_rule_ids，规则列表里以金色「需求变更改动」标签展示，便于区分追责。"
      />

      <Space direction="vertical" style={{ width: '100%' }} size={8}>
        <div>
          <Text strong style={{ fontSize: 13 }}>变更描述</Text>
          <TextArea
            rows={3}
            style={{ marginTop: 4 }}
            value={rulePatchChangeDesc}
            onChange={e => setRulePatchChangeDesc(e.target.value)}
            placeholder="本次需求变更改了什么（例：卡BIN池新增「同BIN多商户」校验，需要同步补规则）"
          />
          {!rulePatchChangeDesc && changeDescription && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              留空将沿用「用例生成 → 增量迭代」域里已填的变更描述：{changeDescription.slice(0, 60)}…
            </Text>
          )}
        </div>
        <div>
          <Text strong style={{ fontSize: 13 }}>影响面分析（可选，建议带上）</Text>
          <TextArea
            rows={3}
            style={{ marginTop: 4 }}
            value={rulePatchImpact}
            onChange={e => setRulePatchImpact(e.target.value)}
            placeholder="留空将沿用「增量迭代」域的影响面分析结果；也可直接粘贴结论作为补丁依据"
          />
          {!rulePatchImpact && impactAnalysis && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              已自动沿用增量迭代域的影响面分析（{impactAnalysis.length} 字）
            </Text>
          )}
        </div>
      </Space>

      <Space wrap style={{ marginTop: 12 }}>
        <Button
          onClick={handleProposeRulePatch}
          loading={patchLoading}
          icon={<PlayCircleOutlined />}
        >
          生成规则补丁
        </Button>
        <Button
          type="primary"
          ghost
          icon={<SaveOutlined />}
          onClick={handleApplyRulePatch}
          loading={patchApplying}
          disabled={!rulePatches.length}
        >
          应用勾选补丁（备份 + 标注 + 热重载）
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          编号只增不改；废弃用「标记废弃」而非删除
        </Text>
      </Space>

      {(ruleFiles || []).length > 0 && (
        <div style={{ marginTop: 10 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>目标规则文件：</Text>
          {(ruleFiles || []).map((f, i) => (
            <span key={`${f.filename}-${i}`} style={{ marginRight: 12 }}>
              <Tag color="blue">{f.filename}</Tag>
              {f.rule_ids && f.rule_ids.length > 0 && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  现有编号：{f.rule_ids.join(', ')}
                </Text>
              )}
            </span>
          ))}
        </div>
      )}

      {patchSummary && (
        <div style={{ marginTop: 10, fontSize: 13 }}>
          <Text strong>补丁依据：</Text>
          <Text>{patchSummary}</Text>
        </div>
      )}

      {(rulePatches || []).map(p => (
        <div
          key={p._id}
          style={{
            marginTop: 10,
            padding: '10px 12px',
            background: '#fafafa',
            border: '1px solid #f0f0f0',
            borderRadius: 6
          }}
        >
          <Space align="start">
            <Checkbox
              checked={Boolean(patchChecked[p._id])}
              onChange={e => setPatchChecked(prev => ({ ...prev, [p._id]: e.target.checked }))}
            />
            <div>
              <Space wrap size={8} style={{ marginBottom: 4 }}>
                <Tag color={p.op === 'add' ? 'green' : (p.op === 'modify' ? 'blue' : 'red')}>
                  {p.op === 'add' ? '新增' : (p.op === 'modify' ? '修改' : '标记废弃')}
                </Tag>
                <Text strong>{p.rule_id || `新增(${p.table}表，编号自动续号)`}</Text>
                {p.field && <Text type="secondary">字段：{p.field}</Text>}
              </Space>
              {p.old ? (
                <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>原值：{p.old}</div>
              ) : null}
              <div style={{ fontSize: 13, marginBottom: 4 }}>
                建议值：{p.new || (p.cells || []).join(' | ') || '（见原因）'}
              </div>
              {p.reason && (
                <div style={{ fontSize: 12, color: '#8c8c8c' }}>依据：{p.reason}</div>
              )}
            </div>
          </Space>
        </div>
      ))}

      {patchResult && (
        <div style={{
          marginTop: 12,
          padding: '10px 12px',
          background: patchResult.code === 0 ? '#f6ffed' : '#fff2f0',
          border: `1px solid ${patchResult.code === 0 ? '#b7eb8f' : '#ffccc7'}`,
          borderRadius: 6
        }}>
          <Text strong>{patchResult.message || ''}</Text>
          {patchResult.backup_paths && (
            <div style={{ fontSize: 12, color: '#8c8c8c', marginTop: 4 }}>
              备份：{Object.values(patchResult.backup_paths).join('；')}
            </div>
          )}
          {(patchResult.files || []).map((f, i) => (
            <div key={i} style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {f.filename}｜应用 {f.applied ? f.applied.length : 0} 条
                {f.backup ? `｜备份 ${f.backup.split(/[\\/]/).pop()}` : ''}
                {f.stamp && f.stamp.changed_rule_ids ? `｜已标注改动编号 ${f.stamp.changed_rule_ids.join(', ')}` : ''}
              </Text>
            </div>
          ))}
        </div>
      )}
    </Card>
  );

  // ==================== 4. 全部规则（references 下）====================
  const renderAllRules = () => (
    <Card
      title="references 下全部规则"
      style={{ borderRadius: 8 }}
      extra={
        <Button size="small" icon={<ReloadOutlined />} loading={ruleFilesAllLoading}
          onClick={loadAllRuleFiles}>
          刷新
        </Button>
      }
    >
      <Paragraph type="secondary" style={{ fontSize: 12 }}>
        不按当前作用域过滤，用于看全貌、确认「规则到底放在哪、归哪个页面」。
        带金色「需求变更改动」标签的 = 由规则补丁（需求变更）改动过。
      </Paragraph>
      <Table
        size="small"
        rowKey="rel"
        loading={ruleFilesAllLoading}
        dataSource={ruleFilesAll}
        columns={[
          { title: '相对路径', dataIndex: 'rel', width: 300, ellipsis: true },
          { title: '模块', width: 110, render: (_, r) => (r.biz_module ? labelOfModule(r.biz_module) : <Text type="secondary">未声明</Text>) },
          { title: '页面', width: 110, render: (_, r) => (r.page ? labelOfPage(r.page) : '模块层') },
          { title: '作用域', width: 80, render: (_, r) => <Tag color={r.rule_scope === 'common' ? 'default' : 'blue'}>{r.rule_scope || 'module'}</Tag> },
          { title: '字数', width: 80, render: (_, r) => `${r.char_count || 0}` },
          { title: '来源标注', width: 150, render: (_, r) => renderOriginTag(r) },
          {
            title: '操作', width: 120, fixed: 'right',
            render: (_, r) => (
              <Space size={0}>
                <Button size="small" type="link" onClick={() => handleOpenRuleFile(r, 'view')}>查看</Button>
                <Button size="small" type="link"
                  onClick={() => { setEditCompensate(false); handleOpenRuleFile(r, 'edit'); }}>编辑</Button>
              </Space>
            ),
          },
        ]}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        scroll={{ x: 1000 }}
        locale={{ emptyText: 'references 下没有规则文件' }}
      />
    </Card>
  );

  return (
    <>
      {/* 顶部说明 */}
      <Card size="small" style={{ marginBottom: 20, borderRadius: 8 }}>
        <Space wrap>
          <FileTextOutlined style={{ color: '#1677ff' }} />
          <Text strong>规则管理</Text>
          <Tag color="blue">{scopeText}</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            规则 = <Text code>skills/req-test-cases/references/**/*.md</Text>；
            归属靠 front matter 的 biz_module + page 等值匹配；保存即自动热重载，<Text strong>无需重启后端</Text>。
          </Text>
        </Space>
      </Card>

      {renderScopedRules()}
      {renderUploadCard()}
      {renderRulePatchCard()}
      {renderAllRules()}

      {/* ==================== 查看 / 编辑 规则文件 ==================== */}
      <Modal
        title={ruleModal ? `${ruleModalMode === 'edit' ? '编辑' : '查看'}规则文件：${ruleModal.rel}` : ''}
        open={Boolean(ruleModal)}
        onCancel={closeRuleModal}
        width={1080}
        footer={
          ruleModal && (
            <Space wrap>
              {ruleModalMode === 'view' ? (
                <Button type="primary" icon={<EditOutlined />}
                  onClick={() => { setEditCompensate(false); setRuleModalMode('edit'); }}>
                  编辑
                </Button>
              ) : (
                <>
                  <Checkbox checked={editCompensate} onChange={e => setEditCompensate(e.target.checked)}>
                    以当前作用域（{scopeText}）补正 front matter
                  </Checkbox>
                  <Button icon={<ReloadOutlined />} onClick={() => {
                    setRuleModalContent(ruleModal.content || '');
                    setRuleModalMode('view');
                  }}>
                    放弃修改
                  </Button>
                  <Button type="primary" icon={<SaveOutlined />} loading={ruleSaving}
                    onClick={() => handleSaveRuleContent(editCompensate ? 'override' : 'keep')}>
                    保存（自动热重载）
                  </Button>
                </>
              )}
            </Space>
          )
        }
      >
        {ruleModal && (
          <>
            <Descriptions size="small" column={3} style={{ marginBottom: 12 }}>
              <Descriptions.Item label="模块">
                {ruleModal.biz_module ? labelOfModule(ruleModal.biz_module) : <Text type="warning">未声明</Text>}
              </Descriptions.Item>
              <Descriptions.Item label="页面">
                {ruleModal.page ? labelOfPage(ruleModal.page) : '模块层'}
              </Descriptions.Item>
              <Descriptions.Item label="作用域">{ruleModal.rule_scope || 'module'}</Descriptions.Item>
              <Descriptions.Item label="编号" span={2}>
                {(ruleModal.rule_ids || []).length ? ruleModal.rule_ids.join(', ') : '（无编号表）'}
              </Descriptions.Item>
              <Descriptions.Item label="字数">{ruleModal.char_count}</Descriptions.Item>
            </Descriptions>
            {(!ruleModal.biz_module) && (
              <Alert
                type="warning" showIcon style={{ marginBottom: 12 }}
                message="这份文件没有 biz_module"
                description="生成时它不会被任何页面注入，规则管理页也不会把它算进任何页面。建议勾选「以当前作用域补正 front matter」后保存。"
              />
            )}
            {ruleModalMode === 'edit' ? (
              <>
                <Alert
                  type="info" showIcon style={{ marginBottom: 8 }}
                  message="直接编辑 content 即可；保存会覆盖该文件，旧内容自动备份到 references/_versions/"
                  description="默认「原样保留」front matter（不擅自改写）。若 front matter 与当前作用域不一致，可勾选下方「以当前作用域补正」。"
                />
                <TextArea
                  value={ruleModalContent}
                  onChange={e => setRuleModalContent(e.target.value)}
                  autoSize={{ minRows: 18, maxRows: 30 }}
                  style={{ fontFamily: 'Consolas, Monaco, monospace', fontSize: 12 }}
                />
              </>
            ) : (
              <div style={{ padding: 12, background: '#fafafa', borderRadius: 6, maxHeight: 620, overflow: 'auto' }}>
                <MarkdownView>{ruleModal.content || ''}</MarkdownView>
              </div>
            )}
          </>
        )}
      </Modal>

      {/* ==================== 上传 / 替换 规则文件 ==================== */}
      <Modal
        title={`上传业务规则${selectedModule ? '（' + scopeText + '）' : ''}`}
        open={uploadOpen}
        onCancel={() => { setUploadOpen(false); clearRulePrecheck(); }}
        width={900}
        footer={
          <Space>
            <Button onClick={() => { setUploadOpen(false); clearRulePrecheck(); }}>取消</Button>
            {!rulePrecheck ? (
              <Button type="primary" onClick={doPrecheck}>预检落盘位置</Button>
            ) : (
              <Button type="primary" icon={<UploadOutlined />} loading={ruleSaving} onClick={doUpload}>
                {rulePrecheck.action === 'replace' ? '确认替换该规则文件' : '确认新建规则文件'}
              </Button>
            )}
          </Space>
        }
      >
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          <div>
            <Text strong style={{ fontSize: 13 }}>1) 选择规则文件（.md）或直接粘贴内容</Text>
            <div style={{ marginTop: 6 }}>
              <Upload beforeUpload={handlePickFile} showUploadList={false} accept=".md,.markdown,.txt">
                <Button icon={<UploadOutlined />}>选择本地 .md 文件</Button>
              </Upload>
              {uploadFilename && <Text type="secondary" style={{ marginLeft: 12 }}>已选：{uploadFilename}</Text>}
            </div>
          </div>
          <div>
            <Text strong style={{ fontSize: 13 }}>规则内容</Text>
            <TextArea
              rows={12}
              style={{ marginTop: 4, fontFamily: 'Consolas, Monaco, monospace', fontSize: 12 }}
              value={uploadContent}
              onChange={e => { setUploadContent(e.target.value); clearRulePrecheck(); }}
              placeholder={'---\nbiz_module: bin\npage: pool\ndescription: 卡BIN池页面规则\n---\n\n在这里粘贴/编辑规则内容（front matter 可留空，系统会按所选作用域补正）'}
            />
          </div>
          <div>
            <Text strong style={{ fontSize: 13 }}>规则用途描述（可选，写入 front matter 的 description）</Text>
            <Input
              style={{ marginTop: 4 }}
              value={uploadDescription}
              onChange={e => { setUploadDescription(e.target.value); clearRulePrecheck(); }}
              placeholder="例：卡BIN池管理页面的字段约束与业务规则"
            />
          </div>

          {!rulePrecheck ? (
            <Alert type="info" showIcon
              message="先点「预检落盘位置」"
              description="预检会告诉你：这次是覆盖同页面已有规则，还是新建到哪个目录，以及 front matter 有没有冲突。冲突会在这里让你二选一，不会静默改写。" />
          ) : (
            <>
              <Alert
                type={rulePrecheck.action === 'replace' ? 'warning' : 'success'}
                showIcon
                message={rulePrecheck.action === 'replace'
                  ? `⚠️ 该页面已有规则文件，保存会【覆盖】它`
                  : `该页面暂无规则文件，将【新建】`}
                description={
                  <div style={{ fontSize: 12 }}>
                    <div>目标：<Text code>{rulePrecheck.target_rel}</Text></div>
                    <div>{rulePrecheck.note}</div>
                    {rulePrecheck.new_dir && <div style={{ color: '#d46b08' }}>注意：模块目录不存在，将新建 references/{rulePrecheck.target_dir}/</div>}
                    {(rulePrecheck.existing || []).length > 0 && (
                      <div>将覆盖：{rulePrecheck.existing.map(x => x.filename).join('、')}</div>
                    )}
                  </div>
                }
              />
              {(rulePrecheck.conflicts || []).length > 0 && (
                <Alert
                  type="error" showIcon
                  message="front matter 与你选择的页面不一致，请二选一（不会静默改写）"
                  description={
                    <div style={{ fontSize: 12 }}>
                      {(rulePrecheck.conflicts || []).map((c, i) => (
                        <div key={i}>
                          <Text code>{c.field}</Text>：文件里写的是 <Text strong>{c.file}</Text>，
                          当前所选是 <Text strong>{c.selected || '（空）'}</Text>
                        </div>
                      ))}
                    </div>
                  }
                />
              )}
              <div>
                <Text strong style={{ fontSize: 13 }}>front matter 处理方式</Text>
                <Radio.Group
                  style={{ display: 'block', marginTop: 6 }}
                  value={uploadMode}
                  onChange={e => setUploadMode(e.target.value)}
                >
                  <Radio value="override">
                    以页面选择为准自动补正<Text type="secondary" style={{ fontSize: 12 }}>（推荐：写入 biz_module={selectedModule} / page={selectedPage || '（模块层）'}）</Text>
                  </Radio>
                  <Radio value="keep">
                    原样保留文件里写的
                    <Text type="warning" style={{ fontSize: 12 }}>
                      （若与所选作用域不一致，这份规则不会被当前页面注入，且下次上传会再新建一份）
                    </Text>
                  </Radio>
                </Radio.Group>
              </div>
            </>
          )}
        </Space>
      </Modal>
    </>
  );
}
