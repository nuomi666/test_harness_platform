// 渲染冒烟（两道）：
//   ① App 在四个功能域下各渲染一遍 —— 不抛异常、不是空白页
//   ② 每个域组件「单独」渲染，并传入**已选作用域**的上下文 —— 断言作用域真的被读到
//
// 为什么要有第 ② 道（2026-09-30 实际踩到）：
//   `export default function X(C)` 写错签名时，C 拿到的是 props 对象 `{C: {...}}`，
//   域内 `const { selectedModule } = C` 全是 undefined ⇒ 全部退化成空态
//   （「请先在顶部选择业务模块」），而**顶栏 ScopeBar 是直接解构 props、显示正常**。
//   第 ① 道完全抓不到：默认就没选模块，渲染出的空态和「正确代码的默认态」一模一样。
//   所以必须显式喂一个「已选作用域」的上下文，才能区分「读到了」和「读到 undefined」。
//
// 跑法：cd frontend && CI=true npx react-scripts test --watchAll=false
//
// 注意：**不能**用 jest.resetModules() —— 那会连带把 react / react-dom 重置成另一份副本，
// 导致 "Cannot read properties of null (reading 'useState')"（hooks dispatcher 对不上）。
// 每次 renderToStaticMarkup 都是**全新的组件实例**，activeNav 的 useState 初始化函数
// 会重新读 window.location.hash，所以直接改 hash 再渲染即可。
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import App from './App';
import GeneratePage from './pages/GeneratePage';
import AssetsPage from './pages/AssetsPage';
import ReportsPage from './pages/ReportsPage';
import RulesPage from './pages/RulesPage';

// react-markdown 是纯 ESM 包，jest 默认不转译 node_modules ⇒ 直接 mock 掉（本冒烟只关心渲染不抛错）
jest.mock('react-markdown', () => {
  const React = require('react');
  return { __esModule: true, default: (props) => React.createElement('div', null, props.children) };
});
jest.mock('remark-gfm', () => ({ __esModule: true, default: () => {} }));
jest.mock('rehype-raw', () => ({ __esModule: true, default: () => {} }));

// antd 在 jsdom 下会用到 matchMedia（CRA 的 jsdom 没实现）
beforeAll(() => {
  window.matchMedia = window.matchMedia || function () {
    return {
      matches: false, media: '', onchange: null,
      addListener() {}, removeListener() {},
      addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
    };
  };
});

test('四个功能域都能渲染出内容（不抛异常、不全空白）', () => {
  const lens = {};
  ['#generate', '#assets', '#reports', '#rules'].forEach((hash) => {
    window.location.hash = hash;
    let html = '';
    expect(() => { html = renderToStaticMarkup(React.createElement(App)); }).not.toThrow();
    lens[hash] = html.length;
    // 侧边导航 + 该域内容都要有输出（全空白 = 只剩导航骨架）
    expect(html.length).toBeGreaterThan(2000);
  });
  // eslint-disable-next-line no-console
  console.log('各域渲染 HTML 长度:', lens);
});

// 建一个「已选作用域」的上下文：未提供的键按名字启发式给安全默认值，
// 目的是让域组件能一路渲染到底（而不是被 undefined 短路）。
const mkCtx = (over = {}) => {
  const base = {
    selectedModule: 'bin',
    selectedPage: 'pool',
    currentMode: 'quick',
    activeNav: 'generate',
    ...over,
  };
  return new Proxy(base, {
    get(t, k) {
      if (k in t) return t[k];
      if (typeof k !== 'string') return undefined;
      if (/^(set|handle|load|on[A-Z]|close|clear|start|stop|open|fetch|toggle|render)/.test(k)) return () => {};
      if (/(Checked|Files|Cases|Patches|Questions|Options|Scores|States|Rows|List)$/.test(k)) return [];
      if (/(Loading|Saving|Applying|Precipitating|Used|Empty)$/.test(k)) return false;
      return '';
    },
  });
};

// 作用域标签（scopes.js 的 scopeLabel）在「已选模块」时会渲染成「卡BIN模块 / 卡BIN池管理」
const SCOPE_LABEL = '卡BIN池管理';
// 各域在「没读到作用域」时才会出现的空态文案（拿到 props 对象时的退化物）
const EMPTY_HINTS = ['请先在顶部选择业务模块', '未选择模块'];

test('域组件真的读到了上下文（而不是拿到 props 对象导致全 undefined）', () => {
  const cases = [
    // [名字, 组件, 该域特有的「确实读到了上下文」正向证据]
    // GeneratePage 不渲染作用域标签，改用 currentMode 生效的证据：
    // 读不到 currentMode 时会渲染「请选择上方的生成模式开始」空态。
    ['GeneratePage', GeneratePage, '需求信息'],
    ['AssetsPage', AssetsPage, SCOPE_LABEL],
    ['ReportsPage', ReportsPage, SCOPE_LABEL],
    ['RulesPage', RulesPage, SCOPE_LABEL],
  ];
  const lens = {};
  cases.forEach(([name, Comp, evidence]) => {
    let html = '';
    expect(() => {
      html = renderToStaticMarkup(React.createElement(Comp, { C: mkCtx() }));
    }).not.toThrow();
    lens[name] = html.length;
    // 正向：读到了作用域 / 模式，才会渲染出这些内容
    expect(html).toContain(evidence);
    // 反向：任何「未选模块」空态都不该出现（签名写错时必然出现）
    EMPTY_HINTS.forEach((hint) => expect(html).not.toContain(hint));
    expect(html).not.toContain('请选择上方的生成模式开始');
  });
  // eslint-disable-next-line no-console
  console.log('各域组件（已选作用域）渲染 HTML 长度:', lens);
});
