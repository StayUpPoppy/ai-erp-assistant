import type { Metadata } from "next";
import "./globals.css";

/**
 * 根布局：全站 HTML 外壳与元数据。
 * 业务页面在 app/page.tsx，此处仅做字体与全局样式挂载。
 */
export const metadata: Metadata = {
  title: "ERP AI 助手",
  description: "独立助手：聊天、文档上传、任务状态、字段补全、ERP 草稿",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" className="scroll-smooth">
      <body>{children}</body>
    </html>
  );
}
