/* ============================================
   运营治理页面（admin）
   - 运营看板 / 健康度 / 审核工作流
   - 用户权限 / 标签管理 / 使用报表 / 反馈管理
   ============================================ */

// === SVG 图表辅助函数 ===

// 柱状图：data 为数值数组，labels 为 X 轴标签
function svgBarChart(data, labels, opts) {
  opts = opts || {};
  const w = opts.width || 560, h = opts.height || 220;
  const color = opts.color || '#4B3FE3';
  const pad = { t: 20, r: 12, b: 30, l: 40 };
  const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;
  const max = Math.max.apply(null, data) * 1.18 || 1;
  const slot = cw / data.length;
  const bw = Math.min(slot * 0.55, 38);
  let bars = '', xLabels = '';
  data.forEach(function (v, i) {
    const bh = (v / max) * ch;
    const x = pad.l + i * slot + (slot - bw) / 2;
    const y = pad.t + ch - bh;
    bars += '<rect x="' + x + '" y="' + y + '" width="' + bw + '" height="' + bh + '" rx="4" fill="' + color + '" opacity="0.9"/>';
    bars += '<text x="' + (x + bw / 2) + '" y="' + (y - 5) + '" text-anchor="middle" font-size="10" fill="#4E4F6B" font-weight="600">' + v + '</text>';
    xLabels += '<text x="' + (pad.l + i * slot + slot / 2) + '" y="' + (h - 10) + '" text-anchor="middle" font-size="10" fill="#8B8D9F">' + labels[i] + '</text>';
  });
  let grid = '';
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (ch / 4) * i;
    grid += '<line x1="' + pad.l + '" y1="' + y + '" x2="' + (w - pad.r) + '" y2="' + y + '" stroke="#F0F1F5" stroke-width="1"/>';
    const val = Math.round(max - (max / 4) * i);
    grid += '<text x="' + (pad.l - 6) + '" y="' + (y + 3) + '" text-anchor="end" font-size="9" fill="#8B8D9F">' + val + '</text>';
  }
  return '<svg viewBox="0 0 ' + w + ' ' + h + '" style="width:100%;height:auto;" xmlns="http://www.w3.org/2000/svg">' + grid + bars + xLabels + '</svg>';
}

// 折线图（带面积填充）
function svgLineChart(data, labels, opts) {
  opts = opts || {};
  const w = opts.width || 560, h = opts.height || 220;
  const color = opts.color || '#4B3FE3';
  const gid = opts.gid || 'areaGrad' + Math.random().toString(36).slice(2, 7);
  const pad = { t: 20, r: 12, b: 30, l: 40 };
  const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;
  const max = Math.max.apply(null, data) * 1.12 || 1;
  const min = Math.min.apply(null, data) * 0.85;
  const range = (max - min) || 1;
  const step = cw / (data.length - 1);
  let points = [];
  data.forEach(function (v, i) {
    const x = pad.l + i * step;
    const y = pad.t + ch - ((v - min) / range) * ch;
    points.push([x, y]);
  });
  let linePath = 'M ' + points.map(function (p) { return p[0] + ',' + p[1]; }).join(' L ');
  let areaPath = linePath + ' L ' + points[points.length - 1][0] + ',' + (pad.t + ch) + ' L ' + points[0][0] + ',' + (pad.t + ch) + ' Z';
  let dots = '';
  points.forEach(function (p) {
    dots += '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="3" fill="white" stroke="' + color + '" stroke-width="2"/>';
  });
  let xLabels = '';
  const labelStep = Math.ceil(data.length / 8);
  labels.forEach(function (l, i) {
    if (i % labelStep === 0 || i === labels.length - 1) {
      const x = pad.l + i * step;
      xLabels += '<text x="' + x + '" y="' + (h - 10) + '" text-anchor="middle" font-size="9" fill="#8B8D9F">' + l + '</text>';
    }
  });
  let grid = '';
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (ch / 4) * i;
    grid += '<line x1="' + pad.l + '" y1="' + y + '" x2="' + (w - pad.r) + '" y2="' + y + '" stroke="#F0F1F5" stroke-width="1"/>';
    const val = Math.round(max - (max - min) / 4 * i);
    grid += '<text x="' + (pad.l - 6) + '" y="' + (y + 3) + '" text-anchor="end" font-size="9" fill="#8B8D9F">' + val + '</text>';
  }
  return '<svg viewBox="0 0 ' + w + ' ' + h + '" style="width:100%;height:auto;" xmlns="http://www.w3.org/2000/svg">' +
    '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0%" stop-color="' + color + '" stop-opacity="0.25"/>' +
    '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>' +
    grid + '<path d="' + areaPath + '" fill="url(#' + gid + ')"/>' +
    '<path d="' + linePath + '" fill="none" stroke="' + color + '" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>' +
    dots + xLabels + '</svg>';
}

