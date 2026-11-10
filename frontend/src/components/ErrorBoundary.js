import React from 'react';
import { Result, Button, Typography } from 'antd';
const { Paragraph, Text } = Typography;

// 单个功能域的渲染错误不拖垮整个应用。
//
// 为什么必须要有（2026-09-29 实际踩到）：
//   React 18 里组件渲染抛错且没有 ErrorBoundary 时，会**卸载整棵根树** ⇒ 页面白屏、
//   侧边导航可能还在、真正的原因只出现在控制台里；表现是「这个页面没加载出来」，
//   而不是「这个页面报错了」，定位成本极高（历史上「用例生成域看起来空了一块」就是这么来的）。
//   套上 ErrorBoundary 后：只有出错的那个域变成错误卡片，其余域照常可用，且错误栈直接显示在页面上。
//
// 复位策略：四个域是「同一时刻只挂一个」，切域时本组件随子树一起卸载 ⇒ 状态天然复位；
// 额外支持 resetKey（传作用域），让「某个模块/页面的数据导致崩溃」在切走作用域后能自动恢复。
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // 关键位置必须有日志：出错域 + 组件栈，便于直接定位到是哪个域/哪一行
    console.error(
      `❌ 功能域渲染失败 | domain=${this.props.name || '(未命名)'}`,
      error,
      info && info.componentStack
    );
  }

  componentDidUpdate(prevProps) {
    const changed =
      prevProps.name !== this.props.name || prevProps.resetKey !== this.props.resetKey;
    if (this.state.error && changed) {
      this.setState({ error: null });
    }
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children || null;

    const stack = String((error && (error.stack || error.message)) || error || '未知错误');
    return (
      <Result
        status="error"
        title={`「${this.props.name || '当前'}」功能域渲染出错`}
        subTitle="其它功能域不受影响，可先切到别的页签继续用；下面是原始错误信息，便于定位。"
        extra={[
          <Button key="retry" type="primary" onClick={() => this.setState({ error: null })}>
            重试渲染
          </Button>,
          <Button key="reload" onClick={() => window.location.reload()}>
            刷新页面
          </Button>,
        ]}
      >
        <Paragraph>
          <Text code style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{stack}</Text>
        </Paragraph>
      </Result>
    );
  }
}
