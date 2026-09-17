# Release 流程模板

> 供维护者按此模板执行一次版本发布（配合 GitHub Actions release workflow）。

## 1. 版本号

- 语义化版本 `MAJOR.MINOR.PATCH`；破坏性变更升 MAJOR，特性升 MINOR，
  修复升 PATCH。
- 预发布：`1.1.0-rc.1`（release workflow 会自动打 `-rc` 标签）。

## 2. Changelog

1. 把 `CHANGELOG.md` 中 `[Unreleased]` 已完成的条目整理到新版本小节
   （按 新增/修复/变更/移除 分组）；
2. 确认每个条目都有对应 PR 链接或可追溯提交。

## 3. 质量门（CI 强制）

- [ ] `pytest tests/ -q`（backend）全绿（环境类失败需在 PR 说明）
- [ ] 前端 `npm run build`（Astro）通过
- [ ] 文档站 `npm run build`（VitePress）通过
- [ ] `helm lint infra/helm/enterprise-knowledge` 通过
- [ ] Ruff / ESLint / TypeScript 检查通过

## 4. 打标签与发布

```bash
git tag -a v1.1.0 -m "release v1.1.0"
git push origin v1.1.0
```

触发 `.github/workflows/release.yml`：
- 构建并推送镜像 `ghcr.io/<owner>/ekb-core:v1.1.0`；
- 生成 Release Notes（取 CHANGELOG 对应小节）；
- 附件：`ekb_cli.py`、`chrome-extension.zip`、Helm chart 包。

## 5. 发布后

- [ ] 在文档站「Releases」页更新最新版本号
- [ ] 通知企业微信发布群（`WECOM_BOT_WEBHOOK`）