// 饼图 / 环形图：segments = [{ label, value, color }]
function svgPieChart(segments, opts) {
  opts = opts || {};
  const w = opts.width || 180, h = opts.height || 180;
  const cx = w / 2, cy = h / 2;
  const r = opts.radius || Math.min(w, h) / 2 - 8;
  const innerR = opts.innerRadius || 0;
  const total = segments.reduce(function (s, seg) { return s + seg.value; }, 0) || 1;
  let currentAngle = -Math.PI / 2;
  let paths = '';
  segments.forEach(function (seg) {
    const angle = (seg.value / total) * Math.PI * 2;
    const endAngle = currentAngle + angle;
    const x1 = cx + r * Math.cos(currentAngle);
    const y1 = cy + r * Math.sin(currentAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const largeArc = angle > Math.PI ? 1 : 0;
    if (innerR > 0) {
      const ix1 = cx + innerR * Math.cos(currentAngle);
      const iy1 = cy + innerR * Math.sin(currentAngle);
      const ix2 = cx + innerR * Math.cos(endAngle);
      const iy2 = cy + innerR * Math.sin(endAngle);
      paths += '<path d="M ' + x1 + ',' + y1 + ' A ' + r + ',' + r + ' 0 ' + largeArc + ' 1 ' + x2 + ',' + y2 + ' L ' + ix2 + ',' + iy2 + ' A ' + innerR + ',' + innerR + ' 0 ' + largeArc + ' 0 ' + ix1 + ',' + iy1 + ' Z" fill="' + seg.color + '"/>';
    } else {
      paths += '<path d="M ' + cx + ',' + cy + ' L ' + x1 + ',' + y1 + ' A ' + r + ',' + r + ' 0 ' + largeArc + ' 1 ' + x2 + ',' + y2 + ' Z" fill="' + seg.color + '"/>';
    }
    currentAngle = endAngle;
  });
  return '<svg viewBox="0 0 ' + w + ' ' + h + '" style="width:100%;max-width:' + w + 'px;height:auto;" xmlns="http://www.w3.org/2000/svg">' + paths + '</svg>';
}

// 环形进度条（依赖 .health-score-ring svg 的旋转）
function svgRing(value, opts) {
  opts = opts || {};
  const size = opts.size || 140;
  const stroke = opts.stroke || 12;
  const color = opts.color || '#4B3FE3';
  const track = opts.track || '#F0F1F5';
  const r = (size - stroke) / 2;
  const circumference = 2 * Math.PI * r;
  const offset = circumference - (value / 100) * circumference;
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '">' +
    '<circle cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + r + '" fill="none" stroke="' + track + '" stroke-width="' + stroke + '"/>' +
    '<circle cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + r + '" fill="none" stroke="' + color + '" stroke-width="' + stroke + '" stroke-linecap="round" stroke-dasharray="' + circumference.toFixed(2) + '" stroke-dashoffset="' + offset.toFixed(2) + '"/>' +
    '</svg>';
}

// 统计卡片 HTML
function statCardHTML(cfg) {
  const trendIcon = cfg.trendDir === 'up' ? '↑' : '↓';
  return '<div class="stat-card">' +
    '<div class="flex items-center justify-between">' +
    '<div class="stat-card-icon" style="background:' + cfg.iconBg + ';color:' + cfg.iconColor + ';">' + Utils.icon(cfg.icon, 20) + '</div>' +
    '<span class="stat-card-trend ' + cfg.trendDir + '">' + trendIcon + ' ' + cfg.trend + '</span>' +
    '</div>' +
    '<div class="stat-card-value">' + cfg.value + '</div>' +
    '<div class="stat-card-label">' + cfg.label + '</div>' +
    '</div>';
}

// 状态指示灯
function statusDot(status) {
  const map = { ok: '#00B884', warn: '#FF9500', err: '#FF3B5C' };
  const labelMap = { ok: '运行中', warn: '警告', err: '异常' };
  return '<span class="flex items-center gap-2">' +
    '<span style="width:8px;height:8px;border-radius:50%;background:' + (map[status] || '#8B8D9F') + ';box-shadow:0 0 0 3px ' + (map[status] || '#8B8D9F') + '22;"></span>' +
    '<span class="text-sm">' + (labelMap[status] || '未知') + '</span>' +
    '</span>';
}

// 角色 badge 映射
function roleBadge(role) {
  const map = {
    '管理员': 'badge-danger',
    '知识管理员': 'badge-primary',
    '编辑者': 'badge-info',
    '查看者': 'badge-neutral',
  };
  return '<span class="badge ' + (map[role] || 'badge-neutral') + '">' + role + '</span>';
}

// ============================================================
// 页面 1：运营看板
// ============================================================
App.registerPage('admin', {
  title: '运营看板',
  render() {
    // 问答趋势 7 天数据
    const trendLabels = ['06-29', '06-30', '07-01', '07-02', '07-03', '07-04', '07-05'];
    const trendData = [156, 189, 134, 234, 267, 198, 212];

    // 热门知识库 top5（按文档数排序）
    const topKBs = Mock.knowledgeBases.slice().sort((a, b) => b.docs - a.docs).slice(0, 5);
    const maxDocs = topKBs[0].docs;

    // 最近活动
    const activities = [
      { user: '张明', avatar: '张', action: '发布了', target: '《2026年Q3产品路线图》', time: '2分钟前', type: 'success' },
      { user: '李华', avatar: '李', action: '提交审核', target: '《API接口设计规范 v3.0》', time: '15分钟前', type: 'warning' },
      { user: '陈静', avatar: '陈', action: '创建了知识库', target: '《IT运维手册》', time: '1小时前', type: 'success' },
      { user: '王芳', avatar: '王', action: '更新了', target: '《品牌视觉识别系统》', time: '2小时前', type: 'info' },
      { user: '孙莉', avatar: '孙', action: '回复了反馈', target: '「文档解析速度优化」', time: '3小时前', type: 'info' },
    ];

    // 系统状态
    const services = [
      { name: 'APISIX 网关', status: 'ok', detail: '99.98% 可用' },
      { name: 'Python 推理引擎', status: 'ok', detail: '响应 1.2s' },
      { name: 'Celery 任务队列', status: 'ok', detail: '0 积压' },
      { name: 'Milvus 向量库', status: 'warn', detail: '索引重建中' },
      { name: 'Neo4j 图数据库', status: 'ok', detail: '38K 节点' },
      { name: 'Redis 缓存', status: 'ok', detail: '命中率 94%' },
    ];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">运营看板</div>
            <div class="page-subtitle">企业知识库整体运营数据概览 · 更新于 2026-07-05 09:00</div>
          </div>
          <div class="flex gap-2">
            <button class="btn btn-secondary btn-sm" onclick="App.toast('数据已刷新','success')">${Utils.icon('refresh', 16)} 刷新数据</button>
            <button class="btn btn-secondary btn-sm" onclick="App.navigate('admin/reports')">${Utils.icon('download', 16)} 导出报表</button>
          </div>
        </div>
      </div>

      <!-- 顶部统计卡片 -->
      <div class="grid grid-4 gap-4 mb-6">
        ${statCardHTML({ label: '文档总数', value: '1,019', trend: '12%', trendDir: 'up', icon: 'doc', iconBg: '#EEEBFE', iconColor: '#4B3FE3' })}
        ${statCardHTML({ label: '活跃用户', value: '128', trend: '8%', trendDir: 'up', icon: 'users', iconBg: '#E0FAF0', iconColor: '#00B884' })}
        ${statCardHTML({ label: 'AI 问答', value: '8,542', trend: '23%', trendDir: 'up', icon: 'chat', iconBg: '#E3F2FD', iconColor: '#2196F3' })}
        ${statCardHTML({ label: '满意度', value: '92.5%', trend: '2.1%', trendDir: 'up', icon: 'star', iconBg: '#FFF5E6', iconColor: '#FF9500' })}
      </div>

      <!-- 中部双列 -->
      <div class="grid grid-2 gap-4 mb-6">
        <div class="card">
          <div class="card-header">
            <div class="card-title">问答趋势</div>
            <span class="badge badge-success">近 7 天</span>
          </div>
          <div class="card-body">
            ${svgBarChart(trendData, trendLabels, { color: '#4B3FE3' })}
          </div>
        </div>
        <div class="card">
          <div class="card-header">
            <div class="card-title">热门知识库 Top 5</div>
            <a href="#manage/kb" class="btn-link">查看全部</a>
          </div>
          <div class="card-body">
            ${topKBs.map((kb, i) => `
              <div class="flex items-center gap-3 mb-3">
                <span class="text-muted text-sm" style="width:18px;font-weight:600;">${i + 1}</span>
                <span style="font-size:20px;">${kb.icon}</span>
                <div class="flex-1">
                  <div class="flex items-center justify-between mb-1">
                    <span class="text-sm font-medium">${kb.name}</span>
                    <span class="text-xs text-muted">${kb.docs} 文档 · ${Math.round(kb.docs * 3.2)} 访问</span>
                  </div>
                  <div class="progress-bar"><div class="progress-bar-fill primary" style="width:${(kb.docs / maxDocs * 100).toFixed(0)}%"></div></div>
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      </div>

      <!-- 底部最近活动 + 系统状态 -->
      <div class="grid grid-2 gap-4">
        <div class="card">
          <div class="card-header">
            <div class="card-title">最近活动</div>
            <a href="#knowledge/timeline" class="btn-link">查看时间线</a>
          </div>
          <div class="card-body" style="padding:8px 20px;">
            ${activities.map(a => {
              const colorMap = { success: '#00B884', warning: '#FF9500', info: '#2196F3', danger: '#FF3B5C' };
              return `
                <div class="flex items-center gap-3" style="padding:10px 0;border-bottom:1px solid var(--border-light);">
                  <div class="avatar avatar-sm" style="background:${Utils.avatarColor(a.user)}">${a.avatar}</div>
                  <div class="flex-1 text-sm">
                    <span class="font-medium">${a.user}</span>
                    <span class="text-muted"> ${a.action} </span>
                    <span class="text-primary">${a.target}</span>
                  </div>
                  <span class="text-xs text-muted">${a.time}</span>
                </div>
              `;
            }).join('')}
          </div>
        </div>
        <div class="card">
          <div class="card-header">
            <div class="card-title">系统状态</div>
            <span class="badge badge-success">全部正常</span>
          </div>
          <div class="card-body">
            <div class="grid grid-2 gap-4">
              ${services.map(s => `
                <div class="flex items-center justify-between" style="padding:8px 0;">
                  <div>
                    <div class="text-sm font-medium">${s.name}</div>
                    <div class="text-xs text-muted">${s.detail}</div>
                  </div>
                  ${statusDot(s.status)}
                </div>
              `).join('')}
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {},
});

// ============================================================
// 页面 2：知识健康度看板
// ============================================================
App.registerPage('admin/health', {
  title: '知识健康度',
  render() {
    const score = 87.3;
    const grade = score >= 85 ? '良好' : (score >= 70 ? '一般' : '需改进');
    const gradeColor = score >= 85 ? '#00B884' : (score >= 70 ? '#FF9500' : '#FF3B5C');

    // 健康度指标
    const metrics = Mock.healthMetrics;
    const statusBadgeMap = { success: 'badge-success', warning: 'badge-warning', danger: 'badge-danger' };
    const statusTextMap = { success: '达标', warning: '待提升', danger: '未达标' };
    const barColorMap = { success: 'success', warning: 'warning', danger: 'danger' };

    // 未达标指标 → 改进建议
    const suggestions = [
      { metric: '知识覆盖率', current: '87.3%', target: '90%', advice: '当前存在 6 个高频搜索缺口，建议优先补充「AI Agent 开发实战」「多租户架构设计」等文档，预计可提升覆盖率至 91%。' },
      { metric: '引用准确率', current: '94.8%', target: '95%', advice: 'RAG 检索召回率略有不足，建议调整 Embedding 模型为 bge-m3 并优化 chunk 分块策略，重算知识库向量索引。' },
      { metric: '用户活跃度', current: '78.5%', target: '80%', advice: '本周活跃用户环比下降，建议在新人 Onboarding 流程中嵌入知识库引导，并推送个性化内容推荐。' },
    ];

    // 知识新鲜度分布（饼图）
    const freshness = [
      { label: '1 周内', value: 186, color: '#4B3FE3' },
      { label: '1 月内', value: 342, color: '#6B5FF7' },
      { label: '3 月内', value: 268, color: '#2196F3' },
      { label: '半年内', value: 143, color: '#FF9500' },
      { label: '1 年以上', value: 80, color: '#FF3B5C' },
    ];
    const freshTotal = freshness.reduce((s, f) => s + f.value, 0);

    return `
      <div class="page-header">
        <div class="page-title">知识健康度</div>
        <div class="page-subtitle">多维度评估知识库质量与活跃度 · 2026 年 7 月</div>
      </div>

      <div class="grid grid-2 gap-4 mb-6">
        <!-- 左侧评分环 -->
        <div class="card">
          <div class="card-header"><div class="card-title">综合健康度评分</div></div>
          <div class="card-body flex items-center gap-6" style="justify-content:center;">
            <div class="health-score-ring">
              ${svgRing(score, { size: 140, stroke: 12, color: gradeColor })}
              <div class="health-score-ring-value" style="color:${gradeColor}">${score}</div>
            </div>
            <div>
              <div class="flex items-center gap-2 mb-2">
                <span class="badge" style="background:${gradeColor}1a;color:${gradeColor};">${grade}</span>
                <span class="text-sm text-muted">综合评级</span>
              </div>
              <div class="text-sm text-secondary" style="line-height:1.7;">
                较上月 <span class="text-success font-medium">↑ 2.1%</span><br>
                知识覆盖率、引用准确率仍存在提升空间
              </div>
              <button class="btn btn-secondary btn-sm mt-3" onclick="App.navigate('manage/gaps')">${Utils.icon('alert', 16)} 查看知识缺口</button>
            </div>
          </div>
        </div>

        <!-- 右侧指标列表 -->
        <div class="card">
          <div class="card-header"><div class="card-title">健康度指标</div><span class="text-xs text-muted">共 6 项</span></div>
          <div class="card-body" style="padding:8px 20px;">
            ${metrics.map(m => `
              <div class="health-metric">
                <div style="flex:1;">
                  <div class="flex items-center justify-between mb-1">
                    <span class="text-sm font-medium">${m.name}</span>
                    <div class="flex items-center gap-2">
                      <span class="font-semibold">${m.value}%</span>
                      <span class="text-xs text-muted">/ 目标 ${m.target}%</span>
                      <span class="badge ${statusBadgeMap[m.status]}">${statusTextMap[m.status]}</span>
                    </div>
                  </div>
                  <div class="progress-bar"><div class="progress-bar-fill ${barColorMap[m.status]}" style="width:${m.value}%"></div></div>
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      </div>

      <!-- 改进建议 + 新鲜度分布 -->
      <div class="grid grid-2 gap-4">
        <div class="card">
          <div class="card-header"><div class="card-title">改进建议</div><span class="badge badge-warning">3 项待优化</span></div>
          <div class="card-body">
            ${suggestions.map((s, i) => `
              <div class="flex items-start gap-3 mb-4" style="${i < suggestions.length - 1 ? 'border-bottom:1px solid var(--border-light);padding-bottom:16px;' : ''}">
                <div style="width:28px;height:28px;border-radius:50%;background:#FFF5E6;color:#FF9500;display:flex;align-items:center;justify-content:center;font-weight:600;flex-shrink:0;">${i + 1}</div>
                <div>
                  <div class="text-sm font-medium mb-1">${s.metric} <span class="text-muted">（${s.current} / ${s.target}）</span></div>
                  <div class="text-sm text-secondary" style="line-height:1.6;">${s.advice}</div>
                </div>
              </div>
            `).join('')}
          </div>
        </div>

        <div class="card">
          <div class="card-header"><div class="card-title">知识新鲜度分布</div><span class="text-xs text-muted">共 ${freshTotal} 篇文档</span></div>
          <div class="card-body flex items-center gap-6">
            <div style="width:160px;flex-shrink:0;">${svgPieChart(freshness, { width: 160, height: 160, innerRadius: 45 })}</div>
            <div class="flex-1">
              ${freshness.map(f => `
                <div class="flex items-center justify-between mb-2">
                  <div class="flex items-center gap-2">
                    <span style="width:10px;height:10px;border-radius:2px;background:${f.color};"></span>
                    <span class="text-sm">${f.label}</span>
                  </div>
                  <div class="text-sm">
                    <span class="font-medium">${f.value}</span>
                    <span class="text-muted text-xs"> (${(f.value / freshTotal * 100).toFixed(1)}%)</span>
                  </div>
                </div>
              `).join('')}
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {},
});

// ============================================================
// 页面 3：审核工作流
// ============================================================
App.registerPage('admin/audit', {
  title: '审核工作流',
  render() {
    const items = Mock.auditItems;
    const counts = {
      pending: items.filter(i => i.status === 'pending').length,
      approved: items.filter(i => i.status === 'approved').length,
      rejected: items.filter(i => i.status === 'rejected').length,
      all: items.length,
    };
    const typeBadge = { new: 'badge-primary', update: 'badge-info' };
    const typeText = { new: '新建', update: '更新' };
    const statusBadge = { pending: 'badge-warning', approved: 'badge-success', rejected: 'badge-danger' };
    const statusText = { pending: '待审核', approved: '已通过', rejected: '已驳回' };

    // 审核流程步骤（以第一篇待审核为例）
    const steps = [
      { title: '提交', desc: '李华 提交文档', icon: 'upload', state: 'done' },
      { title: '初审', desc: '张明 审核中', icon: 'eye', state: 'current' },
      { title: '复审', desc: '等待复审', icon: 'check', state: 'pending' },
      { title: '发布', desc: '审核通过后发布', icon: 'send', state: 'pending' },
    ];
    const stepIconMap = { done: 'done', current: 'current', pending: 'pending' };

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">审核工作流</div>
            <div class="page-subtitle">管理文档发布前的审核流程</div>
          </div>
          <button class="btn btn-secondary btn-sm" onclick="App.toast('审核规则设置（原型演示）','info')">${Utils.icon('settings', 16)} 审核规则</button>
        </div>
      </div>

      <div class="grid gap-4" style="grid-template-columns:1fr 300px;">
        <div>
          <!-- 标签页 -->
          <div class="tabs mb-4">
            <div class="tab active" data-filter="pending">待审核 <span class="badge badge-warning" style="margin-left:4px;">${counts.pending}</span></div>
            <div class="tab" data-filter="approved">已通过 <span class="badge badge-success" style="margin-left:4px;">${counts.approved}</span></div>
            <div class="tab" data-filter="rejected">已驳回 <span class="badge badge-danger" style="margin-left:4px;">${counts.rejected}</span></div>
            <div class="tab" data-filter="all">全部 <span class="badge badge-neutral" style="margin-left:4px;">${counts.all}</span></div>
          </div>

          <!-- 审核列表 -->
          <div class="table-wrap">
            <table class="data-table">
              <thead>
                <tr>
                  <th>文档标题</th>
                  <th>提交人</th>
                  <th>提交时间</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>审核人</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                ${items.map(item => `
                  <tr data-status="${item.status}">
                    <td><a href="#knowledge/doc" class="font-medium">${item.doc}</a></td>
                    <td>
                      <div class="flex items-center gap-2">
                        <div class="avatar avatar-sm" style="background:${Utils.avatarColor(item.submitter)}">${item.submitter.charAt(0)}</div>
                        <span>${item.submitter}</span>
                      </div>
                    </td>
                    <td class="text-muted">${item.submitTime}</td>
                    <td><span class="badge ${typeBadge[item.type]}">${typeText[item.type]}</span></td>
                    <td><span class="badge ${statusBadge[item.status]}">${statusText[item.status]}</span></td>
                    <td class="text-muted">${item.reviewer || '—'} ${item.reviewTime ? '<div class="text-xs">' + item.reviewTime + '</div>' : ''}</td>
                    <td>
                      ${item.status === 'pending' ? `
                        <button class="btn btn-success btn-sm" onclick="AdminAudit.review(${item.id},'approve')">${Utils.icon('check', 14)} 通过</button>
                        <button class="btn btn-danger btn-sm" onclick="AdminAudit.review(${item.id},'reject')">${Utils.icon('close', 14)} 驳回</button>
                      ` : `
                        <button class="btn btn-ghost btn-sm" onclick="App.toast('查看审核详情（原型演示）','info')">${Utils.icon('eye', 14)} 详情</button>
                      `}
                      ${item.reason ? '<div class="text-xs text-danger mt-1">驳回原因：' + item.reason + '</div>' : ''}
                    </td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>

        <!-- 右侧审核流程面板 -->
        <div class="card">
          <div class="card-header"><div class="card-title">审核流程</div></div>
          <div class="card-body">
            <div class="text-sm text-muted mb-4">当前审核：API接口设计规范 v3.0</div>
            ${steps.map(s => `
              <div class="audit-step">
                <div class="audit-step-icon ${stepIconMap[s.state]}">
                  ${s.state === 'done' ? Utils.icon('check', 16) : (s.state === 'current' ? Utils.icon('eye', 16) : '')}
                </div>
                <div class="audit-step-content">
                  <div class="audit-step-title">${s.title}</div>
                  <div class="audit-step-desc">${s.desc}</div>
                </div>
              </div>
            `).join('')}

            <div class="divider"></div>
            <div class="text-sm font-medium mb-3">审核统计</div>
            <div class="flex items-center justify-between mb-2">
              <span class="text-sm text-muted">本月通过率</span>
              <span class="font-semibold text-success">94.2%</span>
            </div>
            <div class="progress-bar mb-3"><div class="progress-bar-fill success" style="width:94%"></div></div>
            <div class="flex items-center justify-between text-sm">
              <span class="text-muted">平均审核时长</span>
              <span class="font-medium">2.3 小时</span>
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {
    // 标签页筛选
    document.querySelectorAll('.tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.tabs .tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const filter = tab.dataset.filter;
        document.querySelectorAll('tbody tr').forEach(tr => {
          if (filter === 'all' || tr.dataset.status === filter) {
            tr.style.display = '';
          } else {
            tr.style.display = 'none';
          }
        });
      });
    });
  },
});

// 审核操作（全局函数，供 onclick 调用）
window.AdminAudit = {
  review(id, action) {
    const isApprove = action === 'approve';
    App.modal({
      title: isApprove ? '确认通过审核' : '驳回审核',
      body: `
        <div class="form-group">
          <label class="form-label">${isApprove ? '审核意见' : '驳回原因'} <span class="required">*</span></label>
          <textarea class="form-textarea" id="audit-comment" placeholder="${isApprove ? '请输入审核意见（可选）' : '请说明驳回原因'}">${isApprove ? '内容完整，符合发布标准，同意发布。' : ''}</textarea>
        </div>
        ${!isApprove ? '<div class="form-hint">驳回原因将通过站内信通知提交人</div>' : ''}
        <div class="flex items-center gap-2 p-3 rounded" style="background:var(--surface-muted);">
          ${Utils.icon('fileText', 18)}
          <span class="text-sm">${Mock.auditItems.find(i => i.id === id).doc}</span>
        </div>
      `,
      confirmText: isApprove ? '确认通过' : '确认驳回',
      onConfirm(overlay) {
        const comment = overlay.querySelector('#audit-comment').value;
        overlay.remove();
        App.toast(isApprove ? '已通过审核，文档已发布' : '已驳回审核，已通知提交人', isApprove ? 'success' : 'warning');
      },
    });
  },
};

// ============================================================
// 页面 4：用户与权限
// ============================================================
App.registerPage('admin/users', {
  title: '用户与权限',
  render() {
    const users = Mock.users;
    const statusMap = { active: 'badge-success', inactive: 'badge-neutral' };
    const statusText = { active: '在线', inactive: '离线' };

    // 角色权限矩阵
    const roles = ['管理员', '知识管理员', '编辑者', '查看者'];
    const permissions = ['查看', '编辑', '删除', '审核', '管理用户', '系统设置'];
    // 权限矩阵：1=有，0=无
    const matrix = {
      '管理员': [1, 1, 1, 1, 1, 1],
      '知识管理员': [1, 1, 1, 1, 0, 0],
      '编辑者': [1, 1, 0, 0, 0, 0],
      '查看者': [1, 0, 0, 0, 0, 0],
    };

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">用户与权限</div>
            <div class="page-subtitle">管理平台用户、角色与权限分配</div>
          </div>
          <button class="btn btn-primary btn-sm" onclick="AdminUsers.showAdd()">${Utils.icon('plus', 16)} 添加用户</button>
        </div>
      </div>

      <!-- 统计卡片 -->
      <div class="grid grid-4 gap-4 mb-6">
        ${statCardHTML({ label: '总用户数', value: '128', trend: '5', trendDir: 'up', icon: 'users', iconBg: '#EEEBFE', iconColor: '#4B3FE3' })}
        ${statCardHTML({ label: '活跃用户', value: '96', trend: '8%', trendDir: 'up', icon: 'zap', iconBg: '#E0FAF0', iconColor: '#00B884' })}
        ${statCardHTML({ label: '管理员', value: '3', trend: '0', trendDir: 'up', icon: 'shield', iconBg: '#FFE5EB', iconColor: '#FF3B5C' })}
        ${statCardHTML({ label: '本周新增', value: '5', trend: '25%', trendDir: 'up', icon: 'trending', iconBg: '#FFF5E6', iconColor: '#FF9500' })}
      </div>

      <!-- 用户表格 -->
      <div class="card mb-6">
        <div class="card-header">
          <div class="card-title">用户列表</div>
          <div class="flex items-center gap-2">
            <div class="topbar-search" style="width:200px;">
              <span class="topbar-search-icon">${Utils.icon('search', 16)}</span>
              <input type="text" placeholder="搜索用户..." class="form-input" style="height:32px;padding-left:32px;">
            </div>
          </div>
        </div>
        <div class="table-wrap" style="border:none;">
          <table class="data-table">
            <thead>
              <tr>
                <th>用户</th>
                <th>角色</th>
                <th>部门</th>
                <th>状态</th>
                <th>最后活跃</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              ${users.map(u => `
                <tr>
                  <td>
                    <div class="flex items-center gap-3">
                      <div class="avatar avatar-md" style="background:${Utils.avatarColor(u.name)}">${u.avatar}</div>
                      <div>
                        <div class="font-medium">${u.name}</div>
                        <div class="text-xs text-muted">${u.name.toLowerCase().replace(' ', '.')}@company.com</div>
                      </div>
                    </div>
                  </td>
                  <td>${roleBadge(u.role)}</td>
                  <td class="text-muted">${u.dept}</td>
                  <td><span class="badge ${statusMap[u.status]}">${statusText[u.status]}</span></td>
                  <td class="text-muted">${u.lastActive}</td>
                  <td>
                    <button class="btn btn-ghost btn-sm btn-icon" onclick="AdminUsers.showEdit('${u.name}','${u.role}','${u.dept}')" title="编辑">${Utils.icon('edit', 16)}</button>
                    <button class="btn btn-ghost btn-sm btn-icon" onclick="AdminUsers.del('${u.name}')" title="删除" style="color:var(--danger);">${Utils.icon('trash', 16)}</button>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>

      <!-- 角色权限矩阵 -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">角色权限矩阵</div>
          <span class="text-xs text-muted">4 种角色 × 6 项权限</span>
        </div>
        <div class="table-wrap" style="border:none;">
          <table class="data-table">
            <thead>
              <tr>
                <th>权限 / 角色</th>
                ${roles.map(r => `<th class="text-center">${r}</th>`).join('')}
              </tr>
            </thead>
            <tbody>
              ${permissions.map((p, pi) => `
                <tr>
                  <td class="font-medium">${p}</td>
                  ${roles.map(r => {
                    const has = matrix[r][pi];
                    return `<td class="text-center">${has ? '<span style="color:#00B884;">' + Utils.icon('check', 18) + '</span>' : '<span class="text-muted">—</span>'}</td>`;
                  }).join('')}
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>
    `;
  },
  init() {},
});

// 用户管理操作
window.AdminUsers = {
  showAdd() {
    App.modal({
      title: '添加用户',
      body: `
        <div class="form-group">
          <label class="form-label">姓名 <span class="required">*</span></label>
          <input class="form-input" id="user-name" placeholder="请输入姓名">
        </div>
        <div class="form-group">
          <label class="form-label">邮箱 <span class="required">*</span></label>
          <input class="form-input" id="user-email" type="email" placeholder="name@company.com">
        </div>
        <div class="grid grid-2 gap-4">
          <div class="form-group">
            <label class="form-label">部门</label>
            <select class="form-select" id="user-dept">
              <option>产品中心</option><option>研发部</option><option>市场部</option><option>财务部</option><option>人力资源</option><option>IT部</option>
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">角色</label>
            <select class="form-select" id="user-role">
              <option>查看者</option><option>编辑者</option><option>知识管理员</option><option>管理员</option>
            </select>
          </div>
        </div>
      `,
      confirmText: '添加用户',
      onConfirm(overlay) {
        const name = overlay.querySelector('#user-name').value.trim();
        if (!name) { App.toast('请输入姓名', 'warning'); return; }
        overlay.remove();
        App.toast('用户「' + name + '」已添加', 'success');
      },
    });
  },
  showEdit(name, role, dept) {
    App.modal({
      title: '编辑用户',
      body: `
        <div class="form-group">
          <label class="form-label">姓名</label>
          <input class="form-input" id="user-name" value="${name}">
        </div>
        <div class="form-group">
          <label class="form-label">邮箱</label>
          <input class="form-input" value="${name.toLowerCase()}@company.com">
        </div>
        <div class="grid grid-2 gap-4">
          <div class="form-group">
            <label class="form-label">部门</label>
            <select class="form-select" id="user-dept">
              ${['产品中心', '研发部', '市场部', '财务部', '人力资源', 'IT部'].map(d => '<option ' + (d === dept ? 'selected' : '') + '>' + d + '</option>').join('')}
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">角色</label>
            <select class="form-select" id="user-role">
              ${['查看者', '编辑者', '知识管理员', '管理员'].map(r => '<option ' + (r === role ? 'selected' : '') + '>' + r + '</option>').join('')}
            </select>
          </div>
        </div>
      `,
      confirmText: '保存修改',
      onConfirm(overlay) {
        overlay.remove();
        App.toast('用户信息已更新', 'success');
      },
    });
  },
  del(name) {
    App.modal({
      title: '确认删除用户',
      body: '<div class="flex items-center gap-3 p-4 rounded" style="background:var(--danger-bg);"><span style="font-size:24px;">⚠️</span><div><div class="font-medium">删除用户「' + name + '」</div><div class="text-sm text-muted">删除后该用户将无法登录，此操作不可撤销</div></div></div>',
      confirmText: '确认删除',
      onConfirm(overlay) {
        overlay.remove();
        App.toast('用户已删除', 'warning');
      },
    });
  },
};

// ============================================================
// 页面 5：标签管理
// ============================================================
App.registerPage('admin/tags', {
  title: '标签管理',
  render() {
    const tags = Mock.tags;
    const maxCount = Math.max.apply(null, tags.map(t => t.count));
    const colorMap = { primary: '#4B3FE3', info: '#2196F3', success: '#00B884', warning: '#FF9500', danger: '#FF3B5C', neutral: '#8B8D9F' };

    // 标签分类
    const categories = [
      { name: '产品', count: 3, tags: ['产品规划', '客户案例', '品牌设计'] },
      { name: '技术', count: 4, tags: ['技术方案', 'API规范', '架构设计', '运维SOP'] },
      { name: '人力', count: 2, tags: ['人力资源', '入职指南'] },
      { name: '财务', count: 1, tags: ['财务制度'] },
    ];

    // 标签使用趋势（6 个月）
    const trendLabels = ['2月', '3月', '4月', '5月', '6月', '7月'];
    const trendData = [45, 62, 78, 95, 112, 128];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">标签管理</div>
            <div class="page-subtitle">管理知识库标签体系与分类</div>
          </div>
          <button class="btn btn-primary btn-sm" onclick="AdminTags.showCreate()">${Utils.icon('plus', 16)} 新建标签</button>
        </div>
      </div>

      <div class="grid gap-4" style="grid-template-columns:1fr 300px;">
        <div>
          <!-- 标签云 -->
          <div class="card mb-4">
            <div class="card-header"><div class="card-title">标签云</div><span class="text-xs text-muted">${tags.length} 个标签</span></div>
            <div class="card-body flex flex-wrap gap-2" style="align-items:center;">
              ${tags.map(t => {
                const size = 12 + (t.count / maxCount) * 10;
                const c = colorMap[t.color] || '#4B3FE3';
                return `<span class="tag" style="font-size:${size}px;background:${c}1a;color:${c};" onclick="App.toast('标签：${t.name}（${t.count} 篇文档）','info')">${t.name} <span class="text-xs">${t.count}</span></span>`;
              }).join('')}
            </div>
          </div>

          <!-- 标签表格 -->
          <div class="card">
            <div class="card-header"><div class="card-title">标签列表</div></div>
            <div class="table-wrap" style="border:none;">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>标签名</th>
                    <th>使用文档数</th>
                    <th>创建时间</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  ${tags.map(t => `
                    <tr>
                      <td>
                        <div class="flex items-center gap-2">
                          <span style="width:8px;height:8px;border-radius:50%;background:${colorMap[t.color] || '#4B3FE3'};"></span>
                          <span class="font-medium">${t.name}</span>
                        </div>
                      </td>
                      <td><span class="badge badge-neutral">${t.count} 篇</span></td>
                      <td class="text-muted">2026-0${(t.count % 6) + 1}-1${t.count % 9}</td>
                      <td>
                        <button class="btn btn-ghost btn-sm btn-icon" onclick="App.toast('编辑标签（原型演示）','info')" title="编辑">${Utils.icon('edit', 16)}</button>
                        <button class="btn btn-ghost btn-sm btn-icon" onclick="App.toast('合并标签（原型演示）','info')" title="合并">${Utils.icon('layers', 16)}</button>
                        <button class="btn btn-ghost btn-sm btn-icon" onclick="App.toast('已删除标签','warning')" title="删除" style="color:var(--danger);">${Utils.icon('trash', 16)}</button>
                      </td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <!-- 标签使用趋势 -->
          <div class="card mt-4">
            <div class="card-header"><div class="card-title">标签使用趋势</div><span class="badge badge-success">近 6 个月</span></div>
            <div class="card-body">${svgBarChart(trendData, trendLabels, { color: '#00B884' })}</div>
          </div>
        </div>

        <!-- 右侧标签分类 -->
        <div class="card">
          <div class="card-header"><div class="card-title">标签分类</div></div>
          <div class="card-body">
            ${categories.map(cat => `
              <div class="mb-4">
                <div class="flex items-center justify-between mb-2">
                  <span class="font-medium text-sm">${cat.name}</span>
                  <span class="badge badge-neutral">${cat.count}</span>
                </div>
                <div class="flex flex-wrap gap-1">
                  ${cat.tags.map(t => '<span class="tag" style="font-size:11px;">' + t + '</span>').join('')}
                </div>
              </div>
            `).join('')}
            <div class="divider"></div>
            <button class="btn btn-secondary btn-sm btn-block" onclick="App.toast('新建分类（原型演示）','info')">${Utils.icon('plus', 16)} 新建分类</button>
          </div>
        </div>
      </div>
    `;
  },
  init() {},
});

// 标签管理操作
window.AdminTags = {
  showCreate() {
    const colors = ['#4B3FE3', '#2196F3', '#00B884', '#FF9500', '#FF3B5C', '#9C27B0'];
    App.modal({
      title: '新建标签',
      body: `
        <div class="form-group">
          <label class="form-label">标签名称 <span class="required">*</span></label>
          <input class="form-input" id="tag-name" placeholder="请输入标签名称">
        </div>
        <div class="form-group">
          <label class="form-label">标签颜色</label>
          <div class="flex gap-2" id="tag-colors">
            ${colors.map((c, i) => '<span data-color="' + c + '" style="width:28px;height:28px;border-radius:50%;background:' + c + ';cursor:pointer;' + (i === 0 ? 'box-shadow:0 0 0 3px ' + c + '44;' : '') + '"></span>').join('')}
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">所属分类</label>
          <select class="form-select" id="tag-category">
            <option>产品</option><option>技术</option><option>人力</option><option>财务</option><option>运维</option>
          </select>
        </div>
      `,
      confirmText: '创建标签',
      onMount(overlay) {
        overlay.querySelectorAll('#tag-colors span').forEach(s => {
          s.addEventListener('click', () => {
            overlay.querySelectorAll('#tag-colors span').forEach(x => x.style.boxShadow = '');
            s.style.boxShadow = '0 0 0 3px ' + s.dataset.color + '44';
          });
        });
      },
      onConfirm(overlay) {
        const name = overlay.querySelector('#tag-name').value.trim();
        if (!name) { App.toast('请输入标签名称', 'warning'); return; }
        overlay.remove();
        App.toast('标签「' + name + '」已创建', 'success');
      },
    });
  },
};

// ============================================================
// 页面 6：使用报表
// ============================================================
App.registerPage('admin/reports', {
  title: '使用报表',
  render() {
    // API 调用趋势（30 天，采样 15 个点）
    const days = [];
    const calls = [];
    for (let i = 29; i >= 0; i -= 2) {
      const d = new Date(2026, 5, 5 + (29 - i) + 1);
      days.push((d.getMonth() + 1) + '/' + d.getDate());
      calls.push(280 + Math.round(Math.random() * 180) + (29 - i) * 4);
    }

    // Token 消耗分布
    const tokenDist = [
      { label: 'GPT-4o', value: 45.2, color: '#4B3FE3' },
      { label: 'Claude-3.5', value: 23.8, color: '#00B884' },
      { label: 'Qwen-VL', value: 12.1, color: '#FF9500' },
      { label: 'DeepSeek-V3', value: 4.9, color: '#2196F3' },
    ];
    const tokenTotal = tokenDist.reduce((s, t) => s + t.value, 0);

    // 用户活跃度排行
    const userRanking = [
      { rank: 1, name: '张明', avatar: '张', queries: 342, docs: 28, last: '在线' },
      { rank: 2, name: '李华', avatar: '李', queries: 289, docs: 35, last: '2分钟前' },
      { rank: 3, name: '陈静', avatar: '陈', queries: 234, docs: 19, last: '在线' },
      { rank: 4, name: '王芳', avatar: '王', queries: 198, docs: 22, last: '1小时前' },
      { rank: 5, name: '孙莉', avatar: '孙', queries: 167, docs: 15, last: '5分钟前' },
    ];

    // 知识库访问排行
    const kbRanking = [
      { rank: 1, name: '产品研发知识库', icon: '🚀', visits: 4523, docs: 326, growth: '+18%' },
      { rank: 2, name: 'IT运维手册', icon: '⚙️', visits: 3210, docs: 203, growth: '+12%' },
      { rank: 3, name: '人力资源制度库', icon: '👥', visits: 2876, docs: 156, growth: '+8%' },
      { rank: 4, name: '市场营销素材库', icon: '📊', visits: 2345, docs: 178, growth: '+15%' },
      { rank: 5, name: '客户成功案例库', icon: '🎯', visits: 1892, docs: 89, growth: '+22%' },
    ];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">使用报表</div>
            <div class="page-subtitle">平台资源消耗与使用情况统计</div>
          </div>
          <div class="flex items-center gap-2">
            <div class="tabs-pill" id="report-range">
              <div class="tab-pill" data-range="today">今天</div>
              <div class="tab-pill active" data-range="week">本周</div>
              <div class="tab-pill" data-range="month">本月</div>
              <div class="tab-pill" data-range="custom">自定义</div>
            </div>
            <button class="btn btn-primary btn-sm" onclick="App.toast('报表已导出为 Excel','success')">${Utils.icon('download', 16)} 导出报表</button>
          </div>
        </div>
      </div>

      <!-- 统计卡片 -->
      <div class="grid grid-4 gap-4 mb-6">
        ${statCardHTML({ label: 'API 调用次数', value: '12,345', trend: '15%', trendDir: 'up', icon: 'zap', iconBg: '#EEEBFE', iconColor: '#4B3FE3' })}
        ${statCardHTML({ label: 'Token 消耗', value: '856K', trend: '23%', trendDir: 'up', icon: 'cpu', iconBg: '#E3F2FD', iconColor: '#2196F3' })}
        ${statCardHTML({ label: '存储用量', value: '45.2GB', trend: '5%', trendDir: 'up', icon: 'database', iconBg: '#FFF5E6', iconColor: '#FF9500' })}
        ${statCardHTML({ label: '月度费用', value: '¥3,285', trend: '8%', trendDir: 'up', icon: 'trending', iconBg: '#E0FAF0', iconColor: '#00B884' })}
      </div>

      <!-- 图表区 -->
      <div class="grid grid-2 gap-4 mb-6">
        <div class="card">
          <div class="card-header"><div class="card-title">API 调用趋势</div><span class="text-xs text-muted">近 30 天</span></div>
          <div class="card-body">${svgLineChart(calls, days, { color: '#4B3FE3' })}</div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">Token 消耗分布</div><span class="text-xs text-muted">按 LLM 提供商</span></div>
          <div class="card-body flex items-center gap-6">
            <div style="width:160px;flex-shrink:0;position:relative;">
              ${svgPieChart(tokenDist, { width: 160, height: 160, innerRadius: 50 })}
              <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;">
                <div class="text-2xl font-bold">${tokenTotal.toFixed(1)}K</div>
                <div class="text-xs text-muted">总消耗</div>
              </div>
            </div>
            <div class="flex-1">
              ${tokenDist.map(t => `
                <div class="flex items-center justify-between mb-2">
                  <div class="flex items-center gap-2">
                    <span style="width:10px;height:10px;border-radius:2px;background:${t.color};"></span>
                    <span class="text-sm">${t.label}</span>
                  </div>
                  <div class="text-sm"><span class="font-medium">${t.value}K</span> <span class="text-muted text-xs">(${(t.value / tokenTotal * 100).toFixed(1)}%)</span></div>
                </div>
              `).join('')}
            </div>
          </div>
        </div>
      </div>

      <!-- 排行表格 -->
      <div class="grid grid-2 gap-4">
        <div class="card">
          <div class="card-header"><div class="card-title">用户活跃度排行</div><span class="badge badge-primary">Top 5</span></div>
          <div class="table-wrap" style="border:none;">
            <table class="data-table">
              <thead><tr><th>排名</th><th>用户</th><th>问答次数</th><th>文档贡献</th><th>最后活跃</th></tr></thead>
              <tbody>
                ${userRanking.map(u => `
                  <tr>
                    <td><span class="font-bold ${u.rank <= 3 ? 'text-primary' : 'text-muted'}">${u.rank}</span></td>
                    <td><div class="flex items-center gap-2"><div class="avatar avatar-sm" style="background:${Utils.avatarColor(u.name)}">${u.avatar}</div><span>${u.name}</span></div></td>
                    <td class="font-medium">${u.queries}</td>
                    <td>${u.docs} 篇</td>
                    <td class="text-muted">${u.last}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">知识库访问排行</div><span class="badge badge-success">Top 5</span></div>
          <div class="table-wrap" style="border:none;">
            <table class="data-table">
              <thead><tr><th>排名</th><th>知识库</th><th>访问量</th><th>文档数</th><th>增长率</th></tr></thead>
              <tbody>
                ${kbRanking.map(k => `
                  <tr>
                    <td><span class="font-bold ${k.rank <= 3 ? 'text-primary' : 'text-muted'}">${k.rank}</span></td>
                    <td><div class="flex items-center gap-2"><span>${k.icon}</span><span>${k.name}</span></div></td>
                    <td class="font-medium">${k.visits.toLocaleString()}</td>
                    <td>${k.docs}</td>
                    <td><span class="badge badge-success">${k.growth}</span></td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  },
  init() {
    // 时间范围切换
    document.querySelectorAll('#report-range .tab-pill').forEach(pill => {
      pill.addEventListener('click', () => {
        document.querySelectorAll('#report-range .tab-pill').forEach(p => p.classList.remove('active'));
        pill.classList.add('active');
        App.toast('已切换至「' + pill.textContent + '」数据范围', 'info');
      });
    });
  },
});

// ============================================================
// 页面 7：反馈管理
// ============================================================
App.registerPage('admin/feedback', {
  title: '反馈管理',
  render() {
    const feedbacks = Mock.feedbacks;
    const typeBadge = { bug: 'badge-danger', feature: 'badge-primary', improvement: 'badge-info' };
    const typeText = { bug: 'Bug', feature: '功能建议', improvement: '改进' };
    const statusBadge = { open: 'badge-warning', planned: 'badge-info', resolved: 'badge-success' };
    const statusText = { open: '待处理', planned: '已规划', resolved: '已解决' };
    const priorityBadge = { high: 'badge-danger', medium: 'badge-warning', low: 'badge-neutral' };
    const priorityText = { high: '高优', medium: '中优', low: '低优' };

    const counts = {
      all: feedbacks.length,
      bug: feedbacks.filter(f => f.type === 'bug').length,
      feature: feedbacks.filter(f => f.type === 'feature').length,
      improvement: feedbacks.filter(f => f.type === 'improvement').length,
    };

    // 反馈趋势（近月）
    const trendLabels = ['第1周', '第2周', '第3周', '第4周'];
    const trendData = [8, 12, 6, 10];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">反馈管理</div>
            <div class="page-subtitle">收集与处理用户反馈，持续优化产品体验</div>
          </div>
          <button class="btn btn-secondary btn-sm" onclick="App.toast('反馈导出（原型演示）','info')">${Utils.icon('download', 16)} 导出反馈</button>
        </div>
      </div>

      <!-- 统计卡片 -->
      <div class="grid grid-4 gap-4 mb-6">
        ${statCardHTML({ label: '待处理', value: '4', trend: '2', trendDir: 'up', icon: 'clock', iconBg: '#FFF5E6', iconColor: '#FF9500' })}
        ${statCardHTML({ label: '本周新增', value: '6', trend: '50%', trendDir: 'up', icon: 'plus', iconBg: '#EEEBFE', iconColor: '#4B3FE3' })}
        ${statCardHTML({ label: '已解决', value: '18', trend: '12%', trendDir: 'up', icon: 'check', iconBg: '#E0FAF0', iconColor: '#00B884' })}
        ${statCardHTML({ label: '平均评分', value: '4.3/5', trend: '0.2', trendDir: 'up', icon: 'star', iconBg: '#FFE5EB', iconColor: '#FF3B5C' })}
      </div>

      <div class="grid gap-4" style="grid-template-columns:1fr 300px;">
        <div>
          <!-- 筛选标签页 -->
          <div class="tabs mb-4">
            <div class="tab active" data-filter="all">全部 <span class="badge badge-neutral" style="margin-left:4px;">${counts.all}</span></div>
            <div class="tab" data-filter="bug">Bug <span class="badge badge-danger" style="margin-left:4px;">${counts.bug}</span></div>
            <div class="tab" data-filter="feature">功能建议 <span class="badge badge-primary" style="margin-left:4px;">${counts.feature}</span></div>
            <div class="tab" data-filter="improvement">改进 <span class="badge badge-info" style="margin-left:4px;">${counts.improvement}</span></div>
          </div>

          <!-- 反馈列表 -->
          <div class="flex flex-col gap-3">
            ${feedbacks.map(f => `
              <div class="card" data-type="${f.type}">
                <div class="card-body">
                  <div class="flex items-start gap-3 mb-3">
                    <div class="avatar avatar-md" style="background:${Utils.avatarColor(f.user)}">${f.user.charAt(0)}</div>
                    <div class="flex-1">
                      <div class="flex items-center gap-2 mb-1">
                        <span class="font-medium">${f.user}</span>
                        <span class="badge ${typeBadge[f.type]}">${typeText[f.type]}</span>
                        <span class="badge ${priorityBadge[f.priority]}">${priorityText[f.priority]}</span>
                        <span class="badge ${statusBadge[f.status]}">${statusText[f.status]}</span>
                        <span class="text-xs text-muted ml-auto">${f.time}</span>
                      </div>
                      <div class="text-sm text-secondary" style="line-height:1.6;">${f.content}</div>
                    </div>
                  </div>
                  <div class="flex items-center justify-between" style="border-top:1px solid var(--border-light);padding-top:12px;">
                    <div class="flex items-center gap-2 text-xs text-muted">
                      ${Utils.icon('message', 14)} 3 条回复
                      <span style="margin:0 4px;">·</span>
                      ${Utils.icon('clock', 14)} 提交于 ${f.time}
                    </div>
                    ${f.status === 'open' ? `<button class="btn btn-primary btn-sm" onclick="AdminFeedback.process(${f.id})">${Utils.icon('edit', 14)} 处理反馈</button>` : `<button class="btn btn-ghost btn-sm" onclick="App.toast('查看处理记录（原型演示）','info')">${Utils.icon('eye', 14)} 查看记录</button>`}
                  </div>
                </div>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- 右侧反馈趋势 -->
        <div class="card">
          <div class="card-header"><div class="card-title">反馈趋势</div><span class="text-xs text-muted">近 4 周</span></div>
          <div class="card-body">${svgBarChart(trendData, trendLabels, { color: '#FF9500', width: 280, height: 180 })}</div>
          <div class="card-body" style="padding-top:0;">
            <div class="divider"></div>
            <div class="text-sm font-medium mb-3">反馈类型分布</div>
            <div class="flex flex-col gap-2">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2"><span style="width:10px;height:10px;border-radius:2px;background:#FF3B5C;"></span><span class="text-sm">Bug</span></div>
                <span class="text-sm font-medium">${counts.bug} 条</span>
              </div>
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2"><span style="width:10px;height:10px;border-radius:2px;background:#4B3FE3;"></span><span class="text-sm">功能建议</span></div>
                <span class="text-sm font-medium">${counts.feature} 条</span>
              </div>
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2"><span style="width:10px;height:10px;border-radius:2px;background:#2196F3;"></span><span class="text-sm">改进</span></div>
                <span class="text-sm font-medium">${counts.improvement} 条</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {
    // 筛选标签页
    document.querySelectorAll('.tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.tabs .tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const filter = tab.dataset.filter;
        document.querySelectorAll('[data-type]').forEach(card => {
          if (filter === 'all' || card.dataset.type === filter) {
            card.style.display = '';
          } else {
            card.style.display = 'none';
          }
        });
      });
    });
  },
});

// 反馈处理操作
window.AdminFeedback = {
  process(id) {
    const fb = Mock.feedbacks.find(f => f.id === id);
    App.modal({
      title: '处理用户反馈',
      body: `
        <div class="flex items-start gap-3 p-3 rounded mb-4" style="background:var(--surface-muted);">
          <div class="avatar avatar-sm" style="background:${Utils.avatarColor(fb.user)}">${fb.user.charAt(0)}</div>
          <div>
            <div class="text-sm font-medium">${fb.user}</div>
            <div class="text-sm text-secondary" style="line-height:1.6;">${fb.content}</div>
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">回复内容 <span class="required">*</span></label>
          <textarea class="form-textarea" id="fb-reply" placeholder="请输入回复内容...">感谢您的反馈！我们已收到您的问题，将在下个版本中优化处理。</textarea>
        </div>
        <div class="grid grid-2 gap-4">
          <div class="form-group">
            <label class="form-label">更新状态</label>
            <select class="form-select" id="fb-status">
              <option value="open">待处理</option>
              <option value="planned" selected>已规划</option>
              <option value="resolved">已解决</option>
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">优先级调整</label>
            <select class="form-select" id="fb-priority">
              <option value="high" ${fb.priority === 'high' ? 'selected' : ''}>高优</option>
              <option value="medium" ${fb.priority === 'medium' ? 'selected' : ''}>中优</option>
              <option value="low" ${fb.priority === 'low' ? 'selected' : ''}>低优</option>
            </select>
          </div>
        </div>
      `,
      confirmText: '提交处理',
      onConfirm(overlay) {
        const reply = overlay.querySelector('#fb-reply').value.trim();
        if (!reply) { App.toast('请输入回复内容', 'warning'); return; }
        overlay.remove();
        App.toast('反馈已处理，回复已发送给用户', 'success');
      },
    });
  },
};
