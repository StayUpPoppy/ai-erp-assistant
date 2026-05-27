import type { Config } from "tailwindcss";

/**
 * Tailwind 配置：扫描 app 目录下所有组件类名，生成最终样式表。
 * 后续若新增 components/ 等目录，记得把路径加入 content。
 */
const config: Config = {
  content: ["./app/**/*.{js,ts,jsx,tsx,mdx}", "./components/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {},
  },
  plugins: [],
};

export default config;
