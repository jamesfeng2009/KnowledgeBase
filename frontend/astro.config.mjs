import { defineConfig } from 'astro/config';
import react from '@astrojs/react';

// https://astro.build/config
export default defineConfig({
  // 启用 React 集成，支持 React Island 组件
  integrations: [react()],

  // 开发服务器配置
  server: {
    port: 3000,
    host: true,
  },

  // Vite 配置，支持 @/* 路径别名
  vite: {
    resolve: {
      alias: {
        '@': new URL('./src', import.meta.url).pathname,
      },
    },
  },
  // 注意：Astro 5 默认将 PUBLIC_ 前缀的环境变量暴露给客户端，无需手动配置 prefix
});
