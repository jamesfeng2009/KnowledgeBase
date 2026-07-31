# 贡献指南 CONTRIBUTING

> 感谢你考虑为 EnterpriseKB 贡献代码！本指南规范了开发流程、代码规范与提交要求。

## 1. 贡献流程总览

1. Fork 仓库并克隆到本地
2. 基于 `develop` 分支创建特性分支
3. 编写代码与测试
4. 通过本地检查后提交 Pull Request
5. 等待 Code Review 与 CI 校验
6. 合并后由维护者发布

## 2. 开发环境准备

```bash
git clone https://github.com/<your-fork>/enterprise-kb.git
cd enterprise-kb
git remote add upstream https://github.com/your-org/enterprise-kb.git
pip install -r requirements-dev.txt
pre-commit install
```

## 3. 代码规范

### 3.1 Python 代码

- 遵循 PEP 8，使用 `black` 格式化（行宽 100）
- 使用 `ruff` 进行静态检查
- 类型注解必须完整，通过 `mypy` 严格模式

```python
# Good
def retrieve(query: str, top_k: int = 5) -> list[Chunk]:
    ...

# Bad
def retrieve(query, top_k=5):
    ...
```

### 3.2 命名约定

| 类型 | 风格 | 示例 |
| --- | --- | --- |
| 类 | PascalCase | DocumentParser |
| 函数/变量 | snake_case | parse_document |
| 常量 | UPPER_SNAKE | MAX_FILE_SIZE |
| 私有 | 前缀下划线 | _extract_body |

### 3.3 前端代码

- Vue 组件使用 `<script setup>` 组合式 API
- CSS 采用 BEM 命名，复用设计系统变量
- 提交前运行 `npm run lint` 与 `npm run typecheck`

## 4. 提交规范

采用 Conventional Commits：

```
<type>(<scope>): <subject>

<body>

<footer>
```

### 4.1 type 取值

| type | 说明 |
| --- | --- |
| feat | 新功能 |
| fix | Bug 修复 |
| docs | 文档变更 |
| style | 代码格式（不影响逻辑） |
| refactor | 重构 |
| perf | 性能优化 |
| test | 测试相关 |
| chore | 构建/工具变更 |

### 4.2 示例

```
feat(retrieval): 支持 RRF 融合算法可配置

新增 hybrid.fusion 配置项，支持 rrf 与 weighted 两种融合策略。

Closes #234
```

## 5. 分支与 PR 规范

### 5.1 分支命名

- 特性：`feat/<short-desc>`
- 修复：`fix/<issue-id>-<desc>`
- 文档：`docs/<desc>`

### 5.2 PR 标题

与提交信息格式一致，一个 PR 只解决一件事。

### 5.3 PR 描述模板

```markdown
## 变更说明
（简述本 PR 做了什么、为什么）

## 变更类型
- [ ] 新功能
- [ ] Bug 修复
- [ ] 重构
- [ ] 文档

## 测试
- [ ] 已新增/更新单元测试
- [ ] 本地全部测试通过
- [ ] 覆盖率未下降

## 关联 Issue
Closes #xxx
```

## 6. 测试要求

- 新功能必须附带单元测试，覆盖率不低于 80%
- Bug 修复需补充回归测试
- 使用 `pytest` 运行：

```bash
pytest tests/ -v --cov=backend --cov-report=term-missing
```

## 7. Code Review 要点

审查者关注：

1. 逻辑正确性与边界处理
2. 是否符合架构与设计原则
3. 测试是否充分
4. 是否引入安全风险（如 SQL 注入、硬编码密钥）
5. 性能影响（N+1 查询、不必要的全量加载）

## 8. 文档贡献

- 新增功能需同步更新对应文档
- API 变更需更新接口文档与示例
- 鼓励补充使用教程与最佳实践

## 9. 行为准则

- 保持友善与尊重，对事不对人
- 欢迎各水平开发者贡献，耐心解答疑问
- 严禁歧视、骚扰与人身攻击

## 10. 发布流程（维护者）

1. 合并 PR 到 `develop`
2. 定期从 `develop` 合并到 `main` 形成发布分支
3. 打 Tag 并生成 CHANGELOG
4. 触发 CI 构建镜像并发布

> 首次贡献者可认领 `good first issue` 标签的任务，降低上手难度。
