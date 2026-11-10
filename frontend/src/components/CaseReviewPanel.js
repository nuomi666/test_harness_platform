import { Card, message, Tag, Checkbox, Alert, Table, Typography } from 'antd';
const { Text } = Typography;
import React from 'react';

// 批2a：从 App.js 机械拆分而出（纯搬运，逻辑未改）。
// 下方解构列表由脚本按「本文件实际用到的 App.js 作用域标识符」自动推导；
// 状态与业务逻辑仍集中在 App.js（控制器），本文件只负责展示。
// ⚠️ 新增 state/函数后，需同步补进 App.js 的 C 对象。
export default function CaseReviewPanel({ C }) {
  const {
    caseReviewChecked,
    caseReviewResult,
    setCaseReviewChecked,
  } = C;


  // ========== 用例评审结果面板（分数表 + 删除勾选，沉淀时联动剔除）==========
  const renderCaseReviewPanel = () => {
    if (!caseReviewResult) return null;
    const s = caseReviewResult.summary || {};
    const cases = caseReviewResult.cases || [];
    const toggleCheck = (name, checked) => {
      setCaseReviewChecked(prev =>
        checked ? [...new Set([...prev, name])] : prev.filter(n => n !== name));
    };
    const scoreColor = (t) => (t >= 85 ? '#52c41a' : t >= 60 ? '#fa8c16' : '#f5222d');
    const decisionTag = (d) => {
      if (d === 'keep') return <Tag color="success">保留</Tag>;
      if (d === 'improve') return <Tag color="warning">待改进</Tag>;
      return <Tag color="error">建议删除</Tag>;
    };
    const columns = [
      {
        title: '删除',
        width: 56,
        render: (_, r) => (
          <Checkbox
            checked={caseReviewChecked.includes(r.name)}
            onChange={e => toggleCheck(r.name, e.target.checked)}
          />
        ),
      },
      { title: '用例名称', dataIndex: 'name', width: 240, ellipsis: true },
      {
        title: '总分', dataIndex: 'total', width: 80,
        sorter: (a, b) => a.total - b.total,
        render: (t, r) => (
          <Text strong style={{ color: scoreColor(t) }}>
            {t}{r.score_drift ? `（上次${r.prev_total}⚠️）` : ''}
          </Text>
        ),
      },
      { title: 'B1步骤', width: 70, render: (_, r) => (r.dims || {}).B1 ?? '-' },
      { title: 'B2预期', width: 70, render: (_, r) => (r.dims || {}).B2 ?? '-' },
      { title: 'B3业务', width: 70, render: (_, r) => (r.dims || {}).B3 ?? '-' },
      { title: 'B4覆盖', width: 70, render: (_, r) => (r.dims || {}).B4 ?? '-' },
      { title: 'A扣分', dataIndex: 'a_deduction', width: 70 },
      {
        title: '可自动化性', dataIndex: 'automatability', width: 130,
        render: (a) => {
          const color = a === '可直接自动化' ? 'green'
            : a === '弱断言可自动化' ? 'cyan'
            : a === '需改造后自动化' ? 'orange' : 'default';
          return <Tag color={color}>{a || '-'}</Tag>;
        },
      },
      { title: '建议', dataIndex: 'decision', width: 90, render: decisionTag },
      { title: '理由', dataIndex: 'reason', ellipsis: true },
    ];
    return (
      <Card
        size="small"
        title={`用例评审结果${caseReviewResult.engine_b ? '' : '（仅代码硬指标）'}`}
        style={{ marginBottom: 16, borderRadius: 8 }}
        extra={
          <Text type="secondary">
            报告文件：report/{caseReviewResult.report_file || '-'}
          </Text>
        }
      >
        <Alert
          style={{ marginBottom: 12 }}
          type={s.suggest_delete > 0 ? 'warning' : 'success'}
          showIcon
          message={
            `共 ${s.total_cases || 0} 条，均分 ${s.avg || 0}：` +
            `✅保留 ${s.keep || 0}　⚠️待改进 ${s.improve || 0}　❌建议删除 ${s.suggest_delete || 0}` +
            `　硬伤 ${s.hard_issue_count || 0} 处（高危 ${s.high_issue_cases || 0} 条）` +
            (s.drift_cases ? `　分数漂移 ${s.drift_cases} 条` : '')
          }
          description={
            (caseReviewResult.failed_segments || []).length > 0
              ? `引擎B第 ${caseReviewResult.failed_segments.join('、')} 片评审失败，这些用例只有硬指标分，建议重新评审。`
              : (caseReviewChecked.length > 0
                ? `已勾选 ${caseReviewChecked.length} 条待删除用例：沉淀时将自动剔除（产物文件同步覆盖，旧版本可追溯）。`
                : '无勾选删除项；勾选「删除」列后，沉淀时将剔除对应用例。')
          }
        />
        <Table
          size="small"
          rowKey={(r) => r.norm || r.name}
          columns={columns}
          dataSource={cases}
          pagination={{ pageSize: 20, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100] }}
          expandable={{
            rowExpandable: (r) => (r.hard_issues || []).length > 0,
            expandedRowRender: (r) => (
              <ul style={{ margin: 0, paddingLeft: 20 }}>
                {(r.hard_issues || []).map((issue, i) => (
                  <li key={i} style={{ color: issue.severity === 'high' ? '#f5222d' : '#fa8c16' }}>
                    [{issue.metric}/{issue.severity}] {issue.detail}
                    {issue.excerpt ? <Text type="secondary">（原文：{issue.excerpt}）</Text> : null}
                  </li>
                ))}
              </ul>
            ),
          }}
        />
      </Card>
    );
  };

  return renderCaseReviewPanel();
}
