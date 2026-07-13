/* === 知识库相关页面 === */

// ============================================================
// 页面1: 知识首页 (路由 'knowledge')
// ============================================================
App.registerPage('knowledge', {
  title: '知识首页',
  render() {
    // 统计卡片数据
    const stats = [
      { icon: 'doc', label: '文档总数', value: '1,019', trend: '+12 本周', color: '#4B3FE3', bg: 'var(--primary-bg)' },
      { icon: 'folder', label: '知识库', value: '6', trend: '已激活全部', color: '#00B884', bg: 'var(--success-bg)' },
      { icon: 'plus', label: '本月新增', value: '87', trend: '+23% 环比', color: '#FF9500', bg: 'var(--warning-bg)' },
      { icon: 'sparkles', label: 'AI 引用次数', value: '2,345', trend: '+456 今日', color: '#9C27B0', bg: '#F3E5F5' },
    ];

    // 推荐文档（取后3条作为推荐）
    const recommends = Mock.documents.slice(2, 5);
    const recommendReasons = ['与您浏览的「微服务架构」相关', '同知识库热门文档', 'AI 根据您的角色推荐'];

    return `
      <div class="page-header flex items-center justify-between">
        <div>
          <h1 class="page-title">知识库</h1>
          <p class="page-subtitle">企业知识的统一入口 · 共 ${Mock.stats.totalDocs} 篇文档分布在 ${Mock.stats.totalKB} 个知识库</p>
        </div>
        <div class="flex gap-2">
          <button class="btn btn-secondary" onclick="App.navigate('knowledge/graph')">
            ${Utils.icon('graph', 16)} <span>知识图谱</span>
          </button>
          <button class="btn btn-primary" onclick="App.navigate('manage/upload')">
            ${Utils.icon('plus', 16)} <span>新建知识库</span>
          </button>
        </div>
      </div>

      <!-- 统计卡片行 -->
      <div class="grid grid-4 gap-4 mb-8">
        ${stats.map(s => `
          <div class="stat-card">
            <div class="flex items-center justify-between">
              <div class="stat-card-icon" style="background:${s.bg};color:${s.color}">${Utils.icon(s.icon, 20)}</div>
              <span class="stat-card-trend up">${Utils.icon('trending', 12)} ${s.trend}</span>
            </div>
            <div class="stat-card-value">${s.value}</div>
            <div class="stat-card-label">${s.label}</div>
          </div>
        `).join('')}
      </div>

      <!-- 我的知识库 -->
      <div class="section-header">
        <div class="flex items-center gap-2">
          <h2 class="section-title">我的知识库</h2>
          <span class="badge badge-neutral">${Mock.knowledgeBases.length}</span>
        </div>
        <a href="#manage/kb" class="btn-link">管理全部 →</a>
      </div>
      <div class="grid grid-auto gap-4 mb-8">
        ${Mock.knowledgeBases.map(kb => `
          <div class="kb-card" onclick="App.navigate('knowledge/search')">
            <div class="kb-card-banner" style="background: linear-gradient(135deg, ${kb.color}, ${kb.color}cc);">${kb.icon}</div>
            <div class="kb-card-body">
              <div class="flex items-center justify-between mb-1">
                <div class="kb-card-title">${kb.name}</div>
                <span class="badge badge-primary">${kb.docs}</span>
              </div>
              <div class="kb-card-desc">${kb.desc}</div>
              <div class="kb-card-stats">
                <span class="flex items-center gap-1">${Utils.icon('doc', 12)} ${kb.docs} 文档</span>
                <span class="flex items-center gap-1">${Utils.icon('users', 12)} ${kb.members} 成员</span>
                <span class="flex items-center gap-1">${Utils.icon('clock', 12)} ${kb.updatedAt}</span>
              </div>
            </div>
          </div>
        `).join('')}
      </div>

      <!-- 最近浏览 -->
      <div class="section-header">
        <div class="flex items-center gap-2">
          <h2 class="section-title">最近浏览</h2>
          <span class="badge badge-neutral">${Mock.documents.length}</span>
        </div>
        <a href="#knowledge/search" class="btn-link">查看全部 →</a>
      </div>
      <div class="mb-8" style="overflow-x:auto;padding-bottom:8px;">
        <div class="flex gap-3" style="min-width:max-content;">
          ${Mock.documents.slice(0, 6).map(d => `
            <div class="doc-item" style="width:320px;flex-direction:column;align-items:stretch;" onclick="App.navigate('knowledge/doc/${d.id}')">
              <div class="flex items-center gap-3 mb-2">
                <div class="doc-item-icon" style="background:var(--primary-bg);color:var(--primary);">${Utils.icon('fileText', 18)}</div>
                <span class="badge badge-info">${Utils.fileTypeLabel(d.type)}</span>
                <span class="ml-auto text-xs text-muted">${Utils.icon('eye', 12)} ${d.views}</span>
              </div>
              <div class="font-semibold text-sm mb-1" style="line-height:1.4;">${d.title}</div>
              <div class="text-xs text-muted mb-2">${Utils.truncate(d.summary, 38)}</div>
              <div class="flex items-center gap-2 text-xs text-muted">
                <span>${d.kb}</span>
                <span>·</span>
                <span>${d.updatedAt.slice(5, 10)}</span>
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <!-- 推荐阅读 -->
      <div class="section-header">
        <div class="flex items-center gap-2">
          <h2 class="section-title">推荐阅读</h2>
          <span class="badge badge-primary">AI 推荐</span>
        </div>
      </div>
      <div class="grid grid-3 gap-4">
        ${recommends.map((d, i) => `
          <div class="card card-shadow" style="cursor:pointer;" onclick="App.navigate('knowledge/doc/${d.id}')">
            <div class="card-body">
              <div class="flex items-center gap-2 mb-3">
                <span class="badge badge-warning">${Utils.icon('sparkles', 12)} 推荐</span>
                <span class="badge badge-neutral">${Utils.fileTypeLabel(d.type)}</span>
              </div>
              <div class="font-semibold text-md mb-2" style="line-height:1.4;">${d.title}</div>
              <div class="text-sm text-secondary mb-3" style="line-height:1.6;">${Utils.truncate(d.summary, 60)}</div>
              <div class="flex items-center gap-2 mb-3">
                <div class="avatar avatar-sm" style="background:${Utils.avatarColor(d.author)}">${d.author.charAt(0)}</div>
                <span class="text-xs text-muted">${d.author} · ${d.updatedAt.slice(5, 10)}</span>
                <span class="ml-auto text-xs text-muted">${Utils.icon('eye', 12)} ${d.views}</span>
              </div>
              <div class="tag" style="font-size:11px;">${Utils.icon('sparkles', 10)} ${recommendReasons[i]}</div>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  },
  init() {
    // 卡片悬停提示
    document.querySelectorAll('.kb-card').forEach(card => {
      card.addEventListener('click', () => {
        App.toast('打开知识库详情（原型演示）', 'info');
      });
    });
  }
});

// ============================================================
// 页面2: 知识搜索 (路由 'knowledge/search')
// ============================================================
App.registerPage('knowledge/search', {
  title: '知识搜索',
  render() {
    // 筛选数据
    const docTypes = [
      { label: 'PDF', count: 234 },
      { label: 'Word', count: 187 },
      { label: 'Markdown', count: 421 },
      { label: 'Excel', count: 89 },
    ];
    const timeRanges = ['全部', '今天', '本周', '本月', '今年'];
    const filterTags = Mock.tags.slice(0, 6);

    return `
      <div class="page-header">
        <div class="search-bar" style="margin:0 auto;">
          <span class="search-bar-icon">${Utils.icon('search', 20)}</span>
          <input type="text" id="ks-input" placeholder="搜索知识库中的文档、知识、人员..." value="微服务架构" />
        </div>
      </div>

      <div class="flex gap-5">
        <!-- 左侧筛选面板 -->
        <aside style="width:240px;flex-shrink:0;">
          <div class="card card-shadow p-4" style="position:sticky;top:72px;">
            <div class="flex items-center justify-between mb-4">
              <span class="font-semibold text-sm">${Utils.icon('filter', 14)} 筛选条件</span>
              <button class="btn-link" id="ks-reset">重置</button>
            </div>

            <!-- 知识库筛选 -->
            <div class="filter-group">
              <div class="filter-label">知识库</div>
              ${Mock.knowledgeBases.map((kb, i) => `
                <label class="filter-option">
                  <input type="checkbox" class="ks-filter" data-type="kb" value="${kb.name}" ${i < 2 ? 'checked' : ''}>
                  <span>${kb.icon} ${Utils.truncate(kb.name, 12)}</span>
                  <span class="ml-auto text-xs text-muted">${kb.docs}</span>
                </label>
              `).join('')}
            </div>

            <!-- 文档类型 -->
            <div class="filter-group">
              <div class="filter-label">文档类型</div>
              ${docTypes.map((t, i) => `
                <label class="filter-option">
                  <input type="checkbox" class="ks-filter" data-type="type" value="${t.label}" ${i < 1 ? 'checked' : ''}>
                  <span>${t.label}</span>
                  <span class="ml-auto text-xs text-muted">${t.count}</span>
                </label>
              `).join('')}
            </div>

            <!-- 时间范围 -->
            <div class="filter-group">
              <div class="filter-label">时间范围</div>
              <div class="tabs-pill" style="flex-wrap:wrap;width:100%;">
                ${timeRanges.map((r, i) => `
                  <div class="tab-pill ks-time ${i === 0 ? 'active' : ''}" data-value="${r}" style="font-size:12px;">${r}</div>
                `).join('')}
              </div>
            </div>

            <!-- 标签筛选 -->
            <div class="filter-group" style="margin-bottom:0;">
              <div class="filter-label">标签</div>
              <div class="flex flex-wrap gap-2">
                ${filterTags.map((t, i) => `
                  <span class="tag ks-tag ${i < 2 ? '' : ''}" data-value="${t.name}" style="font-size:11px;">${t.name}</span>
                `).join('')}
              </div>
            </div>
          </div>
        </aside>

        <!-- 右侧搜索结果 -->
        <div class="flex-1" style="min-width:0;">
          <!-- 结果统计与排序 -->
          <div class="card card-shadow p-4 mb-4 flex items-center justify-between">
            <div class="text-sm text-secondary">
              找到 <span class="font-bold text-primary" id="ks-count">23</span> 条结果 · 用时 <span class="font-medium">0.23s</span>
            </div>
            <div class="flex items-center gap-3">
              <span class="text-xs text-muted">排序：</span>
              <select class="form-select" id="ks-sort" style="width:auto;height:30px;font-size:12px;">
                <option value="relevance">相关度</option>
                <option value="time">时间</option>
                <option value="views">阅读量</option>
              </select>
            </div>
          </div>

          <!-- 搜索结果列表 -->
          <div class="card card-shadow" id="ks-results">
            ${this.renderResults(Mock.documents.slice(0, 7), '微服务架构')}
          </div>

          <!-- 分页控件 -->
          <div class="flex items-center justify-between mt-6">
            <div class="text-xs text-muted">共 23 条记录，每页显示 10 条</div>
            <div class="flex items-center gap-1">
              <button class="btn btn-secondary btn-sm btn-icon" disabled>${Utils.icon('arrowLeft', 14)}</button>
              <button class="btn btn-primary btn-sm" style="width:32px;padding:0;">1</button>
              <button class="btn btn-secondary btn-sm" style="width:32px;padding:0;">2</button>
              <button class="btn btn-secondary btn-sm" style="width:32px;padding:0;">3</button>
              <span class="text-muted">...</span>
              <button class="btn btn-secondary btn-sm" style="width:32px;padding:0;">10</button>
              <button class="btn btn-secondary btn-sm btn-icon">${Utils.icon('arrowRight', 14)}</button>
            </div>
          </div>
        </div>
      </div>
    `;
  },
  // 渲染搜索结果列表（高亮关键词）
  renderResults(docs, keyword) {
    const highlight = (text) => {
      if (!keyword) return Utils.escape(text);
      const esc = Utils.escape(text);
      const re = new RegExp(`(${keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
      return esc.replace(re, '<span class="highlight">$1</span>');
    };
    return docs.map(d => `
      <div class="search-result-item" onclick="App.navigate('knowledge/doc/${d.id}')">
        <div class="flex items-center gap-2 mb-2">
          <span class="badge badge-info">${Utils.fileTypeLabel(d.type)}</span>
          <span class="search-result-title">${highlight(d.title)}</span>
          <span class="badge badge-success ml-auto">${d.status === 'published' ? '已发布' : '审核中'}</span>
        </div>
        <div class="search-result-snippet">${highlight(d.summary)}</div>
        <div class="search-result-meta">
          <span class="flex items-center gap-1">${Utils.icon('folder', 12)} ${d.kb}</span>
          <span class="flex items-center gap-1">${Utils.icon('users', 12)} ${d.author}</span>
          <span class="flex items-center gap-1">${Utils.icon('clock', 12)} ${d.updatedAt}</span>
          <span class="flex items-center gap-1">${Utils.icon('eye', 12)} ${d.views} 阅读</span>
          <div class="flex items-center gap-1">
            ${d.tags.slice(0, 2).map(t => `<span class="tag" style="font-size:10px;padding:1px 6px;">${t}</span>`).join('')}
          </div>
        </div>
      </div>
    `).join('');
  },
  init() {
    const input = document.getElementById('ks-input');
    const resultsEl = document.getElementById('ks-results');
    const countEl = document.getElementById('ks-count');
    let keyword = input.value;

    // 输入搜索关键词实时过滤
    const doSearch = () => {
      const all = Mock.documents;
      const filtered = keyword
        ? all.filter(d => (d.title + d.summary).toLowerCase().includes(keyword.toLowerCase()))
        : all;
      resultsEl.innerHTML = this.renderResults(filtered.slice(0, 8), keyword);
      countEl.textContent = filtered.length;
    };

    input.addEventListener('input', (e) => {
      keyword = e.target.value.trim();
      doSearch();
    });

    // 排序变化
    document.getElementById('ks-sort').addEventListener('change', (e) => {
      const sorted = [...Mock.documents];
      if (e.target.value === 'views') sorted.sort((a, b) => b.views - a.views);
      else if (e.target.value === 'time') sorted.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
      resultsEl.innerHTML = this.renderResults(sorted.slice(0, 8), keyword);
      App.toast('已按' + e.target.options[e.target.selectedIndex].text + '排序', 'info');
    });

    // 时间范围切换
    document.querySelectorAll('.ks-time').forEach(t => {
      t.addEventListener('click', () => {
        document.querySelectorAll('.ks-time').forEach(x => x.classList.remove('active'));
        t.classList.add('active');
        App.toast('已筛选时间范围：' + t.dataset.value, 'info');
      });
    });

    // 标签切换
    document.querySelectorAll('.ks-tag').forEach(t => {
      t.addEventListener('click', () => {
        t.style.background = t.style.background.includes('220, 59, 227') ? 'var(--primary-bg)' : 'rgba(220, 59, 227, 0.1)';
        App.toast('已' + (t.style.background.includes('220, 59, 227') ? '添加' : '移除') + '标签：' + t.dataset.value, 'info');
      });
    });

    // 筛选项变化
    document.querySelectorAll('.ks-filter').forEach(f => {
      f.addEventListener('change', () => {
        doSearch();
        App.toast('筛选条件已更新', 'info');
      });
    });

    // 重置
    document.getElementById('ks-reset').addEventListener('click', () => {
      document.querySelectorAll('.ks-filter').forEach(f => f.checked = false);
      document.querySelectorAll('.ks-time').forEach((t, i) => t.classList.toggle('active', i === 0));
      document.querySelectorAll('.ks-tag').forEach(t => t.style.background = '');
      input.value = '';
      keyword = '';
      doSearch();
      App.toast('已重置所有筛选条件', 'success');
    });
  }
});

// ============================================================
// 页面3: 知识图谱 (路由 'knowledge/graph')
// ============================================================
App.registerPage('knowledge/graph', {
  title: '知识图谱',
  render() {
    // 节点定义（位置以百分比表示）
    const nodes = [
      // 中心节点
      { id: 'core', label: '企业知识库', type: 'core', x: 46, y: 46, links: ['langgraph', 'rag', 'mem0', 'ms', 'apisix', 'vlm', 'product', 'tech', 'ops'] },
      // 实体节点
      { id: 'langgraph', label: 'LangGraph', type: 'entity', x: 14, y: 14, desc: 'Agent 编排框架', docs: 12 },
      { id: 'rag', label: 'RAG', type: 'entity', x: 72, y: 18, desc: '检索增强生成', docs: 28 },
      { id: 'mem0', label: 'Mem0', type: 'entity', x: 82, y: 50, desc: '长期记忆系统', docs: 8 },
      { id: 'ms', label: '微服务', type: 'entity', x: 18, y: 70, desc: '服务架构模式', docs: 19 },
      { id: 'apisix', label: 'APISIX', type: 'entity', x: 8, y: 42, desc: 'API 网关', docs: 7 },
      { id: 'vlm', label: 'VLM', type: 'entity', x: 66, y: 78, desc: '视觉语言模型', docs: 5 },
      { id: 'mcp', label: 'MCP', type: 'entity', x: 50, y: 14, desc: '工具协议', docs: 9 },
      // 主题节点
      { id: 'product', label: '产品规划', type: 'topic', x: 36, y: 82, desc: '产品方向与路线图', docs: 23 },
      { id: 'tech', label: '技术方案', type: 'topic', x: 78, y: 66, desc: '架构与实现方案', docs: 45 },
      { id: 'ops', label: '运维手册', type: 'topic', x: 32, y: 28, desc: '系统运维与排障', docs: 34 },
      // 文档节点
      { id: 'doc1', label: '微服务架构设计规范 v2.0', type: 'doc', x: 52, y: 90, desc: '微服务架构设计文档', docs: 1 },
      { id: 'doc2', label: 'RAG 系统技术方案', type: 'doc', x: 58, y: 8, desc: 'RAG 架构方案文档', docs: 1 },
      { id: 'doc3', label: 'API 接口设计规范 v3.0', type: 'doc', x: 90, y: 30, desc: 'API 设计规范', docs: 1 },
    ];

    // 连接关系（用于绘制边）
    const edges = [
      ['core', 'langgraph'], ['core', 'rag'], ['core', 'mem0'], ['core', 'ms'],
      ['core', 'apisix'], ['core', 'vlm'], ['core', 'mcp'],
      ['core', 'product'], ['core', 'tech'], ['core', 'ops'],
      ['langgraph', 'rag'], ['rag', 'mem0'], ['mcp', 'langgraph'],
      ['ms', 'apisix'], ['vlm', 'rag'],
      ['ms', 'doc1'], ['rag', 'doc2'], ['apisix', 'doc3'],
      ['product', 'doc1'], ['tech', 'doc2'], ['ops', 'doc3'],
    ];

    const nodeMap = {};
    nodes.forEach(n => nodeMap[n.id] = n);

    return `
      <div class="page-header flex items-center justify-between">
        <div>
          <h1 class="page-title">知识图谱</h1>
          <p class="page-subtitle">可视化展示知识库中实体、主题与文档的关联关系</p>
        </div>
        <div class="tabs-pill">
          <div class="tab-pill active" data-view="force">力导向图</div>
          <div class="tab-pill" data-view="tree">树状图</div>
          <div class="tab-pill" data-view="circle">环形图</div>
        </div>
      </div>

      <div class="flex gap-4">
        <!-- 中央图谱区域 -->
        <div class="flex-1" style="min-width:0;">
          <div class="graph-canvas" id="kg-canvas">
            <!-- SVG 连接线 -->
            <svg id="kg-edges" style="position:absolute;left:0;top:0;width:100%;height:100%;z-index:1;pointer-events:none;">
              ${edges.map(([a, b]) => {
                const na = nodeMap[a], nb = nodeMap[b];
                return `<line class="kg-edge" data-from="${a}" data-to="${b}" x1="${na.x}%" y1="${na.y}%" x2="${nb.x}%" y2="${nb.y}%" stroke="var(--border-dark)" stroke-width="1.5" stroke-opacity="0.5" />`;
              }).join('')}
            </svg>

            <!-- 节点 -->
            ${nodes.map(n => `
              <div class="graph-node ${n.type}" data-id="${n.id}" data-type="${n.type}"
                style="left:${n.x}%;top:${n.y}%;transform:translate(-50%,-50%);">
                ${n.type === 'core' ? Utils.icon('database', 14) + ' ' : ''}${n.label}
              </div>
            `).join('')}
          </div>

          <!-- 图例说明 -->
          <div class="card card-shadow mt-4 p-4 flex items-center gap-6 flex-wrap">
            <span class="text-xs text-muted font-semibold">图例：</span>
            <div class="flex items-center gap-2">
              <span style="width:12px;height:12px;border-radius:50%;background:var(--primary);display:inline-block;"></span>
              <span class="text-sm">核心实体</span>
            </div>
            <div class="flex items-center gap-2">
              <span style="width:12px;height:12px;border-radius:50%;background:var(--info-bg);border:1px solid var(--info);display:inline-block;"></span>
              <span class="text-sm">技术实体</span>
            </div>
            <div class="flex items-center gap-2">
              <span style="width:12px;height:12px;border-radius:50%;background:var(--success-bg);border:1px solid var(--success);display:inline-block;"></span>
              <span class="text-sm">主题分类</span>
            </div>
            <div class="flex items-center gap-2">
              <span style="width:12px;height:12px;border-radius:50%;background:var(--warning-bg);border:1px solid var(--warning);display:inline-block;"></span>
              <span class="text-sm">文档节点</span>
            </div>
            <span class="ml-auto text-xs text-muted">提示：点击节点查看关联详情</span>
          </div>
        </div>

        <!-- 右侧详情面板 -->
        <aside style="width:320px;flex-shrink:0;">
          <div class="card card-shadow" id="kg-detail" style="position:sticky;top:72px;">
            <div class="card-header">
              <span class="card-title">实体详情</span>
              <span class="badge badge-primary" id="kg-detail-type">核心</span>
            </div>
            <div class="card-body" id="kg-detail-body">
              <div class="text-center py-4">
                <div style="font-size:40px;margin-bottom:12px;">🧭</div>
                <div class="font-semibold text-md mb-1">企业知识库</div>
                <div class="text-sm text-muted">点击任意节点查看详情</div>
              </div>
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init() {
    const canvas = document.getElementById('kg-canvas');
    const edges = document.querySelectorAll('.kg-edge');
    const nodes = canvas.querySelectorAll('.graph-node');
    const detailBody = document.getElementById('kg-detail-body');
    const detailType = document.getElementById('kg-detail-type');

    // 节点详情数据
    const details = {
      core: { label: '企业知识库', type: '核心', icon: '🧭', desc: '知识库的根节点，连接所有核心实体与主题分类。', docs: 1019, entities: 7, updated: '2026-07-04', related: ['LangGraph', 'RAG', 'Mem0', '微服务', 'APISIX', 'VLM', 'MCP'] },
      langgraph: { label: 'LangGraph', type: '技术实体', icon: '🔗', desc: 'Agent 编排框架，用于构建多步骤工作流的智能体应用。', docs: 12, entities: 3, updated: '2026-07-03', related: ['RAG', 'MCP', 'Mem0'] },
      rag: { label: 'RAG', type: '技术实体', icon: '🔍', desc: '检索增强生成，结合知识检索与大模型生成的核心能力。', docs: 28, entities: 4, updated: '2026-07-02', related: ['LangGraph', 'Mem0', 'VLM'] },
      mem0: { label: 'Mem0', type: '技术实体', icon: '💾', desc: '长期记忆系统，为 AI 对话提供上下文记忆能力。', docs: 8, entities: 2, updated: '2026-06-30', related: ['RAG', 'LangGraph'] },
      ms: { label: '微服务', type: '技术实体', icon: '🧩', desc: '微服务架构模式，服务拆分与独立部署的设计理念。', docs: 19, entities: 3, updated: '2026-07-03', related: ['APISIX', '产品规划'] },
      apisix: { label: 'APISIX', type: '技术实体', icon: '🚪', desc: '云原生 API 网关，负责路由、鉴权与流量管理。', docs: 7, entities: 2, updated: '2026-06-29', related: ['微服务', '运维手册'] },
      vlm: { label: 'VLM', type: '技术实体', icon: '👁️', desc: '视觉语言模型，支持图像理解与多模态文档处理。', docs: 5, entities: 2, updated: '2026-06-28', related: ['RAG', '技术方案'] },
      mcp: { label: 'MCP', type: '技术实体', icon: '🛠️', desc: 'Model Context Protocol，工具调用协议标准。', docs: 9, entities: 2, updated: '2026-06-27', related: ['LangGraph', '技术方案'] },
      product: { label: '产品规划', type: '主题', icon: '🎯', desc: '产品方向与季度路线图相关文档集合。', docs: 23, entities: 3, updated: '2026-07-04', related: ['微服务架构设计规范', 'RAG 系统技术方案'] },
      tech: { label: '技术方案', type: '主题', icon: '⚙️', desc: '架构设计与实现方案文档集合。', docs: 45, entities: 4, updated: '2026-07-02', related: ['RAG 系统技术方案', 'API 接口设计规范'] },
      ops: { label: '运维手册', type: '主题', icon: '🛡️', desc: '系统运维、部署与故障排查相关文档。', docs: 34, entities: 3, updated: '2026-06-29', related: ['APISIX', 'API 接口设计规范'] },
      doc1: { label: '微服务架构设计规范 v2.0', type: '文档', icon: '📄', desc: '基于 APISIX 网关的微服务架构设计规范文档。', docs: 1, entities: 2, updated: '2026-07-03', related: ['微服务', 'APISIX'] },
      doc2: { label: 'RAG 系统技术方案', type: '文档', icon: '📄', desc: '基于 LangGraph + LlamaIndex 的 Agentic RAG 架构设计。', docs: 1, entities: 3, updated: '2026-07-02', related: ['RAG', 'LangGraph', 'Mem0'] },
      doc3: { label: 'API 接口设计规范 v3.0', type: '文档', icon: '📄', desc: 'RESTful API 设计规范第三版，新增 MCP 工具协议支持。', docs: 1, entities: 2, updated: '2026-06-26', related: ['APISIX', 'MCP'] },
    };

    // 点击节点高亮关联连接并显示详情
    nodes.forEach(node => {
      node.addEventListener('click', () => {
        const id = node.dataset.id;
        const d = details[id];
        if (!d) return;

        // 重置所有节点与边
        nodes.forEach(n => n.style.opacity = '0.4');
        edges.forEach(e => { e.setAttribute('stroke-opacity', '0.15'); e.setAttribute('stroke', 'var(--border-dark)'); });

        // 高亮当前节点
        node.style.opacity = '1';

        // 高亮关联边与目标节点
        edges.forEach(e => {
          const from = e.dataset.from, to = e.dataset.to;
          if (from === id || to === id) {
            e.setAttribute('stroke-opacity', '1');
            e.setAttribute('stroke', 'var(--primary)');
            e.setAttribute('stroke-width', '2.5');
            const targetId = from === id ? to : from;
            const targetNode = canvas.querySelector(`[data-id="${targetId}"]`);
            if (targetNode) targetNode.style.opacity = '1';
          } else {
            e.setAttribute('stroke-width', '1.5');
          }
        });

        // 渲染详情面板
        detailType.textContent = d.type;
        const typeColor = { '核心': 'primary', '技术实体': 'info', '主题': 'success', '文档': 'warning' }[d.type] || 'neutral';
        detailType.className = `badge badge-${typeColor}`;
        detailBody.innerHTML = `
          <div class="text-center mb-4">
            <div style="font-size:48px;margin-bottom:12px;">${d.icon}</div>
            <div class="font-semibold text-lg mb-1">${d.label}</div>
            <span class="badge badge-${typeColor}">${d.type}</span>
          </div>
          <div class="text-sm text-secondary mb-4" style="line-height:1.7;">${d.desc}</div>
          <div class="divider"></div>
          <div class="grid grid-3 gap-3 text-center mb-4">
            <div>
              <div class="font-bold text-md text-primary">${d.docs}</div>
              <div class="text-xs text-muted">关联文档</div>
            </div>
            <div>
              <div class="font-bold text-md text-success">${d.entities}</div>
              <div class="text-xs text-muted">关联实体</div>
            </div>
            <div>
              <div class="font-bold text-md text-warning">${d.updated.slice(5)}</div>
              <div class="text-xs text-muted">最近更新</div>
            </div>
          </div>
          <div class="divider"></div>
          <div class="filter-label mb-2">关联实体</div>
          <div class="flex flex-wrap gap-2">
            ${d.related.map(r => `<span class="tag" style="font-size:11px;">${r}</span>`).join('')}
          </div>
          <button class="btn btn-primary btn-block btn-sm mt-4" onclick="App.navigate('knowledge/search')">
            ${Utils.icon('search', 14)} 查看关联文档
          </button>
        `;
      });
    });

    // 视图切换
    document.querySelectorAll('[data-view]').forEach(v => {
      v.addEventListener('click', () => {
        document.querySelectorAll('[data-view]').forEach(x => x.classList.remove('active'));
        v.classList.add('active');
        App.toast('已切换为' + v.textContent + '视图（原型演示）', 'info');
      });
    });
  }
});

// ============================================================
// 页面4: 知识时间线 (路由 'knowledge/timeline')
// ============================================================
App.registerPage('knowledge/timeline', {
  title: '知识时间线',
  render() {
    const filters = [
      { label: '全部', value: 'all', active: true },
      { label: '文档', value: 'success' },
      { label: '审核', value: 'warning' },
      { label: '缺口', value: 'danger' },
      { label: '反馈', value: 'feedback' },
    ];
    // 顶部统计
    const topStats = [
      { label: '本周新增文档', value: 12, icon: 'doc', color: 'var(--primary)', bg: 'var(--primary-bg)' },
      { label: '审核通过', value: 8, icon: 'check', color: 'var(--success)', bg: 'var(--success-bg)' },
      { label: '缺口预警', value: 3, icon: 'alert', color: 'var(--danger)', bg: 'var(--danger-bg)' },
    ];
    // 热门贡献者
    const contributors = [
      { name: '张明', dept: '产品中心', count: 28, avatar: '张' },
      { name: '李华', dept: '研发部', count: 23, avatar: '李' },
      { name: '陈静', dept: 'IT部', count: 19, avatar: '陈' },
      { name: '孙莉', dept: '人力资源', count: 15, avatar: '孙' },
    ];

    return `
      <div class="page-header flex items-center justify-between">
        <div>
          <h1 class="page-title">知识时间线</h1>
          <p class="page-subtitle">追踪知识库的动态变化与关键事件</p>
        </div>
        <div class="tabs-pill">
          ${filters.map((f, i) => `<div class="tab-pill tl-filter ${f.active ? 'active' : ''}" data-value="${f.value}">${f.label}</div>`).join('')}
        </div>
      </div>

      <!-- 顶部统计 -->
      <div class="grid grid-3 gap-4 mb-6">
        ${topStats.map(s => `
          <div class="card card-shadow p-4 flex items-center gap-4">
            <div class="stat-card-icon" style="background:${s.bg};color:${s.color};">${Utils.icon(s.icon, 20)}</div>
            <div>
              <div class="font-bold text-2xl">${s.value}</div>
              <div class="text-xs text-muted">${s.label}</div>
            </div>
          </div>
        `).join('')}
      </div>

      <div class="flex gap-5">
        <!-- 时间线主体 -->
        <div class="flex-1" style="min-width:0;">
          <div class="card card-shadow p-6">
            <div class="flex items-center justify-between mb-5">
              <h2 class="section-title">最近 7 天动态</h2>
              <span class="text-xs text-muted">${Mock.timelineEvents.length} 条事件</span>
            </div>
            <div class="timeline" id="tl-list">
              ${Mock.timelineEvents.map(e => {
                const typeIcon = { success: '✓', warning: '⏳', danger: '⚠' }[e.type] || '•';
                return `
                <div class="timeline-item ${e.type}" data-type="${e.type}">
                  <div class="timeline-date">${e.date}</div>
                  <div class="timeline-title">${typeIcon} ${e.title}</div>
                  <div class="timeline-content">${e.content}</div>
                </div>
              `;
              }).join('')}
            </div>
          </div>
        </div>

        <!-- 右侧热门活动 -->
        <aside style="width:280px;flex-shrink:0;">
          <div class="card card-shadow" style="position:sticky;top:72px;">
            <div class="card-header">
              <span class="card-title">${Utils.icon('star', 16)} 热门贡献者</span>
              <span class="badge badge-warning">本周</span>
            </div>
            <div class="card-body">
              ${contributors.map((c, i) => `
                <div class="flex items-center gap-3 ${i < contributors.length - 1 ? 'mb-3' : ''}">
                  <div style="position:relative;">
                    <div class="avatar avatar-md" style="background:${Utils.avatarColor(c.name)}">${c.avatar}</div>
                    ${i < 3 ? `<div style="position:absolute;top:-4px;right:-4px;width:18px;height:18px;border-radius:50%;background:${['#FFD700','#C0C0C0','#CD7F32'][i]};color:white;font-size:10px;display:flex;align-items:center;justify-content:center;font-weight:700;border:2px solid var(--surface);">${i + 1}</div>` : ''}
                  </div>
                  <div class="flex-1">
                    <div class="font-medium text-sm">${c.name}</div>
                    <div class="text-xs text-muted">${c.dept}</div>
                  </div>
                  <div class="text-right">
                    <div class="font-bold text-primary">${c.count}</div>
                    <div class="text-xs text-muted">贡献</div>
                  </div>
                </div>
              `).join('')}
            </div>
            <div class="card-footer">
              <a href="#admin/users" class="btn-link">查看完整榜单 →</a>
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init() {
    // 筛选事件类型
    document.querySelectorAll('.tl-filter').forEach(f => {
      f.addEventListener('click', () => {
        document.querySelectorAll('.tl-filter').forEach(x => x.classList.remove('active'));
        f.classList.add('active');
        const value = f.dataset.value;
        const items = document.querySelectorAll('#tl-list .timeline-item');
        items.forEach(item => {
          if (value === 'all') {
            item.style.display = '';
          } else {
            item.style.display = item.dataset.type === value ? '' : 'none';
          }
        });
        App.toast('已筛选：' + f.textContent, 'info');
      });
    });
  }
});

// ============================================================
// 页面5: 文档详情 (路由 'knowledge/doc')
// ============================================================
App.registerPage('knowledge/doc', {
  title: '文档详情',
  render(params) {
    // 接收文档ID，默认取第1篇
    const docId = params && params[0] ? parseInt(params[0]) : 1;
    const doc = Mock.documents.find(d => d.id === docId) || Mock.documents[0];

    // 文档大纲（TOC）
    const toc = [
      { level: 1, title: '概述', active: true },
      { level: 2, title: '背景与目标', active: false },
      { level: 1, title: '架构设计', active: false },
      { level: 2, title: '整体架构', active: false },
      { level: 2, title: '服务拆分原则', active: false },
      { level: 2, title: '通信协议选型', active: false },
      { level: 1, title: '部署策略', active: false },
      { level: 1, title: '监控与告警', active: false },
    ];

    // 相关文档
    const related = Mock.documents.filter(d => d.id !== doc.id).slice(0, 3);
    // 协作者
    const collaborators = Mock.users.slice(0, 4);
    // 引用记录
    const citations = [
      { user: '王芳', avatar: '王', time: '14:32', content: '请问这个方案的部署成本大概是多少？' },
      { user: '刘强', avatar: '刘', time: '13:15', content: '在微服务拆分时，建议补充数据一致性章节。' },
      { user: '陈静', avatar: '陈', time: '昨天', content: 'APISIX 网关的限流策略我们已经在用，效果不错。' },
    ];
    // 文档讨论评论
    const docComments = [
      { user: '李华', avatar: '李', role: '编辑者', time: '2小时前', content: '关于第3章的服务拆分原则，建议补充「数据库拆分边界」的内容。目前只提到了服务边界，但数据层的一致性方案没有展开。', status: 'open', likes: 5, aiAnswer: null },
      { user: '王芳', avatar: '王', role: '查看者', time: '5小时前', content: 'APISIX 和 Nginx 的主要区别是什么？为什么要从 Nginx 迁移？', status: 'resolved', likes: 3, aiAnswer: 'APISIX 基于 Nginx + etcd 架构，核心优势是动态配置（无需 reload）、80+ 插件生态、原生支持 SSE/WebSocket 长连接、Apache 2.0 协议合规。对于知识库项目，SSE 流式输出和 WebSocket 协同编辑是刚需，APISIX 原生支持这些特性。' },
      { user: '刘强', avatar: '刘', role: '编辑者', time: '昨天', content: '部署策略部分缺少灰度发布方案，能否补充？', status: 'open', likes: 2, aiAnswer: null },
      { user: '陈静', avatar: '陈', role: '作者', time: '3天前', content: '已更新部署章节，新增了灰度发布方案和回滚策略，请查看 4.2 节。', status: 'resolved', likes: 8, aiAnswer: null },
    ];

    return `
      <!-- 顶部工具栏 -->
      <div class="card card-shadow p-3 mb-4 flex items-center gap-2">
        <button class="btn btn-ghost btn-sm" onclick="history.back()">
          ${Utils.icon('arrowLeft', 16)} 返回
        </button>
        <div class="divider-vertical"></div>
        <div class="flex items-center gap-2 text-sm text-muted">
          <a href="#knowledge">知识库</a>
          <span>/</span>
          <a href="#knowledge/search">${doc.kb}</a>
          <span>/</span>
          <span class="text-secondary">${Utils.truncate(doc.title, 20)}</span>
        </div>
        <div class="ml-auto flex items-center gap-2">
          <button class="btn btn-secondary btn-sm" onclick="App.navigate('manage/editor')">${Utils.icon('edit', 14)} 编辑</button>
          <button class="btn btn-secondary btn-sm" onclick="App.toast('分享链接已复制', 'success')">${Utils.icon('share', 14)} 分享</button>
          <button class="btn btn-secondary btn-sm" onclick="App.toast('开始下载...', 'info')">${Utils.icon('download', 14)} 下载</button>
          <button class="btn btn-ghost btn-sm btn-icon" onclick="App.toast('已收藏', 'success')">${Utils.icon('bookmark', 14)}</button>
          <button class="btn btn-ghost btn-sm btn-icon">${Utils.icon('more', 14)}</button>
        </div>
      </div>

      <div class="flex gap-5">
        <!-- 左侧目录 -->
        <aside style="width:220px;flex-shrink:0;">
          <div class="card card-shadow" style="position:sticky;top:72px;">
            <div class="card-header"><span class="card-title text-sm">${Utils.icon('layers', 14)} 目录</span></div>
            <div class="card-body p-3">
              ${toc.map(t => `
                <div class="list-item doc-toc ${t.active ? 'active' : ''}" data-level="${t.level}" style="padding:6px 10px;font-size:13px;${t.level === 2 ? 'padding-left:24px;' : ''}">
                  ${t.title}
                </div>
              `).join('')}
            </div>
          </div>
        </aside>

        <!-- 中央文档内容 -->
        <div class="flex-1" style="min-width:0;">
          <div class="card card-shadow p-6 mb-4">
            <!-- 文档标题与元信息 -->
            <h1 style="font-size:28px;font-weight:700;line-height:1.3;margin-bottom:12px;">${doc.title}</h1>
            <div class="flex items-center gap-4 mb-5 flex-wrap">
              <div class="flex items-center gap-2">
                <div class="avatar avatar-sm" style="background:${Utils.avatarColor(doc.author)}">${doc.author.charAt(0)}</div>
                <span class="text-sm">${doc.author}</span>
              </div>
              <span class="text-sm text-muted">${Utils.icon('clock', 12)} ${doc.updatedAt}</span>
              <span class="text-sm text-muted">${Utils.icon('eye', 12)} ${doc.views} 阅读</span>
              <span class="badge badge-info">${Utils.fileTypeLabel(doc.type)}</span>
              <span class="badge badge-success">${doc.status === 'published' ? '已发布' : '审核中'}</span>
              <div class="flex gap-1">
                ${doc.tags.map(t => `<span class="tag" style="font-size:11px;">${t}</span>`).join('')}
              </div>
            </div>

            <div class="divider"></div>

            <!-- 文档正文 -->
            <div class="doc-content">
              <h2 id="sec-1" style="font-size:20px;font-weight:600;margin:24px 0 12px;">概述</h2>
              <p style="line-height:1.8;color:var(--text-secondary);margin-bottom:16px;">${doc.summary}本文档旨在为团队提供一套可落地的设计参考，覆盖从架构选型到部署运维的完整生命周期。</p>

              <h3 style="font-size:16px;font-weight:600;margin:20px 0 10px;">背景与目标</h3>
              <p style="line-height:1.8;color:var(--text-secondary);margin-bottom:16px;">随着业务规模扩张，单体架构在扩展性、发布效率和团队协作上的瓶颈日益凸显。本方案目标是通过微服务化改造，实现：</p>
              <ul style="line-height:1.8;color:var(--text-secondary);margin-bottom:16px;padding-left:24px;list-style:disc;">
                <li>独立部署与弹性伸缩，提升系统吞吐能力 3 倍以上</li>
                <li>服务边界清晰，支持多团队并行开发</li>
                <li>故障隔离，单服务异常不影响全局可用性</li>
                <li>统一 API 网关，集中管理鉴权、限流与监控</li>
              </ul>

              <h2 id="sec-2" style="font-size:20px;font-weight:600;margin:24px 0 12px;">架构设计</h2>
              <h3 style="font-size:16px;font-weight:600;margin:20px 0 10px;">整体架构</h3>
              <p style="line-height:1.8;color:var(--text-secondary);margin-bottom:16px;">系统采用 APISIX 作为统一入口网关，下游服务按业务域拆分为订单、用户、商品、支付等独立服务，通过 gRPC 进行内部通信，使用 Nacos 做服务发现与配置管理。</p>

              <!-- 代码块 -->
              <pre style="background:var(--sidebar-bg);color:#E0E0E0;padding:16px;border-radius:8px;overflow-x:auto;font-family:var(--font-mono);font-size:13px;line-height:1.6;margin-bottom:16px;"><code># APISIX 路由配置示例
routes:
  - uri: /api/v1/orders/*
    upstream:
      type: roundrobin
      nodes:
        "order-service:8080": 1
    plugins:
      - jwt-auth: {}
      - limit-req:
          rate: 100
          burst: 50</code></pre>

              <h3 style="font-size:16px;font-weight:600;margin:20px 0 10px;">服务拆分原则</h3>
              <p style="line-height:1.8;color:var(--text-secondary);margin-bottom:16px;">遵循领域驱动设计（DDD），按业务能力而非技术层级拆分服务。每个服务拥有独立的数据存储，避免共享数据库导致的耦合。</p>

              <!-- 表格 -->
              <div class="table-wrap mb-4">
                <table class="data-table">
                  <thead>
                    <tr><th>服务名</th><th>职责</th><th>技术栈</th><th>SLA</th></tr>
                  </thead>
                  <tbody>
                    <tr><td>订单服务</td><td>订单创建、查询、状态流转</td><td>Go + MySQL</td><td>99.95%</td></tr>
                    <tr><td>用户服务</td><td>账户、权限、会话管理</td><td>Java + PostgreSQL</td><td>99.9%</td></tr>
                    <tr><td>商品服务</td><td>商品目录、库存、检索</td><td>Go + Redis</td><td>99.95%</td></tr>
                    <tr><td>支付服务</td><td>支付渠道对接、对账</td><td>Java + MongoDB</td><td>99.99%</td></tr>
                  </tbody>
                </table>
              </div>

              <h3 style="font-size:16px;font-weight:600;margin:20px 0 10px;">通信协议选型</h3>
              <p style="line-height:1.8;color:var(--text-secondary);margin-bottom:16px;">内部服务间采用 gRPC（基于 HTTP/2 + Protobuf）通信，对外暴露 RESTful API。需要低延迟、高吞吐的场景使用 gRPC，对浏览器友好的场景使用 HTTP/JSON。</p>

              <h2 id="sec-3" style="font-size:20px;font-weight:600;margin:24px 0 12px;">部署策略</h2>
              <p style="line-height:1.8;color:var(--text-secondary);margin-bottom:16px;">采用 Kubernetes 容器化部署，通过 Helm Chart 管理服务配置。发布流程支持蓝绿部署与金丝雀发布，确保零停机上线。</p>

              <h2 id="sec-4" style="font-size:20px;font-weight:600;margin:24px 0 12px;">监控与告警</h2>
              <p style="line-height:1.8;color:var(--text-secondary);margin-bottom:16px;">基于 Prometheus + Grafana 构建监控体系，关键指标包括 QPS、P99 延迟、错误率、资源利用率。告警规则分级：P0 立即电话通知，P1 钉钉群告警，P2 邮件汇总。</p>
            </div>

            <!-- 文档反馈 -->
            <div class="divider mt-6"></div>
            <div class="flex items-center justify-between mt-4">
              <span class="text-sm text-muted">这篇文档对您有帮助吗？</span>
              <div class="flex gap-2">
                <button class="btn btn-secondary btn-sm" onclick="App.toast('感谢您的反馈！', 'success')">${Utils.icon('check', 14)} 有帮助</button>
                <button class="btn btn-secondary btn-sm" onclick="App.toast('已记录改进建议', 'warning')">需改进</button>
              </div>
            </div>
          </div>

          <!-- 相关文档 -->
          <div class="card card-shadow p-5 mb-4">
            <h3 class="section-title mb-4">${Utils.icon('link', 16)} 相关文档</h3>
            <div class="grid grid-3 gap-3">
              ${related.map(r => `
                <div class="doc-item" style="cursor:pointer;flex-direction:column;align-items:stretch;" onclick="App.navigate('knowledge/doc/${r.id}')">
                  <div class="flex items-center gap-2 mb-2">
                    <span class="badge badge-info">${Utils.fileTypeLabel(r.type)}</span>
                    <span class="text-xs text-muted ml-auto">${Utils.icon('eye', 12)} ${r.views}</span>
                  </div>
                  <div class="font-semibold text-sm mb-1" style="line-height:1.4;">${r.title}</div>
                  <div class="text-xs text-muted">${Utils.truncate(r.summary, 30)}</div>
                </div>
              `).join('')}
            </div>
          </div>

          <!-- 文档评论区 -->
          <div class="card card-shadow">
            <div class="card-header">
              <span class="card-title">${Utils.icon('message', 16)} 文档讨论 (${docComments.length})</span>
              <div class="flex items-center gap-2">
                <span class="text-xs text-muted">按时间</span>
                <select class="form-select" style="width:auto;height:30px;font-size:12px;">
                  <option>最新优先</option>
                  <option>最早优先</option>
                  <option>热门优先</option>
                </select>
              </div>
            </div>
            <div class="card-body">
              <!-- 评论输入框 -->
              <div class="flex gap-3 mb-5">
                <div class="avatar avatar-md" style="background:${Utils.avatarColor(Mock.currentUser.name)}">${Mock.currentUser.avatar}</div>
                <div style="flex:1;">
                  <textarea class="form-textarea" id="docCommentInput" placeholder="对这篇文档有疑问或补充？发表你的看法..." style="min-height:60px;"></textarea>
                  <div class="flex items-center justify-between mt-2">
                    <div class="flex items-center gap-2 text-xs text-muted">
                      <span>支持 @提及、Markdown</span>
                    </div>
                    <button class="btn btn-primary btn-sm" onclick="submitDocComment()">
                      ${Utils.icon('send', 14)} 发表评论
                    </button>
                  </div>
                </div>
              </div>
              <div class="divider"></div>
              <!-- 评论列表 -->
              ${docComments.map((c, i) => `
                <div class="flex gap-3 ${i < docComments.length - 1 ? 'mb-4 pb-4 border-b' : ''}">
                  <div class="avatar avatar-md" style="background:${Utils.avatarColor(c.user)}">${c.avatar}</div>
                  <div style="flex:1;">
                    <div class="flex items-center gap-2 mb-1">
                      <span class="text-sm font-medium">${c.user}</span>
                      <span class="badge ${c.role === '作者' ? 'badge-primary' : 'badge-neutral'}">${c.role}</span>
                      <span class="text-xs text-muted ml-auto">${c.time}</span>
                    </div>
                    <div class="text-sm text-secondary mb-2" style="line-height:1.7;">${c.content}</div>
                    ${c.aiAnswer ? `
                      <div class="card" style="background:var(--primary-bg);border:none;padding:12px 16px;margin:8px 0;">
                        <div class="flex items-center gap-2 mb-1">
                          <div style="width:20px;height:20px;background:var(--primary);border-radius:4px;display:flex;align-items:center;justify-content:center;color:white;font-size:10px;font-weight:600;">AI</div>
                          <span class="text-xs font-medium" style="color:var(--primary);">AI 助手回答</span>
                          <span class="badge badge-success" style="font-size:10px;">已采纳</span>
                        </div>
                        <div class="text-sm" style="line-height:1.7;">${c.aiAnswer}</div>
                      </div>
                    ` : ''}
                    <div class="flex items-center gap-4 text-xs text-muted">
                      <button class="btn btn-ghost btn-sm" onclick="App.toast('已点赞','success')">${Utils.icon('star', 12)} ${c.likes}</button>
                      <button class="btn btn-ghost btn-sm" onclick="App.toast('回复功能','info')">${Utils.icon('message', 12)} 回复</button>
                      ${c.status === 'open' ? '<span class="badge badge-warning">待解答</span>' : '<span class="badge badge-success">已解决</span>'}
                      ${c.status === 'open' ? `<button class="btn btn-link btn-sm" onclick="App.navigate('chat')">让 AI 回答</button>` : ''}
                    </div>
                  </div>
                </div>
              `).join('')}
            </div>
          </div>
        </div>

        <!-- 右侧面板 -->
        <aside style="width:280px;flex-shrink:0;">
          <!-- 文档信息卡片 -->
          <div class="card card-shadow mb-4">
            <div class="card-header"><span class="card-title text-sm">${Utils.icon('fileText', 14)} 文档信息</span></div>
            <div class="card-body">
              <div class="flex items-center justify-between mb-3"><span class="text-xs text-muted">版本</span><span class="text-sm font-medium">v2.0</span></div>
              <div class="flex items-center justify-between mb-3"><span class="text-xs text-muted">文件大小</span><span class="text-sm font-medium">2.4 MB</span></div>
              <div class="flex items-center justify-between mb-3"><span class="text-xs text-muted">来源</span><span class="text-sm font-medium">上传</span></div>
              <div class="flex items-center justify-between mb-3"><span class="text-xs text-muted">创建时间</span><span class="text-sm font-medium">2026-06-20</span></div>
              <div class="flex items-center justify-between"><span class="text-xs text-muted">创建者</span><span class="text-sm font-medium">${doc.author}</span></div>
            </div>
          </div>

          <!-- AI 问答入口 -->
          <div class="card card-shadow mb-4" style="background:linear-gradient(135deg,var(--primary-bg),#F3E5F5);">
            <div class="card-body text-center">
              <div style="font-size:32px;margin-bottom:8px;">🤖</div>
              <div class="font-semibold text-md mb-1">AI 智能问答</div>
              <div class="text-xs text-muted mb-3">基于本文档内容回答您的问题</div>
              <button class="btn btn-primary btn-block btn-sm" onclick="App.navigate('chat')">
                ${Utils.icon('sparkles', 14)} 针对此文档提问
              </button>
            </div>
          </div>

          <!-- 协作者列表 -->
          <div class="card card-shadow mb-4">
            <div class="card-header"><span class="card-title text-sm">${Utils.icon('users', 14)} 协作者</span><span class="badge badge-neutral">${collaborators.length}</span></div>
            <div class="card-body">
              <div class="avatar-group mb-3">
                ${collaborators.map(c => `<div class="avatar avatar-md" style="background:${Utils.avatarColor(c.name)}">${c.avatar}</div>`).join('')}
              </div>
              ${collaborators.map(c => `
                <div class="flex items-center gap-2 mb-2">
                  <div class="avatar avatar-sm" style="background:${Utils.avatarColor(c.name)}">${c.avatar}</div>
                  <span class="text-sm">${c.name}</span>
                  <span class="text-xs text-muted ml-auto">${c.role}</span>
                </div>
              `).join('')}
            </div>
          </div>

          <!-- 引用记录 -->
          <div class="card card-shadow">
            <div class="card-header"><span class="card-title text-sm">${Utils.icon('message', 14)} 引用记录</span><span class="badge badge-primary">${citations.length}</span></div>
            <div class="card-body">
              ${citations.map(c => `
                <div class="mb-3 pb-3 ${citations.indexOf(c) < citations.length - 1 ? 'border-b' : ''}">
                  <div class="flex items-center gap-2 mb-1">
                    <div class="avatar avatar-sm" style="background:${Utils.avatarColor(c.user)}">${c.avatar}</div>
                    <span class="text-sm font-medium">${c.user}</span>
                    <span class="text-xs text-muted ml-auto">${c.time}</span>
                  </div>
                  <div class="text-xs text-secondary" style="line-height:1.6;padding-left:24px;">${c.content}</div>
                </div>
              `).join('')}
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init(params) {
    // 目录点击高亮与滚动
    document.querySelectorAll('.doc-toc').forEach(toc => {
      toc.addEventListener('click', () => {
        document.querySelectorAll('.doc-toc').forEach(t => t.classList.remove('active'));
        toc.classList.add('active');
        App.toast('已定位到章节：' + toc.textContent.trim(), 'info');
      });
    });
    // 提交评论
    window.submitDocComment = function() {
      const input = document.getElementById('docCommentInput');
      if (!input || !input.value.trim()) { App.toast('请输入评论内容', 'warning'); return; }
      App.toast('评论已发表，等待审核', 'success');
      input.value = '';
    };
  }
});

// ============================================
// 页面6: 问答社区 (knowledge/qa)
// ============================================
App.registerPage('knowledge/qa', {
  title: '问答社区',
  render() {
    const questions = [
      { id: 1, title: 'APISIX 网关和 Kong 的选型对比？', author: '李华', avatar: '李', dept: '研发部', time: '2小时前', tags: ['架构', 'API网关'], views: 234, answers: 3, status: 'resolved', aiAnswered: true, summary: '我们正在评估 API 网关方案，APISIX 和 Kong 都是开源方案，选哪个更好？' },
      { id: 2, title: 'LangGraph 的 Checkpoint 机制如何持久化多轮对话？', author: '王芳', avatar: '王', dept: '研发部', time: '5小时前', tags: ['LangGraph', 'RAG'], views: 156, answers: 2, status: 'resolved', aiAnswered: true, summary: '多轮对话场景下，Checkpoint 需要存储哪些状态？PostgreSQL 作为后端是否合适？' },
      { id: 3, title: 'VLM 视觉模型 API 调用的成本如何控制？', author: '刘强', avatar: '刘', dept: '产品中心', time: '昨天', tags: ['VLM', '成本'], views: 345, answers: 1, status: 'open', aiAnswered: false, summary: '每月图片处理量约 5 万张，VLM API 调用成本如何优化？是否需要本地部署？' },
      { id: 4, title: '知识库的权限体系如何设计才能满足多部门需求？', author: '陈静', avatar: '陈', dept: 'IT部', time: '昨天', tags: ['权限', '设计'], views: 289, answers: 4, status: 'resolved', aiAnswered: true, summary: '我们有 8 个部门，每个部门有独立知识库，但部分文档需要跨部门共享...' },
      { id: 5, title: 'Milvus 和 Qdrant 向量数据库的性能对比？', author: '赵伟', avatar: '赵', dept: '研发部', time: '2天前', tags: ['向量数据库', '性能'], views: 412, answers: 5, status: 'resolved', aiAnswered: true, summary: '10亿级向量检索场景下，Milvus 和 Qdrant 的 QPS 和延迟表现如何？' },
      { id: 6, title: 'RAG 系统中如何处理多模态文档（图片+表格）？', author: '孙莉', avatar: '孙', dept: '研发部', time: '3天前', tags: ['RAG', '多模态'], views: 178, answers: 0, status: 'open', aiAnswered: false, summary: '我们的文档中有大量图片和复杂表格，纯文本 RAG 效果不好，有什么方案？' },
      { id: 7, title: 'MCP 工具协议如何接入自定义的企业系统？', author: '周杰', avatar: '周', dept: '研发部', time: '4天前', tags: ['MCP', '集成'], views: 267, answers: 2, status: 'resolved', aiAnswered: false, summary: '想接入 OA 系统和 Jira，MCP 协议的接入流程是怎样的？' },
    ];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <div class="page-title">问答社区</div>
            <div class="page-subtitle">提问、解答、沉淀 — 让团队经验成为可检索的知识</div>
          </div>
          <button class="btn btn-primary" onclick="App.modal({title:'提问',body:'<div class=\\'form-group\\'><label class=\\'form-label\\'>问题标题</label><input class=\\'form-input\\' placeholder=\\'清晰描述你的问题\\'></div><div class=\\'form-group\\'><label class=\\'form-label\\'>问题详情</label><textarea class=\\'form-textarea\\' rows=\\'5\\' placeholder=\\'详细描述问题背景和你的尝试...\\'></textarea></div><div class=\\'form-group\\'><label class=\\'form-label\\'>标签</label><input class=\\'form-input\\' placeholder=\\'输入标签，逗号分隔\\'></div>',confirmText:'发布问题',onConfirm:(o)=>{o.remove();App.toast('问题已发布','success');}})">
            ${Utils.icon('plus', 16)} 提问
          </button>
        </div>
      </div>

      <!-- 统计 -->
      <div class="grid grid-4 gap-4 mb-5">
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--primary-bg);color:var(--primary);">${Utils.icon('message',20)}</div>
          </div>
          <div class="stat-card-value">1,247</div>
          <div class="stat-card-label">累计问题</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--warning-bg);color:var(--warning);">${Utils.icon('alert',20)}</div>
          </div>
          <div class="stat-card-value">23</div>
          <div class="stat-card-label">待解答</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--success-bg);color:var(--success);">${Utils.icon('check',20)}</div>
          </div>
          <div class="stat-card-value">94.2%</div>
          <div class="stat-card-label">解决率</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--info-bg);color:var(--info);">${Utils.icon('sparkles',20)}</div>
          </div>
          <div class="stat-card-value">68.5%</div>
          <div class="stat-card-label">AI 首答采纳率</div>
        </div>
      </div>

      <div class="flex gap-6">
        <!-- 左侧：问题列表 -->
        <div style="flex:1;min-width:0;">
          <!-- 筛选栏 -->
          <div class="flex items-center justify-between mb-4">
            <div class="tabs-pill">
              <div class="tab-pill active" data-qa-filter="all">全部</div>
              <div class="tab-pill" data-qa-filter="open">待解答 (2)</div>
              <div class="tab-pill" data-qa-filter="resolved">已解决 (5)</div>
              <div class="tab-pill" data-qa-filter="hot">热门</div>
            </div>
            <div class="flex items-center gap-2">
              <input class="form-input" style="width:200px;height:32px;font-size:13px;" placeholder="搜索问题...">
              <select class="form-select" style="width:auto;height:32px;font-size:13px;">
                <option>最新</option>
                <option>热门</option>
                <option>未回答</option>
              </select>
            </div>
          </div>

          <!-- 问题列表 -->
          <div class="card">
            ${questions.map((q, i) => `
              <div class="qa-item" onclick="App.navigate('knowledge/qa/${q.id}')" style="padding:20px;border-bottom:1px solid var(--border-light);cursor:pointer;transition:background 0.15s ease;" onmouseover="this.style.background='var(--surface-muted)'" onmouseout="this.style.background='transparent'">
                <div class="flex gap-4">
                  <!-- 统计列 -->
                  <div style="text-align:center;min-width:60px;flex-shrink:0;">
                    <div style="font-size:20px;font-weight:700;color:${q.answers > 0 ? 'var(--success)' : 'var(--text-muted)'};">${q.answers}</div>
                    <div style="font-size:11px;color:var(--text-muted);">回答</div>
                    <div style="font-size:14px;font-weight:600;color:var(--text-muted);margin-top:4px;">${q.views}</div>
                    <div style="font-size:11px;color:var(--text-muted);">浏览</div>
                  </div>
                  <!-- 内容列 -->
                  <div style="flex:1;min-width:0;">
                    <div class="flex items-center gap-2 mb-1">
                      <h3 class="text-md font-semibold" style="color:${q.status === 'open' ? 'var(--text)' : 'var(--primary)'};">${q.title}</h3>
                      ${q.status === 'open' ? '<span class="badge badge-warning">待解答</span>' : '<span class="badge badge-success">已解决</span>'}
                      ${q.aiAnswered ? '<span class="badge badge-primary">AI 已答</span>' : ''}
                    </div>
                    <p class="text-sm text-secondary mb-2" style="line-height:1.6;">${q.summary}</p>
                    <div class="flex items-center gap-3 flex-wrap">
                      ${q.tags.map(t => `<span class="tag">${t}</span>`).join('')}
                      <span class="text-xs text-muted ml-auto">
                        <div class="avatar avatar-sm" style="background:${Utils.avatarColor(q.author)};display:inline-flex;vertical-align:middle;margin-right:4px;">${q.avatar}</div>
                        ${q.author} · ${q.dept} · ${q.time}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            `).join('')}
          </div>

          <!-- 分页 -->
          <div class="flex items-center justify-between mt-4">
            <span class="text-sm text-muted">共 1,247 个问题</span>
            <div class="flex items-center gap-2">
              <button class="btn btn-secondary btn-sm" disabled>上一页</button>
              <button class="btn btn-primary btn-sm" style="min-width:32px;">1</button>
              <button class="btn btn-ghost btn-sm" style="min-width:32px;">2</button>
              <button class="btn btn-ghost btn-sm" style="min-width:32px;">3</button>
              <button class="btn btn-ghost btn-sm">...</button>
              <button class="btn btn-ghost btn-sm" style="min-width:32px;">42</button>
              <button class="btn btn-secondary btn-sm">下一页</button>
            </div>
          </div>
        </div>

        <!-- 右侧：热门标签 + 活跃用户 -->
        <aside style="width:260px;flex-shrink:0;">
          <!-- 热门标签 -->
          <div class="card card-shadow mb-4">
            <div class="card-header"><span class="card-title text-sm">${Utils.icon('tag', 14)} 热门标签</span></div>
            <div class="card-body">
              <div class="flex flex-wrap gap-2">
                ${Mock.tags.slice(0, 10).map(t => `<span class="tag">${t.name} <span class="text-muted">${t.count}</span></span>`).join('')}
              </div>
            </div>
          </div>

          <!-- 活跃贡献者 -->
          <div class="card card-shadow mb-4">
            <div class="card-header"><span class="card-title text-sm">${Utils.icon('star', 14)} 活跃贡献者</span></div>
            <div class="card-body">
              ${Mock.users.slice(0, 5).map((u, i) => `
                <div class="flex items-center gap-2 ${i < 4 ? 'mb-3 pb-3 border-b' : ''}">
                  <div style="width:20px;text-align:center;font-size:13px;font-weight:700;color:${i === 0 ? '#FFD700' : i === 1 ? '#C0C0C0' : i === 2 ? '#CD7F32' : 'var(--text-muted)'};">${i + 1}</div>
                  <div class="avatar avatar-sm" style="background:${Utils.avatarColor(u.name)}">${u.avatar}</div>
                  <div style="flex:1;min-width:0;">
                    <div class="text-sm font-medium truncate">${u.name}</div>
                    <div class="text-xs text-muted">${u.dept}</div>
                  </div>
                  <span class="badge badge-primary">${[234, 189, 156, 98, 67][i]} 回答</span>
                </div>
              `).join('')}
            </div>
          </div>

          <!-- AI 助手提示 -->
          <div class="card card-shadow" style="background:linear-gradient(135deg,var(--primary-bg),#F3E5F5);border:none;">
            <div class="card-body text-center">
              <div style="font-size:32px;margin-bottom:8px;">🤖</div>
              <div class="font-semibold text-sm mb-1">AI 先答，人工补充</div>
              <div class="text-xs text-muted mb-3">提问后 AI 会先基于知识库给出参考答案，团队成员可补充或修正</div>
              <button class="btn btn-primary btn-block btn-sm" onclick="App.navigate('chat')">
                ${Utils.icon('sparkles', 14)} 去 AI 对话
              </button>
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init(params) {
    // 筛选标签切换
    document.querySelectorAll('[data-qa-filter]').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('[data-qa-filter]').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        App.toast('筛选：' + tab.textContent.trim(), 'info');
      });
    });
  }
});
