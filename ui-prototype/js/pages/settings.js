/* ============================================
   系统设置页面（settings）
   - 租户管理 / API 密钥
   - LLM/VLM 配置 / 系统设置
   ============================================ */

// 注：SVG 图表辅助函数（svgBarChart / svgLineChart / svgPieChart
// / svgRing / statCardHTML）定义于 admin.js，本文件直接复用。

// ============================================================
// 页面 1：租户管理
// ============================================================
App.registerPage('settings/tenant', {
  title: '租户管理',
  render() {
    // 套餐对比数据
    const plans = [
      {
        name: '免费版', price: '¥0', period: '/永久',
        features: ['50 篇文档上限', '5 名用户', '基础 AI 问答', '社区支持'],
        current: false, highlight: false,
      },
      {
        name: 'SaaS 标准版', price: '¥3,285', period: '/月',
        features: ['无限文档', '200 名用户', '高级 RAG + 知识图谱', 'API 接入', '优先支持'],
        current: true, highlight: true,
      },
      {
        name: '企业版', price: '定制', period: '/年',
        features: ['私有化部署', '无限用户', '自定义 LLM 模型', 'SSO + 审计日志', '专属客户成功经理'],
        current: false, highlight: false,
      },
    ];

    // 用量统计
    const usage = [
      { label: '存储用量', current: 45.2, max: 100, unit: 'GB', percent: 45, color: 'primary' },
      { label: '用户数', current: 128, max: 200, unit: '人', percent: 64, color: 'success' },
      { label: 'API 调用', current: 12.3, max: 50, unit: 'K', percent: 25, color: 'warning' },
      { label: '知识库数', current: 6, max: 10, unit: '个', percent: 60, color: 'info' },
    ];

    return `
      <div class="page-header">
        <div class="page-title">租户管理</div>
        <div class="page-subtitle">管理企业租户信息、套餐与用量</div>
      </div>

      <!-- 租户信息卡片 -->
      <div class="card mb-6">
        <div class="card-body flex items-center gap-6">
          <div style="width:72px;height:72px;border-radius:16px;background:linear-gradient(135deg,#4B3FE3,#6B5FF7);display:flex;align-items:center;justify-content:center;font-size:36px;flex-shrink:0;">🧠</div>
          <div class="flex-1">
            <div class="flex items-center gap-3 mb-2">
              <span class="text-xl font-bold">智云科技有限公司</span>
              <span class="badge badge-primary">SaaS 标准版</span>
            </div>
            <div class="flex gap-6 text-sm text-muted">
              <span>${Utils.icon('globe', 14)} company.ekb.com</span>
              <span>${Utils.icon('calendar', 14)} 创建于 2025-03-15</span>
              <span>${Utils.icon('building', 14)} 统一社会信用代码 91110****</span>
            </div>
          </div>
          <button class="btn btn-secondary btn-sm" onclick="App.toast('编辑租户信息（原型演示）','info')">${Utils.icon('edit', 16)} 编辑信息</button>
        </div>
      </div>

      <div class="grid gap-4 mb-6" style="grid-template-columns:1fr 320px;">
        <div>
          <!-- 套餐对比 -->
          <div class="card mb-6">
            <div class="card-header"><div class="card-title">套餐对比</div></div>
            <div class="card-body">
              <div class="grid grid-3 gap-4">
                ${plans.map(p => `
                  <div class="card" style="padding:24px;border:2px solid ${p.highlight ? 'var(--primary)' : 'var(--border-light)'};position:relative;${p.highlight ? 'box-shadow:0 4px 16px rgba(75,63,227,0.12);' : ''}">
                    ${p.highlight ? '<span class="badge badge-primary" style="position:absolute;top:-10px;left:50%;transform:translateX(-50%);">当前套餐</span>' : ''}
                    <div class="text-md font-semibold mb-1">${p.name}</div>
                    <div class="mb-3">
                      <span class="text-3xl font-bold">${p.price}</span>
                      <span class="text-sm text-muted">${p.period}</span>
                    </div>
                    <div class="flex flex-col gap-2 mb-4">
                      ${p.features.map(f => '<div class="flex items-center gap-2 text-sm"><span style="color:var(--success);">' + Utils.icon('check', 14) + '</span><span>' + f + '</span></div>').join('')}
                    </div>
                    ${p.current
                      ? '<button class="btn btn-secondary btn-block btn-sm" disabled>当前套餐</button>'
                      : `<button class="btn ${p.name === '企业版' ? 'btn-secondary' : 'btn-primary'} btn-block btn-sm" onclick="App.toast('升级套餐申请已提交，稍后将有顾问联系','success')">${p.name === '企业版' ? '联系销售' : '升级到此'}</button>`}
                  </div>
                `).join('')}
              </div>
            </div>
          </div>

          <!-- 用量统计 -->
          <div class="card">
            <div class="card-header"><div class="card-title">用量统计</div><span class="text-xs text-muted">本月</span></div>
            <div class="card-body">
              <div class="grid grid-2 gap-5">
                ${usage.map(u => `
                  <div>
                    <div class="flex items-center justify-between mb-2">
                      <span class="text-sm font-medium">${u.label}</span>
                      <span class="text-sm"><span class="font-bold">${u.current}</span><span class="text-muted"> / ${u.max} ${u.unit}</span></span>
                    </div>
                    <div class="progress-bar"><div class="progress-bar-fill ${u.color}" style="width:${u.percent}%"></div></div>
                    <div class="text-xs text-muted mt-1">已使用 ${u.percent}%</div>
                  </div>
                `).join('')}
              </div>
            </div>
          </div>
        </div>

        <!-- 右侧订阅信息 -->
        <div>
          <div class="card mb-4">
            <div class="card-header"><div class="card-title">订阅信息</div></div>
            <div class="card-body">
              <div class="text-center mb-4">
                <div class="text-sm text-muted mb-1">当前月费</div>
                <div class="text-4xl font-bold text-primary">¥3,285<span class="text-sm text-muted font-normal">/月</span></div>
              </div>
              <div class="divider"></div>
              <div class="flex items-center justify-between mb-3">
                <span class="text-sm text-muted">下次扣费</span>
                <span class="text-sm font-medium">2026-08-01</span>
              </div>
              <div class="flex items-center justify-between mb-3">
                <span class="text-sm text-muted">支付方式</span>
                <span class="text-sm font-medium">企业对公账户</span>
              </div>
              <div class="flex items-center justify-between mb-4">
                <span class="text-sm text-muted">账单周期</span>
                <span class="text-sm font-medium">月付</span>
              </div>
              <button class="btn btn-primary btn-block" onclick="App.toast('升级套餐申请已提交','success')">${Utils.icon('trending', 16)} 升级套餐</button>
              <button class="btn btn-ghost btn-block btn-sm mt-2" onclick="App.toast('查看账单记录（原型演示）','info')">查看账单记录</button>
            </div>
          </div>

          <div class="card">
            <div class="card-header"><div class="card-title">域名与品牌</div></div>
            <div class="card-body">
              <div class="form-group">
                <label class="form-label">自定义域名</label>
                <div class="form-input-group">
                  <span class="form-input-prefix">https://</span>
                  <input class="form-input" value="company.ekb.com" style="border-radius:0 var(--radius) var(--radius) 0;">
                </div>
                <div class="form-hint">需添加 CNAME 记录指向 ekb.r2.cloudflarestorage.com</div>
              </div>
              <div class="form-group">
                <label class="form-label">企业 Logo</label>
                <div class="upload-zone" style="padding:20px;" onclick="App.toast('Logo 上传（原型演示）','info')">
                  <div style="font-size:32px;margin-bottom:8px;">🧠</div>
                  <div class="text-sm font-medium">点击或拖拽上传 Logo</div>
                  <div class="text-xs text-muted">支持 PNG/SVG，建议 256×256</div>
                </div>
              </div>
              <div class="form-group">
                <label class="form-label">主题色</label>
                <div class="flex gap-2">
                  ${['#4B3FE3', '#00B884', '#2196F3', '#FF9500', '#FF3B5C'].map((c, i) => 
                    `<span style="width:32px;height:32px;border-radius:50%;background:${c};cursor:pointer;${i === 0 ? 'box-shadow:0 0 0 3px ' + c + '44;' : ''}" onclick="App.toast('主题色已更新','success')"></span>`
                  ).join('')}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {},
});

// ============================================================
// 页面 2：API 密钥管理
// ============================================================
App.registerPage('settings/api', {
  title: 'API 密钥管理',
  render() {
    const keys = Mock.apiKeys;
    const scopeBadge = { '读写': 'badge-primary', '只读': 'badge-info' };
    const statusBadge = { active: 'badge-success', inactive: 'badge-neutral' };
    const statusText = { active: '启用', inactive: '已禁用' };

    // API 调用统计（近 7 天）
    const statLabels = ['06-29', '06-30', '07-01', '07-02', '07-03', '07-04', '07-05'];
    const statData = [234, 312, 289, 345, 401, 378, 423];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">API 密钥管理</div>
            <div class="page-subtitle">管理 API 凭证、Webhook 与调用统计</div>
          </div>
          <button class="btn btn-primary btn-sm" onclick="SettingsAPI.showCreate()">${Utils.icon('plus', 16)} 创建密钥</button>
        </div>
      </div>

      <div class="grid gap-4" style="grid-template-columns:1fr 300px;">
        <div>
          <!-- API 密钥表格 -->
          <div class="card mb-6">
            <div class="card-header">
              <div class="card-title">API 密钥</div>
              <span class="text-xs text-muted">${keys.length} 个密钥</span>
            </div>
            <div class="table-wrap" style="border:none;">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>名称</th>
                    <th>密钥</th>
                    <th>权限范围</th>
                    <th>创建时间</th>
                    <th>最后使用</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  ${keys.map(k => `
                    <tr>
                      <td class="font-medium">${k.name}</td>
                      <td>
                        <div class="flex items-center gap-2">
                          <code class="text-xs" style="background:var(--surface-muted);padding:2px 6px;border-radius:4px;font-family:var(--font-mono);">${k.key}</code>
                          <button class="icon-btn btn-sm" style="width:24px;height:24px;" onclick="SettingsAPI.copy('${k.key}')" title="复制">${Utils.icon('copy', 14)}</button>
                        </div>
                      </td>
                      <td><span class="badge ${scopeBadge[k.scope]}">${k.scope}</span></td>
                      <td class="text-muted">${k.created}</td>
                      <td class="text-muted">${k.lastUsed}</td>
                      <td><span class="badge ${statusBadge[k.status]}">${statusText[k.status]}</span></td>
                      <td>
                        <button class="btn btn-ghost btn-sm btn-icon" onclick="App.toast('查看密钥详情（原型演示）','info')" title="查看">${Utils.icon('eye', 16)}</button>
                        <button class="btn btn-ghost btn-sm btn-icon" onclick="SettingsAPI.toggle('${k.name}')" title="${k.status === 'active' ? '禁用' : '启用'}">${Utils.icon('zap', 16)}</button>
                        <button class="btn btn-ghost btn-sm btn-icon" onclick="SettingsAPI.del('${k.name}')" title="删除" style="color:var(--danger);">${Utils.icon('trash', 16)}</button>
                      </td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <!-- Webhook 配置 -->
          <div class="card">
            <div class="card-header">
              <div class="card-title">Webhook 配置</div>
              <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
            </div>
            <div class="card-body">
              <div class="form-group">
                <label class="form-label">Webhook URL</label>
                <div class="form-input-group">
                  <input class="form-input" value="https://api.company.com/ekb-webhook" style="border-radius:var(--radius) 0 0 var(--radius);">
                  <button class="btn btn-secondary" onclick="App.toast('测试请求已发送，返回 200 OK','success')" style="border-radius:0 var(--radius) var(--radius) 0;">${Utils.icon('send', 16)} 测试</button>
                </div>
              </div>
              <div class="form-group">
                <label class="form-label">订阅事件</label>
                <div class="grid grid-2 gap-2">
                  ${[
                    { label: '文档创建', checked: true },
                    { label: '文档更新', checked: true },
                    { label: '审核通过', checked: true },
                    { label: '审核驳回', checked: false },
                    { label: 'AI 问答', checked: true },
                    { label: '用户注册', checked: false },
                    { label: '知识库创建', checked: true },
                    { label: '标签变更', checked: false },
                  ].map(e => `
                    <label class="checkbox"><input type="checkbox" ${e.checked ? 'checked' : ''}> ${e.label}</label>
                  `).join('')}
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 右侧 -->
        <div>
          <div class="card mb-4">
            <div class="card-header"><div class="card-title">API 文档</div></div>
            <div class="card-body text-center">
              <div style="font-size:40px;margin-bottom:12px;">📚</div>
              <div class="text-sm text-secondary mb-3">查看完整的 REST API 文档、SDK 与示例代码</div>
              <button class="btn btn-primary btn-block btn-sm" onclick="App.toast('API 文档（原型演示）','info')">${Utils.icon('fileText', 16)} 查看文档</button>
              <div class="flex gap-2 mt-2">
                <button class="btn btn-secondary btn-block btn-sm" onclick="App.toast('SDK 下载（原型演示）','info')">Python SDK</button>
                <button class="btn btn-secondary btn-block btn-sm" onclick="App.toast('SDK 下载（原型演示）','info')">Node SDK</button>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-header"><div class="card-title">调用统计</div><span class="text-xs text-muted">近 7 天</span></div>
            <div class="card-body">
              ${svgBarChart(statData, statLabels, { color: '#4B3FE3', width: 280, height: 180 })}
              <div class="divider"></div>
              <div class="flex items-center justify-between mb-2">
                <span class="text-sm text-muted">总调用</span>
                <span class="font-bold">2,382</span>
              </div>
              <div class="flex items-center justify-between mb-2">
                <span class="text-sm text-muted">成功率</span>
                <span class="font-bold text-success">99.8%</span>
              </div>
              <div class="flex items-center justify-between">
                <span class="text-sm text-muted">平均延迟</span>
                <span class="font-bold">128ms</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {},
});

// API 密钥操作
window.SettingsAPI = {
  showCreate() {
    App.modal({
      title: '创建 API 密钥',
      body: `
        <div class="form-group">
          <label class="form-label">密钥名称 <span class="required">*</span></label>
          <input class="form-input" id="key-name" placeholder="如：数据分析平台">
        </div>
        <div class="form-group">
          <label class="form-label">权限范围</label>
          <select class="form-select" id="key-scope">
            <option value="读写">读写（可创建、修改、删除）</option>
            <option value="只读">只读（仅查询）</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">有效期</label>
          <select class="form-select" id="key-expire">
            <option>永久有效</option>
            <option>90 天</option>
            <option>180 天</option>
            <option>365 天</option>
          </select>
        </div>
        <div class="form-hint">创建后请妥善保管密钥，密钥仅在创建时完整显示一次</div>
      `,
      confirmText: '创建密钥',
      onConfirm(overlay) {
        const name = overlay.querySelector('#key-name').value.trim();
        if (!name) { App.toast('请输入密钥名称', 'warning'); return; }
        overlay.remove();
        App.toast('密钥「' + name + '」已创建', 'success');
      },
    });
  },
  copy(key) {
    // 模拟复制到剪贴板
    if (navigator.clipboard) {
      navigator.clipboard.writeText(key).catch(function () {});
    }
    App.toast('已复制到剪贴板', 'success');
  },
  toggle(name) {
    App.toast('密钥状态已更新', 'info');
  },
  del(name) {
    App.modal({
      title: '确认删除密钥',
      body: '<div class="flex items-center gap-3 p-4 rounded" style="background:var(--danger-bg);"><span style="font-size:24px;">⚠️</span><div><div class="font-medium">删除密钥「' + name + '」</div><div class="text-sm text-muted">删除后使用该密钥的应用将无法访问 API，此操作不可撤销</div></div></div>',
      confirmText: '确认删除',
      onConfirm(overlay) {
        overlay.remove();
        App.toast('密钥已删除', 'warning');
      },
    });
  },
};

// ============================================================
// 页面 3：LLM/VLM 配置
// ============================================================
App.registerPage('settings/llm', {
  title: 'LLM/VLM 配置',
  render() {
    const configs = Mock.llmConfigs;
    const statusText = { active: '已启用', inactive: '已停用' };

    // Token 用量数据（各模型）
    const tokenLabels = ['GPT-4o', 'Claude-3.5', 'Qwen-VL', 'DeepSeek'];
    const tokenData = [45200, 23800, 12100, 4500];

    // 模型路由策略
    const routes = [
      { scenario: '简单问答', model: 'DeepSeek-V3', reason: '低成本、响应快', cost: '¥0.001/次' },
      { scenario: '复杂推理', model: 'GPT-4o', reason: '推理能力强', cost: '¥0.05/次' },
      { scenario: '长文档总结', model: 'Claude-3.5-Sonnet', reason: '长上下文支持', cost: '¥0.03/次' },
      { scenario: '图像理解', model: 'Qwen-VL-Max', reason: '视觉能力优秀', cost: '¥0.02/次' },
    ];

    // VLM 配置
    const vlmConfigs = [
      { name: 'Qwen-VL-Max', provider: '阿里云通义', status: 'active', desc: '高质量视觉理解，支持图表、文档 OCR', cost: '¥0.02/次', latency: '2.1s' },
      { name: 'Qwen-VL-Plus', provider: '阿里云通义', status: 'active', desc: '平衡质量与成本，适用于常规图像', cost: '¥0.008/次', latency: '1.3s' },
      { name: '本地部署 PP-OCRv5', provider: '自建 GPU 集群', status: 'active', desc: '轻量 OCR 引擎，零 API 成本', cost: '¥0/次', latency: '0.3s' },
    ];

    // 成本对比
    const costCompare = [
      { task: '简单 OCR（票据识别）', qwen: '¥0.008/次', local: '¥0/次', save: '100%' },
      { task: '图表理解', qwen: '¥0.02/次', plus: '¥0.008/次', save: '60%' },
      { task: '复杂文档解析', max: '¥0.02/次', plus: '¥0.008/次', save: '60%' },
    ];

    return `
      <div class="page-header">
        <div class="page-title">LLM/VLM 配置</div>
        <div class="page-subtitle">管理大语言模型与视觉语言模型的接入与路由策略</div>
      </div>

      <!-- 标签页 -->
      <div class="tabs mb-4">
        <div class="tab active" data-tab="llm">LLM 模型</div>
        <div class="tab" data-tab="vlm">VLM 视觉模型</div>
        <div class="tab" data-tab="embed">Embedding 模型</div>
      </div>

      <!-- LLM 标签页 -->
      <div class="tab-content active" id="tab-llm">
        <!-- 模型提供商卡片 -->
        <div class="grid grid-2 gap-4 mb-6">
          ${configs.map(c => `
            <div class="card">
              <div class="card-body">
                <div class="flex items-center justify-between mb-3">
                  <div class="flex items-center gap-3">
                    <div style="width:40px;height:40px;border-radius:10px;background:var(--primary-bg);display:flex;align-items:center;justify-content:center;font-size:20px;">
                      ${c.provider === 'OpenAI' ? '🤖' : c.provider === 'Anthropic' ? '🧠' : c.provider === 'Qwen' ? '🌐' : '💡'}
                    </div>
                    <div>
                      <div class="font-semibold">${c.model}</div>
                      <div class="text-xs text-muted">${c.provider}</div>
                    </div>
                  </div>
                  <label class="switch"><input type="checkbox" ${c.status === 'active' ? 'checked' : ''}><span class="switch-slider"></span></label>
                </div>
                <div class="flex items-center gap-2 mb-3">
                  <code class="text-xs" style="background:var(--surface-muted);padding:4px 8px;border-radius:6px;font-family:var(--font-mono);">${c.apiKey}</code>
                  <button class="icon-btn btn-sm" style="width:24px;height:24px;" onclick="App.toast('已复制到剪贴板','success')">${Utils.icon('copy', 14)}</button>
                </div>
                <div class="grid grid-2 gap-3 mb-3">
                  <div class="p-3 rounded" style="background:var(--surface-muted);">
                    <div class="text-xs text-muted mb-1">本月用量</div>
                    <div class="font-semibold">${c.usage}</div>
                  </div>
                  <div class="p-3 rounded" style="background:var(--surface-muted);">
                    <div class="text-xs text-muted mb-1">本月费用</div>
                    <div class="font-semibold">${c.cost}</div>
                  </div>
                </div>
                <div class="flex gap-2">
                  <button class="btn btn-secondary btn-sm btn-block" onclick="App.toast('${c.model} 连接测试成功，延迟 1.2s','success')">${Utils.icon('zap', 14)} 测试连接</button>
                  <button class="btn btn-ghost btn-sm btn-icon" onclick="App.toast('编辑模型配置（原型演示）','info')">${Utils.icon('edit', 16)}</button>
                </div>
              </div>
            </div>
          `).join('')}
        </div>

        <!-- 模型路由策略 + Token 用量 -->
        <div class="grid grid-2 gap-4">
          <div class="card">
            <div class="card-header"><div class="card-title">模型路由策略</div><span class="badge badge-primary">智能调度</span></div>
            <div class="table-wrap" style="border:none;">
              <table class="data-table">
                <thead><tr><th>场景</th><th>模型</th><th>原因</th><th>成本</th></tr></thead>
                <tbody>
                  ${routes.map(r => `
                    <tr>
                      <td><span class="badge badge-info">${r.scenario}</span></td>
                      <td class="font-medium">${r.model}</td>
                      <td class="text-muted text-xs">${r.reason}</td>
                      <td class="text-xs">${r.cost}</td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <div class="card">
            <div class="card-header"><div class="card-title">Token 用量分布</div><span class="text-xs text-muted">本月</span></div>
            <div class="card-body">${svgBarChart(tokenData, tokenLabels, { color: '#2196F3' })}</div>
          </div>
        </div>
      </div>

      <!-- VLM 标签页 -->
      <div class="tab-content" id="tab-vlm" style="display:none;">
        <!-- VLM 提供商配置 -->
        <div class="grid grid-3 gap-4 mb-6">
          ${vlmConfigs.map(v => `
            <div class="card">
              <div class="card-body">
                <div class="flex items-center justify-between mb-3">
                  <div class="font-semibold">${v.name}</div>
                  <label class="switch"><input type="checkbox" ${v.status === 'active' ? 'checked' : ''}><span class="switch-slider"></span></label>
                </div>
                <div class="text-xs text-muted mb-2">${v.provider}</div>
                <div class="text-sm text-secondary mb-3" style="line-height:1.6;min-height:42px;">${v.desc}</div>
                <div class="flex items-center justify-between text-sm mb-3">
                  <span class="text-muted">成本</span><span class="font-medium">${v.cost}</span>
                </div>
                <div class="flex items-center justify-between text-sm">
                  <span class="text-muted">延迟</span><span class="font-medium">${v.latency}</span>
                </div>
              </div>
            </div>
          `).join('')}
        </div>

        <!-- 分层路由策略（流程图） -->
        <div class="card mb-6">
          <div class="card-header"><div class="card-title">分层路由策略</div><span class="badge badge-success">降本 62%</span></div>
          <div class="card-body">
            <div class="flex items-center justify-between gap-2">
              <div class="card text-center" style="padding:20px;flex:1;border:2px solid var(--success);">
                <div style="font-size:32px;margin-bottom:8px;">📄</div>
                <div class="font-semibold mb-1">PP-OCRv5</div>
                <div class="text-xs text-muted mb-2">简单 OCR · 本地部署</div>
                <span class="badge badge-success">68% 流量</span>
                <div class="text-xs text-muted mt-2">¥0/次 · 0.3s</div>
              </div>
              <div style="font-size:24px;color:var(--text-muted);">→</div>
              <div class="card text-center" style="padding:20px;flex:1;border:2px solid var(--warning);">
                <div style="font-size:32px;margin-bottom:8px;">🖼️</div>
                <div class="font-semibold mb-1">Qwen-VL-Plus</div>
                <div class="text-xs text-muted mb-2">复杂理解 · 云端</div>
                <span class="badge badge-warning">22% 流量</span>
                <div class="text-xs text-muted mt-2">¥0.008/次 · 1.3s</div>
              </div>
              <div style="font-size:24px;color:var(--text-muted);">→</div>
              <div class="card text-center" style="padding:20px;flex:1;border:2px solid var(--primary);">
                <div style="font-size:32px;margin-bottom:8px;">🎯</div>
                <div class="font-semibold mb-1">Qwen-VL-Max</div>
                <div class="text-xs text-muted mb-2">高质量 · 云端</div>
                <span class="badge badge-primary">10% 流量</span>
                <div class="text-xs text-muted mt-2">¥0.02/次 · 2.1s</div>
              </div>
            </div>
            <div class="divider"></div>
            <div class="text-sm text-secondary" style="line-height:1.7;">
              <strong>路由逻辑：</strong>图像先经 PP-OCRv5 进行轻量 OCR 与版面分析；若需语义理解（如图表、复杂文档），升级至 Qwen-VL-Plus；对高精度场景（如医疗、法律文档），最终路由至 Qwen-VL-Max。该分层策略使整体 VLM 成本降低 62%。
            </div>
          </div>
        </div>

        <!-- 成本对比表格 -->
        <div class="card">
          <div class="card-header"><div class="card-title">成本对比</div></div>
          <div class="table-wrap" style="border:none;">
            <table class="data-table">
              <thead><tr><th>任务类型</th><th>直接使用 Max</th><th>分层路由</th><th>节省</th></tr></thead>
              <tbody>
                ${costCompare.map(c => `
                  <tr>
                    <td class="font-medium">${c.task}</td>
                    <td>${c.max || c.qwen}</td>
                    <td>${c.local || c.plus}</td>
                    <td><span class="badge badge-success">省 ${c.save}</span></td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Embedding 标签页 -->
      <div class="tab-content" id="tab-embed" style="display:none;">
        <div class="grid grid-2 gap-4">
          <div class="card">
            <div class="card-header"><div class="card-title">模型选择</div></div>
            <div class="card-body">
              <div class="form-group">
                <label class="form-label">Embedding 模型</label>
                <select class="form-select">
                  <option selected>bge-m3（BAAI，推荐）</option>
                  <option>text-embedding-3-small（OpenAI）</option>
                  <option>text-embedding-3-large（OpenAI）</option>
                  <option>m3e-base（Moka AI）</option>
                  <option>gte-large-zh（阿里达摩院）</option>
                </select>
                <div class="form-hint">bge-m3 支持多语言、多粒度，向量维度 1024</div>
              </div>
              <div class="form-group">
                <label class="form-label">向量维度</label>
                <select class="form-select">
                  <option selected>1024（推荐）</option>
                  <option>768</option>
                  <option>512</option>
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">索引类型</label>
                <select class="form-select">
                  <option selected>HNSW（高召回率）</option>
                  <option>IVF_FLAT（均衡）</option>
                  <option>IVF_PQ（低内存）</option>
                </select>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-header"><div class="card-title">索引参数</div></div>
            <div class="card-body">
              <div class="form-group">
                <label class="form-label">HNSW M 参数 <span class="text-muted">（连接数）</span></label>
                <input class="form-input" type="number" value="16" min="4" max="64">
                <div class="form-hint">M 越大召回率越高，但内存占用增加</div>
              </div>
              <div class="form-group">
                <label class="form-label">efConstruction <span class="text-muted">（构建参数）</span></label>
                <input class="form-input" type="number" value="256" min="32" max="1024">
              </div>
              <div class="form-group">
                <label class="form-label">efSearch <span class="text-muted">（搜索参数）</span></label>
                <input class="form-input" type="number" value="128" min="16" max="512">
              </div>
              <div class="form-group">
                <label class="form-label">分块大小 <span class="text-muted">（chunk size）</span></label>
                <input class="form-input" type="number" value="512">
                <div class="form-hint">每个文本块的最大 token 数</div>
              </div>
              <button class="btn btn-primary btn-block" onclick="App.toast('参数已保存，将重新构建索引','success')">${Utils.icon('refresh', 16)} 重建向量索引</button>
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {
    // 标签页切换
    document.querySelectorAll('.tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.tabs .tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.querySelectorAll('.tab-content').forEach(c => { c.style.display = 'none'; c.classList.remove('active'); });
        const target = document.getElementById('tab-' + tab.dataset.tab);
        if (target) { target.style.display = 'block'; target.classList.add('active'); }
      });
    });
  },
});

// ============================================================
// 页面 4：系统设置
// ============================================================
App.registerPage('settings/system', {
  title: '系统设置',
  render() {
    const navItems = [
      { key: 'general', label: '通用', icon: 'settings' },
      { key: 'appearance', label: '外观', icon: 'image' },
      { key: 'notify', label: '通知', icon: 'bell' },
      { key: 'security', label: '安全', icon: 'shield' },
      { key: 'storage', label: '存储', icon: 'database' },
      { key: 'advanced', label: '高级', icon: 'cpu' },
    ];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">系统设置</div>
            <div class="page-subtitle">配置平台全局参数与偏好设置</div>
          </div>
          <button class="btn btn-primary btn-sm" onclick="App.toast('设置已保存','success')">${Utils.icon('check', 16)} 保存设置</button>
        </div>
      </div>

      <div class="flex gap-6">
        <!-- 左侧设置导航 -->
        <div style="width:200px;flex-shrink:0;">
          <div class="card">
            <div class="card-body" style="padding:8px;">
              ${navItems.map((n, i) => `
                <div class="list-item ${i === 0 ? 'active' : ''}" data-panel="${n.key}" onclick="SettingsSystem.switch('${n.key}')">
                  <span style="color:var(--text-secondary);">${Utils.icon(n.icon, 18)}</span>
                  <span class="text-sm">${n.label}</span>
                </div>
              `).join('')}
            </div>
          </div>
        </div>

        <!-- 右侧设置内容 -->
        <div class="flex-1">
          <!-- 通用设置 -->
          <div class="settings-panel" id="panel-general">
            <div class="card">
              <div class="card-header"><div class="card-title">通用设置</div></div>
              <div class="card-body">
                <div class="grid grid-2 gap-4">
                  <div class="form-group">
                    <label class="form-label">企业名称</label>
                    <input class="form-input" value="智云科技有限公司">
                  </div>
                  <div class="form-group">
                    <label class="form-label">系统语言</label>
                    <select class="form-select"><option selected>简体中文</option><option>English</option><option>繁體中文</option></select>
                  </div>
                  <div class="form-group">
                    <label class="form-label">时区</label>
                    <select class="form-select"><option selected>(UTC+08:00) 北京</option><option>(UTC+00:00) 伦敦</option><option>(UTC-08:00) 旧金山</option></select>
                  </div>
                  <div class="form-group">
                    <label class="form-label">默认知识库</label>
                    <select class="form-select"><option selected>产品研发知识库</option>${Mock.knowledgeBases.slice(1).map(kb => '<option>' + kb.name + '</option>').join('')}</select>
                  </div>
                  <div class="form-group">
                    <label class="form-label">文档版本保留数</label>
                    <input class="form-input" type="number" value="10">
                    <div class="form-hint">保留最近 N 个版本的编辑历史</div>
                  </div>
                  <div class="form-group">
                    <label class="form-label">AI 默认模型</label>
                    <select class="form-select"><option selected>DeepSeek-V3（智能路由）</option><option>GPT-4o</option><option>Claude-3.5-Sonnet</option></select>
                  </div>
                </div>
                <div class="divider"></div>
                <div class="flex items-center justify-between">
                  <div>
                    <div class="text-sm font-medium">自动生成摘要</div>
                    <div class="text-xs text-muted">文档发布时自动调用 AI 生成内容摘要</div>
                  </div>
                  <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                </div>
              </div>
            </div>
          </div>

          <!-- 外观设置 -->
          <div class="settings-panel" id="panel-appearance" style="display:none;">
            <div class="card">
              <div class="card-header"><div class="card-title">外观设置</div></div>
              <div class="card-body">
                <div class="form-group">
                  <label class="form-label">主题色</label>
                  <div class="flex gap-3">
                    ${['#4B3FE3', '#00B884', '#2196F3', '#FF9500', '#FF3B5C'].map((c, i) => 
                      '<div style="width:40px;height:40px;border-radius:10px;background:' + c + ';cursor:pointer;display:flex;align-items:center;justify-content:center;color:white;' + (i === 0 ? 'box-shadow:0 0 0 3px ' + c + '44;' : '') + '" onclick="App.toast(\'主题色已更新\',\'success\')">' + (i === 0 ? Utils.icon('check', 18) : '') + '</div>'
                    ).join('')}
                  </div>
                </div>
                <div class="divider"></div>
                <div class="flex items-center justify-between mb-4">
                  <div>
                    <div class="text-sm font-medium">暗色模式</div>
                    <div class="text-xs text-muted">切换至深色主题，减轻视觉疲劳</div>
                  </div>
                  <label class="switch"><input type="checkbox"><span class="switch-slider"></span></label>
                </div>
                <div class="flex items-center justify-between mb-4">
                  <div>
                    <div class="text-sm font-medium">侧边栏默认折叠</div>
                    <div class="text-xs text-muted">启动时折叠侧边栏以获得更大内容区域</div>
                  </div>
                  <label class="switch"><input type="checkbox"><span class="switch-slider"></span></label>
                </div>
                <div class="flex items-center justify-between">
                  <div>
                    <div class="text-sm font-medium">紧凑模式</div>
                    <div class="text-xs text-muted">减小间距，在单屏显示更多内容</div>
                  </div>
                  <label class="switch"><input type="checkbox"><span class="switch-slider"></span></label>
                </div>
              </div>
            </div>
          </div>

          <!-- 通知设置 -->
          <div class="settings-panel" id="panel-notify" style="display:none;">
            <div class="card">
              <div class="card-header"><div class="card-title">通知设置</div></div>
              <div class="card-body">
                ${[
                  { label: '文档审核通知', desc: '有文档待审核时通知', checked: true },
                  { label: 'AI 问答异常', desc: 'AI 回答失败或超时时通知', checked: true },
                  { label: '知识缺口预警', desc: '检测到高频搜索无结果时通知', checked: true },
                  { label: '用户反馈通知', desc: '收到新用户反馈时通知', checked: false },
                  { label: '系统资源告警', desc: '存储或 API 用量超阈值时通知', checked: true },
                  { label: '每日运营简报', desc: '每天 9:00 推送运营数据摘要', checked: false },
                ].map(n => `
                  <div class="flex items-center justify-between mb-4">
                    <div>
                      <div class="text-sm font-medium">${n.label}</div>
                      <div class="text-xs text-muted">${n.desc}</div>
                    </div>
                    <label class="switch"><input type="checkbox" ${n.checked ? 'checked' : ''}><span class="switch-slider"></span></label>
                  </div>
                `).join('')}
                <div class="divider"></div>
                <div class="form-group">
                  <label class="form-label">通知渠道</label>
                  <div class="flex gap-4">
                    <label class="checkbox"><input type="checkbox" checked> 站内信</label>
                    <label class="checkbox"><input type="checkbox" checked> 邮件</label>
                    <label class="checkbox"><input type="checkbox"> 飞书</label>
                    <label class="checkbox"><input type="checkbox"> 企业微信</label>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- 安全设置 -->
          <div class="settings-panel" id="panel-security" style="display:none;">
            <div class="card">
              <div class="card-header"><div class="card-title">安全设置</div></div>
              <div class="card-body">
                <div class="form-group">
                  <label class="form-label">密码策略</label>
                  <select class="form-select"><option selected>强（至少 12 位，含大小写+数字+符号）</option><option>中（至少 8 位，含字母+数字）</option><option>弱（至少 6 位）</option></select>
                </div>
                <div class="form-group">
                  <label class="form-label">密码过期天数</label>
                  <input class="form-input" type="number" value="90">
                </div>
                <div class="divider"></div>
                <div class="flex items-center justify-between mb-4">
                  <div>
                    <div class="text-sm font-medium">二次验证（2FA）</div>
                    <div class="text-xs text-muted">要求用户登录时进行二次身份验证</div>
                  </div>
                  <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                </div>
                <div class="flex items-center justify-between mb-4">
                  <div>
                    <div class="text-sm font-medium">SSO 单点登录</div>
                    <div class="text-xs text-muted">支持 SAML / OAuth / OIDC 协议</div>
                  </div>
                  <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                </div>
                <div class="form-group">
                  <label class="form-label">IP 白名单</label>
                  <textarea class="form-textarea" placeholder="每行一个 IP 或 CIDR">10.0.0.0/8
192.168.1.0/24</textarea>
                  <div class="form-hint">仅白名单内 IP 可访问管理后台，留空则不限制</div>
                </div>
                <div class="form-group">
                  <label class="form-label">会话超时</label>
                  <select class="form-select"><option>30 分钟</option><option selected>2 小时</option><option>8 小时</option><option>永不超时</option></select>
                </div>
              </div>
            </div>
          </div>

          <!-- 存储设置 -->
          <div class="settings-panel" id="panel-storage" style="display:none;">
            <div class="card">
              <div class="card-header"><div class="card-title">存储设置</div></div>
              <div class="card-body">
                <div class="form-group">
                  <label class="form-label">对象存储类型</label>
                  <select class="form-select"><option selected>MinIO（自建）</option><option>AWS S3</option><option>阿里云 OSS</option><option>腾讯云 COS</option></select>
                </div>
                <div class="grid grid-2 gap-4">
                  <div class="form-group">
                    <label class="form-label">MinIO Endpoint</label>
                    <input class="form-input" value="https://minio.company.com:9000">
                  </div>
                  <div class="form-group">
                    <label class="form-label">Bucket</label>
                    <input class="form-input" value="ekb-documents">
                  </div>
                  <div class="form-group">
                    <label class="form-label">Access Key</label>
                    <input class="form-input" type="password" value="minioadmin">
                  </div>
                  <div class="form-group">
                    <label class="form-label">Secret Key</label>
                    <input class="form-input" type="password" value="********">
                  </div>
                </div>
                <button class="btn btn-secondary btn-sm" onclick="App.toast('存储连接测试成功','success')">${Utils.icon('zap', 14)} 测试连接</button>
                <div class="divider"></div>
                <div class="form-group">
                  <label class="form-label">备份策略</label>
                  <select class="form-select"><option selected>每日自动备份（保留 30 天）</option><option>每周备份（保留 12 周）</option><option>手动备份</option></select>
                </div>
                <div class="form-group">
                  <label class="form-label">自动清理策略</label>
                  <select class="form-select"><option selected>清理 1 年未访问的文档版本</option><option>清理 6 月未访问的文档版本</option><option>不自动清理</option></select>
                </div>
                <div class="flex items-center justify-between">
                  <div>
                    <div class="text-sm font-medium">启用压缩存储</div>
                    <div class="text-xs text-muted">对文档附件启用 gzip 压缩，节省约 40% 空间</div>
                  </div>
                  <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                </div>
              </div>
            </div>
          </div>

          <!-- 高级设置 -->
          <div class="settings-panel" id="panel-advanced" style="display:none;">
            <div class="card">
              <div class="card-header"><div class="card-title">高级设置</div><span class="badge badge-warning">谨慎操作</span></div>
              <div class="card-body">
                <div class="form-group">
                  <label class="form-label">API 请求限流</label>
                  <input class="form-input" type="number" value="100">
                  <div class="form-hint">每分钟最大 API 请求数</div>
                </div>
                <div class="form-group">
                  <label class="form-label">RAG 检索 Top-K</label>
                  <input class="form-input" type="number" value="5">
                  <div class="form-hint">每次检索返回的相关文档片段数</div>
                </div>
                <div class="flex items-center justify-between mb-4">
                  <div>
                    <div class="text-sm font-medium">调试模式</div>
                    <div class="text-xs text-muted">输出详细日志，仅用于开发环境</div>
                  </div>
                  <label class="switch"><input type="checkbox"><span class="switch-slider"></span></label>
                </div>
                <div class="divider"></div>
                <div class="text-sm font-medium mb-3">危险操作</div>
                <div class="flex gap-3">
                  <button class="btn btn-secondary btn-sm" onclick="App.toast('缓存已清空','success')">${Utils.icon('trash', 14)} 清空缓存</button>
                  <button class="btn btn-secondary btn-sm" onclick="App.toast('索引重建已启动','info')">${Utils.icon('refresh', 14)} 重建索引</button>
                  <button class="btn btn-danger btn-sm" onclick="App.toast('请谨慎操作！','warning')">${Utils.icon('alert', 14)} 重置系统</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {},
});

// 系统设置面板切换
window.SettingsSystem = {
  switch(key) {
    document.querySelectorAll('.settings-panel').forEach(p => { p.style.display = 'none'; });
    document.querySelectorAll('[data-panel]').forEach(n => n.classList.remove('active'));
    const panel = document.getElementById('panel-' + key);
    const nav = document.querySelector('[data-panel="' + key + '"]');
    if (panel) panel.style.display = 'block';
    if (nav) nav.classList.add('active');
  },
};
