import { Button, Card, message, Modal, Table, Empty, Typography, Space } from 'antd';
const { Text } = Typography;
import { ReloadOutlined, DeleteOutlined } from '@ant-design/icons';
import { getAssetFile } from '../api';
import { labelOfPage, scopeLabel } from '../config/scopes';
import React from 'react';
import MarkdownView from '../components/MarkdownView';

// 批2a：从 App.js 机械拆分而出（纯搬运，逻辑未改）。
// 下方解构列表由脚本按「本文件实际用到的 App.js 作用域标识符」自动推导；
// 状态与业务逻辑仍集中在 App.js（控制器），本文件只负责展示。
// ⚠️ 新增 state/函数后，需同步补进 App.js 的 C 对象。
export default function ReportsPage({ C }) {
  const {
    handleDeleteAsset,
    historyAssetModule,
    historyAssetPage,
    loadReportFiles,
    loading,
    reportFiles,
    reportFilesLoading,
    reportPreview,
    selectedModule,
    selectedPage,
    setReportPreview,
    slug,
  } = C;


  const openReportPreview = async (targetSlug, assetType) => {
    try {
      const res = await getAssetFile(targetSlug, assetType);
      const data = res.data || {};
      if (data.code === 0 && data.data) {
        setReportPreview({ slug: targetSlug, asset_type: assetType, content: data.data });
      } else {
        message.error(data.message || '读取报告失败');
      }
    } catch (e) {
      message.error('读取报告失败');
    }
  };

  const renderReportDomain = () => (
    <Card
      title={`报告中心${selectedModule ? '（' + scopeLabel(selectedModule, selectedPage) + '）' : ''}`}
      style={{ borderRadius: 8 }}
      extra={
        <Button size="small" icon={<ReloadOutlined />}
          onClick={() => loadReportFiles(historyAssetModule, historyAssetPage)}
          disabled={!selectedModule}>
          刷新
        </Button>
      }
    >
      <Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 12 }}>
        需求评审报告与用例评审报告统一落在后端 report/ 目录（与用例产物 outputs/ 分开），此处查看与删除。
        删除只删本地报告文件，不影响已沉淀的用例基线与向量库。
      </Text>
      {!selectedModule ? (
        <Empty description="请先在顶部选择业务模块" />
      ) : (
        <Table
          size="small"
          rowKey="filename"
          loading={reportFilesLoading}
          dataSource={reportFiles}
          pagination={false}
          scroll={{ x: 1180 }}
          locale={{ emptyText: `「${scopeLabel(selectedModule, selectedPage)}」下暂无报告` }}
          columns={[
            { title: '报告文件', dataIndex: 'filename', width: 380, ellipsis: true },
            {
              title: '类型', width: 130,
              render: (_, r) => (r.asset_type === 'case_review_report' ? '用例评审报告' : '需求评审报告'),
            },
            { title: '页面', width: 130, render: (_, r) => (r.page ? labelOfPage(r.page) : '未知') },
            { title: '版本', dataIndex: 'version', width: 100, render: (v) => v || '-' },
            { title: '生成时间', dataIndex: 'mtime', width: 160 },
            { title: '大小', width: 90, render: (_, r) => `${(r.size / 1024).toFixed(1)}KB` },
            {
              title: '操作', width: 150, fixed: 'right',
              render: (_, r) => (
                <Space size={0}>
                  <Button size="small" type="link" onClick={() => openReportPreview(r.slug, r.asset_type)}>
                    查看
                  </Button>
                  <Button size="small" type="link" danger icon={<DeleteOutlined />}
                    onClick={() => handleDeleteAsset(r, () => loadReportFiles(historyAssetModule, historyAssetPage))}>
                    删除
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      )}
    </Card>
  );

  return (
    <>
    
      {/* 报告预览（只读，不落盘） */}
      <Modal
        title={reportPreview ? `报告预览：${reportPreview.slug}` : ''}
        open={Boolean(reportPreview)}
        onCancel={() => setReportPreview(null)}
        footer={null}
        width={1000}
      >
        {reportPreview && (
          <div style={{ padding: 12, background: '#fafafa', borderRadius: 6, maxHeight: 640, overflow: 'auto' }}>
            <MarkdownView>{reportPreview.content}</MarkdownView>
          </div>
        )}
      </Modal>
    {renderReportDomain()}
    </>
  );
}
