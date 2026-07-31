# CI/CD 流水线说明

> 本文档描述 EnterpriseKB 的持续集成与持续交付流水线，涵盖阶段定义、配置文件、触发条件、审批与回滚。

## 1. 流水线总览

```
[提交PR] -> [CI校验] -> [合并main] -> [构建镜像] -> [部署staging] -> [审批] -> [部署prod]
                                       |                                  |
                                   [安全扫描]                          [冒烟测试]
```

工具选型：GitHub Actions（CI）+ ArgoCD（CD，GitOps 模式）。

## 2. CI 阶段定义

### 2.1 阶段与职责

| 阶段 | 内容 | 失败处理 |
| --- | --- | --- |
| checkout | 拉取代码 | 重试 |
| lint | ruff + mypy + 前端 lint | 阻断 |
| unit-test | 单元测试 + 覆盖率 | 阻断 |
| integration-test | 集成测试（testcontainers） | 阻断 |
| security-scan | 依赖扫描 + SAST + 镜像扫描 | 阻断（高危） |
| build | 构建镜像并推送 | 阻断 |
| e2e | 端到端测试（仅 main） | 阻断 |

### 2.2 GitHub Actions 配置

```yaml
name: ci
on:
  pull_request:
    branches: [develop, main]
  push:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.11"}
      - run: pip install ruff mypy black
      - run: ruff check backend/
      - run: mypy backend/
      - run: black --check backend/

  unit-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r requirements-dev.txt
      - run: pytest tests/unit -v --cov=backend --cov-fail-under=80

  build:
    needs: [lint, unit-test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ${{ secrets.REGISTRY }}
          username: ${{ secrets.REGISTRY_USER }}
          password: ${{ secrets.REGISTRY_PASS }}
      - run: docker build -t ekb/api:${{ github.sha }} ./backend
      - run: docker push ekb/api:${{ github.sha }}
```

## 3. 触发条件

| 事件 | 触发流水线 | 部署目标 |
| --- | --- | --- |
| PR 提交/更新 | CI 校验 | 不部署 |
| 合并到 develop | CI + 构建 | staging |
| 合并到 main | CI + 构建 + E2E | staging + 审批后 prod |
| 打 Tag (v*.*.*) | 构建正式镜像 | prod（审批） |
| 定时（每日凌晨） | 安全扫描 | - |

## 4. 构建与镜像管理

- 镜像标签：`commit SHA`（CI）与 `语义版本`（正式发布）
- 多架构构建（amd64/arm64）
- 基础镜像定期更新并重建以修复漏洞
- 镜像签名（cosign）防篡改

## 5. CD 部署（GitOps）

### 5.1 ArgoCD 模型

部署清单存放于独立 `ekb-deploy` 仓库，ArgoCD 监听该仓库变更并同步到集群：

```
[main合并] -> [CI更新镜像tag] -> [提交deploy清单] -> [ArgoCD同步] -> [滚动发布]
```

### 5.2 滚动发布策略

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxSurge: 1
    maxUnavailable: 0
```

- 先起一个新 Pod，健康检查通过后逐步替换旧 Pod
- 全程不中断服务

## 6. 审批流程

### 6.1 生产部署审批

1. CI 全绿
2. 自动部署到 staging 并通过冒烟测试
3. 生成「发布申请」工单，附变更说明与回滚方案
4. 至少 2 名 Release Manager 审批
5. 审批通过后手动触发 prod 部署

### 6.2 审批要素

- 变更范围与影响评估
- 是否含破坏性变更
- 回滚方案是否就绪
- 监控告警是否覆盖

## 7. 回滚机制

### 7.1 应用层回滚

ArgoCD 一键回滚到历史版本：

```bash
argocd app rollback ekb-api <revision-id>
```

### 7.2 数据库回滚

- 每次部署前自动执行数据库备份
- 迁移支持 downgrade，但破坏性迁移需谨慎
- 数据回滚优先使用备份恢复

### 7.3 回滚时效要求

- 应用回滚：< 2 分钟
- 数据库回滚：< 15 分钟

## 8. 环境管理

| 环境 | 用途 | 数据 | 访问 |
| --- | --- | --- | --- |
| dev | 开发自测 | 模拟数据 | 开发者 |
| staging | 预发布验证 | 脱敏数据 | QA + 运维 |
| prod | 生产 | 真实数据 | 受限 |

环境配置通过 values 文件区分，密钥来自各环境 Vault。

## 9. 制品库与版本

- 镜像存放于私有 Harbor
- Helm Chart 版本化发布
- 每次正式发布生成 Release Notes（自动从 CHANGELOG 抽取）

## 10. 流水线监控

- 流水线成功率与耗时看板
- 失败自动通知到飞书群
- 每月 review 耗时最长的阶段并优化

## 11. 安全实践

- Secrets 全部使用 GitHub Actions Secrets / Vault，不进仓库
- 依赖锁定 + 定期升级
- SBOM（软件物料清单）随产物归档
- 生产部署遵循最小权限，CI 使用独立服务账户

> 流水线是质量的护栏，任何「临时跳过检查」的行为需记录并事后补测。
