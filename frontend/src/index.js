import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';

// 找到 public/index.html 里的 <div id="root"></div>，把 React 组件挂载进去
const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);