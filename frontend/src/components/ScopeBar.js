import React from 'react';
import { Select, Tag, Typography, Space, Layout } from 'antd';
import { MODULE_LIST, getPagesForModule, scopeLabel } from '../config/scopes';

const { Text } = Typography;
const { Option } = Select;
const { Header } = Layout;

// 顶部全局作用域条（模块 + 页面）—— 全平台唯一的作用域入口，四个功能域共用。
export default function ScopeBar({ selectedModule, selectedPage, handleModuleChange, handleScopePageChange }) {
  return (
<Header style={{
  background: '#fff',
  borderBottom: '1px solid #f0f0f0',
  padding: '12px 24px',
  height: 'auto',
  lineHeight: 'normal'
}}>
  <Space wrap size={12}>
    <Text strong>当前作用域</Text>
    <Select
      placeholder="业务模块（必选）"
      style={{ width: 190 }}
      value={selectedModule || undefined}
      onChange={handleModuleChange}
    >
      {MODULE_LIST.map(m => <Option key={m.value} value={m.value}>{m.label}</Option>)}
    </Select>
    <Select
      placeholder="页面（建议选择，不选=模块层）"
      style={{ width: 240 }}
      value={selectedPage || undefined}
      onChange={handleScopePageChange}
      allowClear
      disabled={!selectedModule}
    >
      {getPagesForModule(selectedModule).map(p => (
        <Option key={p.value} value={p.value}>{p.label}</Option>
      ))}
    </Select>
    <Tag color={selectedModule ? 'blue' : 'default'}>
      {selectedModule ? scopeLabel(selectedModule, selectedPage) : '未选模块（会放弃页面级精确规则注入）'}
    </Tag>
  </Space>
</Header>
  );
}
