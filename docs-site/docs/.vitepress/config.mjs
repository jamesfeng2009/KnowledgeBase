import { defineConfig } from "vitepress";

export default defineConfig({
  lang: "zh-CN",
  title: "企业知识库 Agent 平台",
  description: "对标 WeKnora 的企业级 RAG + Agent 知识库平台",
  themeConfig: {
    nav: [
      { text: "指南", link: "/guide/getting-started" },
      { text: "P0-P2 路线", link: "/guide/p0-p2-roadmap" },
      { text: "GitHub", link: "https://github.com/jamesfeng2009/KnowledgeBase" },
    ],
    sidebar: [
      {
        text: "指南",
        items: [
          { text: "快速开始", link: "/guide/getting-started" },
          { text: "架构总览", link: "/guide/architecture" },
          { text: "部署（Docker / K8s）", link: "/guide/deployment" },
          { text: "P0-P2 提升路线", link: "/guide/p0-p2-roadmap" },
          { text: "安全与多租户", link: "/guide/security" },
        ],
      },
    ],
    outline: { level: [2, 3] },
    footer: {
      message: "企业知识库 Agent 平台 · 文档站",
      copyright: "Copyright © 2026",
    },
  },
});
