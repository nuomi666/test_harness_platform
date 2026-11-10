import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';

// 统一 Markdown 渲染：这套 components / remark / rehype 配置原先在 App.js 里被 8 处重复书写，
// 收敛到这里，改渲染风格只改一处。
export const markdownComponents = {
    table: ({children}) => <table style={{borderCollapse: 'collapse', width: '100%', margin: '12px 0'}}>{children}</table>,
    th: ({children}) => <th style={{border: '1px solid #e8e8e8', padding: '10px 12px', background: '#fafafa', fontWeight: 600, color: '#333'}}>{children}</th>,
    td: ({children}) => <td style={{border: '1px solid #e8e8e8', padding: '10px 12px', color: '#555'}}>{children}</td>,
    h1: ({children}) => <h1 style={{fontSize: '20px', margin: '16px 0 12px', color: '#1f1f1f'}}>{children}</h1>,
    h2: ({children}) => <h2 style={{fontSize: '18px', margin: '14px 0 10px', color: '#1f1f1f'}}>{children}</h2>,
    h3: ({children}) => <h3 style={{fontSize: '16px', margin: '12px 0 8px', color: '#262626'}}>{children}</h3>,
    p: ({children}) => <p style={{margin: '8px 0', lineHeight: 1.6}}>{children}</p>,
};

export default function MarkdownView({ children }) {
  return (
    <ReactMarkdown components={markdownComponents} remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>
      {children}
    </ReactMarkdown>
  );
}
