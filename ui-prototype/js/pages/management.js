/* === 知识管理相关页面 === */

// ============================================================
// 页面1: 知识库管理 (路由 'manage/kb')
// ============================================================
App.registerPage('manage/kb', {
  title: '知识库管理',
  render() {
    // 每个知识库的知识覆盖度
    const coverage = [92, 78, 85, 88, 65, 73];

    return `
      <div class="page-header flex items-center justify-between">
        <div>
          <h1 class="page-title">知识库管理</h1>
          <p class="page-subtitle">管理企业知识库的文档、成员与配置</p>
        </div>
        <button class="btn btn-primary" onclick="App.navigate('manage/upload')">
          ${Utils.icon('plus', 16)} 新建知识库
        </button>
      </div>

      <!-- 视图切换 + 搜索 -->
      <div class="card card-shadow p-3 mb-5 flex items-center gap-3">
        <div class="tabs-pill">
          <div class="tab-pill active kb-view" data-view="grid">${Utils.icon('grid', 14)} 网格视图</div>
          <div class="tab-pill kb-view" data-view="list">${Utils.icon('menu', 14)} 列表视图</div>
        </div>
        <div class="divider-vertical"></div>
        <div class="topbar-search" style="width:260px;">
          <span class="topbar-search-icon">${Utils.icon('search', 16)}</span>
          <input type="text" id="kb-search" placeholder="搜索知识库..." />
        </div>
        <div class="ml-auto flex items-center gap-2">
          <span class="text-xs text-muted">共 ${Mock.knowledgeBases.length} 个知识库</span>
          <button class="btn btn-secondary btn-sm" onclick="App.toast('导出中...', 'info')">${Utils.icon('download', 14)} 导出</button>
        </div>
      </div>

      <!-- 知识库网格 -->
      <div class="grid grid-auto gap-4" id="kb-grid">
        ${Mock.knowledgeBases.map((kb, i) => `
          <div class="kb-card kb-manage" data-id="${kb.id}" data-name="${kb.name}">
            <div class="kb-card-banner" style="background: linear-gradient(135deg, ${kb.color}, ${kb.color}cc);position:relative;">
              ${kb.icon}
              <!-- 操作菜单 -->
              <div class="dropdown" style="position:absolute;top:8px;right:8px;" onclick="event.stopPropagation()">
                <button class="icon-btn kb-menu-btn" style="background:rgba(255,255,255,0.2);color:white;">${Utils.icon('more', 16)}</button>
                <div class="dropdown-menu">
                  <div class="dropdown-item kb-action" data-action="edit">${Utils.icon('edit', 14)} 编辑信息</div>
                  <div class="dropdown-item kb-action" data-action="members">${Utils.icon('users', 14)} 成员管理</div>
                  <div class="dropdown-item kb-action" data-action="settings">${Utils.icon('settings', 14)} 设置</div>
                  <div class="dropdown-divider"></div>
                  <div class="dropdown-item danger kb-action" data-action="delete">${Utils.icon('trash', 14)} 删除</div>
                </div>
              </div>
            </div>
            <div class="kb-card-body">
              <div class="flex items-center justify-between mb-1">
                <div class="kb-card-title">${kb.name}</div>
                <span class="badge badge-primary">${kb.docs}</span>
              </div>
              <div class="kb-card-desc">${kb.desc}</div>
              <div class="kb-card-stats mb-3">
                <span class="flex items-center gap-1">${Utils.icon('doc', 12)} ${kb.docs} 文档</span>
                <span class="flex items-center gap-1">${Utils.icon('users', 12)} ${kb.members} 成员</span>
                <span class="flex items-center gap-1">${Utils.icon('clock', 12)} ${kb.updatedAt}</span>
              </div>
              <!-- 知识覆盖度进度条 -->
              <div class="mb-1 flex items-center justify-between">
                <span class="text-xs text-muted">知识覆盖度</span>
                <span class="text-xs font-medium text-${coverage[i] >= 85 ? 'success' : coverage[i] >= 75 ? 'warning' : 'danger'}">${coverage[i]}%</span>
              </div>
              <div class="progress-bar">
                <div class="progress-bar-fill ${coverage[i] >= 85 ? 'success' : coverage[i] >= 75 ? 'warning' : 'danger'}" style="width:${coverage[i]}%;"></div>
              </div>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  },
  init() {
    // 视图切换
    document.querySelectorAll('.kb-view').forEach(v => {
      v.addEventListener('click', () => {
        document.querySelectorAll('.kb-view').forEach(x => x.classList.remove('active'));
        v.classList.add('active');
        const grid = document.getElementById('kb-grid');
        if (v.dataset.view === 'list') {
          grid.style.gridTemplateColumns = '1fr';
        } else {
          grid.style.gridTemplateColumns = 'repeat(auto-fill, minmax(280px, 1fr))';
        }
        App.toast('已切换为' + v.textContent.trim() + '视图', 'info');
      });
    });

    // 搜索过滤
    document.getElementById('kb-search').addEventListener('input', (e) => {
      const kw = e.target.value.trim().toLowerCase();
      document.querySelectorAll('.kb-manage').forEach(card => {
        const name = card.dataset.name.toLowerCase();
        card.style.display = name.includes(kw) ? '' : 'none';
      });
    });

    // 操作菜单切换
    document.querySelectorAll('.kb-menu-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const dropdown = btn.closest('.dropdown');
        document.querySelectorAll('.dropdown').forEach(d => { if (d !== dropdown) d.classList.remove('open'); });
        dropdown.classList.toggle('open');
      });
    });
    document.addEventListener('click', () => {
      document.querySelectorAll('.dropdown.open').forEach(d => d.classList.remove('open'));
    });

    // 菜单项操作
    document.querySelectorAll('.kb-action').forEach(item => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        const action = item.dataset.action;
        const card = item.closest('.kb-manage');
        const kbName = card.dataset.name;
        const kb = Mock.knowledgeBases.find(k => k.name === kbName);

        if (action === 'edit') {
          this.openEditModal(kb);
        } else if (action === 'members') {
          this.openMembersModal(kb);
        } else if (action === 'settings') {
          this.openSettingsModal(kb);
        } else if (action === 'delete') {
          App.modal({
            title: '确认删除',
            body: `<p>确定要删除知识库「${kbName}」吗？此操作不可撤销，将一并删除 ${kb.docs} 篇文档。</p>`,
            confirmText: '确认删除',
            onConfirm: (overlay) => {
              card.style.opacity = '0.5';
              card.style.pointerEvents = 'none';
              overlay.remove();
              App.toast('知识库已删除（原型演示）', 'success');
            }
          });
        }
        document.querySelectorAll('.dropdown.open').forEach(d => d.classList.remove('open'));
      });
    });

    // 点击卡片打开管理模态框
    document.querySelectorAll('.kb-manage').forEach(card => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('.dropdown') || e.target.closest('.kb-action')) return;
        const kb = Mock.knowledgeBases.find(k => k.name === card.dataset.name);
        this.openManageModal(kb);
      });
    });
  },
  // 打开管理模态框（文档列表/成员列表/设置）
  openManageModal(kb) {
    const docs = Mock.documents.filter(d => d.kb === kb.name);
    const displayDocs = docs.length ? docs : Mock.documents.slice(0, 4);
    const members = Mock.users.slice(0, kb.members > 8 ? 8 : kb.members);

    App.modal({
      title: `${kb.icon} ${kb.name}`,
      size: 'modal-xl',
      footer: false,
      body: `
        <div class="tabs mb-4">
          <div class="tab active" data-tab="docs">${Utils.icon('doc', 14)} 文档列表 (${kb.docs})</div>
          <div class="tab" data-tab="members">${Utils.icon('users', 14)} 成员列表 (${kb.members})</div>
          <div class="tab" data-tab="settings">${Utils.icon('settings', 14)} 设置</div>
        </div>
        <div id="kb-tab-docs" class="tab-content active">
          <div class="flex items-center gap-2 mb-3">
            <button class="btn btn-primary btn-sm" onclick="App.navigate('manage/upload')">${Utils.icon('plus', 14)} 上传文档</button>
            <button class="btn btn-secondary btn-sm">${Utils.icon('refresh', 14)} 同步</button>
            <span class="ml-auto text-xs text-muted">显示 ${displayDocs.length} / ${kb.docs} 篇</span>
          </div>
          <div class="table-wrap">
            <table class="data-table">
              <thead><tr><th>文档标题</th><th>类型</th><th>作者</th><th>更新时间</th><th>阅读量</th><th>状态</th></tr></thead>
              <tbody>
                ${displayDocs.map(d => `
                  <tr>
                    <td><a href="#knowledge/doc/${d.id}">${d.title}</a></td>
                    <td><span class="badge badge-info">${Utils.fileTypeLabel(d.type)}</span></td>
                    <td>${d.author}</td>
                    <td>${d.updatedAt}</td>
                    <td>${d.views}</td>
                    <td><span class="badge badge-${d.status === 'published' ? 'success' : 'warning'}">${d.status === 'published' ? '已发布' : '审核中'}</span></td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>
        <div id="kb-tab-members" class="tab-content" style="display:none;">
          <div class="flex items-center gap-2 mb-3">
            <button class="btn btn-primary btn-sm">${Utils.icon('plus', 14)} 邀请成员</button>
            <span class="ml-auto text-xs text-muted">${members.length} 位成员</span>
          </div>
          <div class="grid grid-2 gap-3">
            ${members.map(m => `
              <div class="list-item" style="border:1px solid var(--border-light);">
                <div class="avatar avatar-md" style="background:${Utils.avatarColor(m.name)}">${m.avatar}</div>
                <div class="flex-1">
                  <div class="font-medium text-sm">${m.name}</div>
                  <div class="text-xs text-muted">${m.dept} · ${m.lastActive}</div>
                </div>
                <span class="badge badge-${m.role === '管理员' ? 'primary' : m.role === '编辑者' ? 'info' : 'neutral'}">${m.role}</span>
              </div>
            `).join('')}
          </div>
        </div>
        <div id="kb-tab-settings" class="tab-content" style="display:none;">
          <div class="form-group">
            <label class="form-label">知识库名称</label>
            <input class="form-input" value="${kb.name}">
          </div>
          <div class="form-group">
            <label class="form-label">描述</label>
            <textarea class="form-textarea">${kb.desc}</textarea>
          </div>
          <div class="grid grid-2 gap-3">
            <div class="form-group">
              <label class="form-label">访问权限</label>
              <select class="form-select"><option>部门可见</option><option>全员可见</option><option>仅邀请</option></select>
            </div>
            <div class="form-group">
              <label class="form-label">AI 索引</label>
              <div class="flex items-center gap-3">
                <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                <span class="text-sm">允许 AI 检索</span>
              </div>
            </div>
          </div>
          <button class="btn btn-primary btn-block" onclick="App.toast('设置已保存', 'success')">保存设置</button>
        </div>
      `,
      onMount: (overlay) => {
        // 模态框内 Tab 切换
        overlay.querySelectorAll('.tab').forEach(tab => {
          tab.addEventListener('click', () => {
            overlay.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            overlay.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
            overlay.querySelector('#kb-tab-' + tab.dataset.tab).style.display = 'block';
          });
        });
      }
    });
  },
  openEditModal(kb) {
    App.modal({
      title: '编辑知识库',
      body: `
        <div class="form-group"><label class="form-label">名称</label><input class="form-input" value="${kb.name}"></div>
        <div class="form-group"><label class="form-label">描述</label><textarea class="form-textarea">${kb.desc}</textarea></div>
        <div class="form-group"><label class="form-label">图标</label><input class="form-input" value="${kb.icon}" style="font-size:20px;"></div>
      `,
      confirmText: '保存',
      onConfirm: (overlay) => { overlay.remove(); App.toast('知识库信息已更新', 'success'); }
    });
  },
  openMembersModal(kb) {
    App.modal({
      title: `成员管理 - ${kb.name}`,
      body: `<p class="text-sm text-muted mb-4">当前共 ${kb.members} 位成员，可在此邀请新成员或调整角色权限。</p>
        <div class="form-input-group mb-4">
          <input class="form-input" placeholder="输入邮箱邀请成员">
          <button class="btn btn-primary" onclick="App.toast('邀请已发送', 'success')">发送邀请</button>
        </div>`,
      confirmText: '完成'
    });
  },
  openSettingsModal(kb) {
    App.modal({
      title: `设置 - ${kb.name}`,
      body: `
        <div class="form-group"><label class="form-label">访问权限</label><select class="form-select"><option>部门可见</option><option>全员可见</option></select></div>
        <div class="flex items-center justify-between mb-4"><span>允许 AI 检索</span><label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label></div>
        <div class="flex items-center justify-between mb-4"><span>自动摘要</span><label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label></div>
        <div class="flex items-center justify-between"><span>文档版本历史</span><label class="switch"><input type="checkbox"><span class="switch-slider"></span></label></div>
      `,
      confirmText: '保存设置',
      onConfirm: (overlay) => { overlay.remove(); App.toast('设置已保存', 'success'); }
    });
  }
});

// ============================================================
// 页面2: 文档上传 (路由 'manage/upload')
// ============================================================
App.registerPage('manage/upload', {
  title: '文档上传',
  render() {
    // 上传文件列表
    const files = [
      { name: '微服务架构设计规范_v2.pdf', size: '2.4 MB', type: 'pdf', status: '完成', progress: 100, color: 'success' },
      { name: '2026年Q3产品路线图.docx', size: '1.8 MB', type: 'docx', status: '向量化中', progress: 75, color: 'primary' },
      { name: 'API接口数据字典.xlsx', size: '856 KB', type: 'xlsx', status: '解析中', progress: 45, color: 'warning' },
      { name: '季度财务报告.xlsx', size: '1.2 MB', type: 'xlsx', status: '等待中', progress: 0, color: 'info' },
      { name: '产品培训视频.mp4', size: '45.6 MB', type: 'video', status: 'ASR 转写中', progress: 30, color: 'warning' },
      { name: '客户访谈录音.mp3', size: '12.3 MB', type: 'audio', status: '等待中', progress: 0, color: 'info' },
      { name: '部署手册.md', size: '124 KB', type: 'md', status: '索引中', progress: 90, color: 'info' },
    ];

    return `
      <div class="page-header">
        <h1 class="page-title">文档上传</h1>
        <p class="page-subtitle">支持批量上传，自动解析、向量化并建立索引</p>
      </div>

      <div class="flex gap-5">
        <!-- 左侧上传区域 -->
        <div class="flex-1" style="min-width:0;">
          <!-- 大型上传区域 -->
          <div class="upload-zone mb-5" id="upload-zone">
            <div class="upload-zone-icon">📁</div>
            <div class="upload-zone-title">拖拽文件到此处，或点击选择文件</div>
            <div class="upload-zone-hint">支持 PDF、Word、Excel、PPT、Markdown、图片、视频、音频等格式，单个文件最大 100MB</div>
            <button class="btn btn-primary mt-4" onclick="App.toast('文件选择器（原型演示）', 'info')">${Utils.icon('upload', 16)} 选择文件</button>
          </div>

          <!-- 上传文件处理列表 -->
          <div class="card card-shadow">
            <div class="card-header">
              <span class="card-title">${Utils.icon('fileText', 16)} 文件处理列表</span>
              <span class="badge badge-primary">${files.length} 个文件</span>
            </div>
            <div class="card-body">
              ${files.map((f, i) => `
                <div class="upload-file-item" style="display:flex;align-items:center;gap:12px;padding:12px 0;${i < files.length - 1 ? 'border-bottom:1px solid var(--border-light);' : ''}">
                  <div class="doc-item-icon" style="width:40px;height:40px;background:var(--primary-bg);color:var(--primary);font-size:20px;">${Utils.fileIcon(f.type)}</div>
                  <div class="flex-1" style="min-width:0;">
                    <div class="flex items-center justify-between mb-1">
                      <span class="text-sm font-medium truncate">${f.name}</span>
                      <span class="text-xs text-muted ml-2">${f.size}</span>
                    </div>
                    <!-- 处理进度条 -->
                    <div class="progress-bar mb-1">
                      <div class="progress-bar-fill ${f.color}" style="width:${f.progress}%;"></div>
                    </div>
                    <div class="flex items-center justify-between">
                      <span class="badge badge-${f.color}">${f.status}</span>
                      <span class="text-xs text-muted">${f.progress}%</span>
                    </div>
                  </div>
                  <button class="icon-btn" onclick="this.closest('.upload-file-item').style.display='none';App.toast('已移除文件', 'info')">${Utils.icon('close', 16)}</button>
                </div>
              `).join('')}

              <!-- 上传完成提示 -->
              <div class="mt-4 p-4" style="background:var(--success-bg);border-radius:8px;display:flex;align-items:center;gap:12px;">
                <span style="font-size:24px;">✅</span>
                <div class="flex-1">
                  <div class="font-medium text-sm text-success">1 个文件已成功上传并完成索引</div>
                  <a href="#knowledge/doc/2" class="text-xs">查看文档 →</a>
                </div>
              </div>
            </div>
            <div class="card-footer">
              <span class="text-xs text-muted">支持并发上传 5 个文件</span>
              <button class="btn btn-primary" onclick="App.toast('已开始处理上传队列', 'success')">${Utils.icon('upload', 16)} 开始上传</button>
            </div>
          </div>
        </div>

        <!-- 右侧元数据表单 -->
        <aside style="width:320px;flex-shrink:0;">
          <div class="card card-shadow" style="position:sticky;top:72px;">
            <div class="card-header"><span class="card-title">${Utils.icon('settings', 16)} 元数据设置</span></div>
            <div class="card-body">
              <!-- 目标知识库 -->
              <div class="form-group">
                <label class="form-label">目标知识库 <span class="required">*</span></label>
                <select class="form-select">
                  ${Mock.knowledgeBases.map(kb => `<option ${kb.id === 1 ? 'selected' : ''}>${kb.icon} ${kb.name}</option>`).join('')}
                </select>
              </div>

              <!-- 文档标签 -->
              <div class="form-group">
                <label class="form-label">文档标签</label>
                <div class="flex flex-wrap gap-2 mb-2" id="upload-tags">
                  <span class="tag tag-removable">架构设计<span class="tag-close" onclick="this.parentElement.remove()">×</span></span>
                  <span class="tag tag-removable">微服务<span class="tag-close" onclick="this.parentElement.remove()">×</span></span>
                </div>
                <input class="form-input" placeholder="输入标签后回车" id="upload-tag-input">
                <div class="form-hint">建议标签：产品规划、技术方案、运维SOP</div>
              </div>

              <!-- 权限范围 -->
              <div class="form-group">
                <label class="form-label">权限范围</label>
                <div class="flex flex-col gap-2">
                  <label class="radio"><input type="radio" name="scope" checked> <span>公开 - 全员可读</span></label>
                  <label class="radio"><input type="radio" name="scope"> <span>部门 - 仅本部门可见</span></label>
                  <label class="radio"><input type="radio" name="scope"> <span>私密 - 仅自己可见</span></label>
                </div>
              </div>

              <div class="divider"></div>

              <!-- 处理选项 -->
              <div class="form-group" style="margin-bottom:12px;">
                <div class="flex items-center justify-between">
                  <div>
                    <div class="text-sm font-medium">自动摘要</div>
                    <div class="text-xs text-muted">AI 生成文档摘要</div>
                  </div>
                  <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                </div>
              </div>
              <div class="form-group" style="margin-bottom:12px;">
                <div class="flex items-center justify-between">
                  <div>
                    <div class="text-sm font-medium">VLM 视觉理解</div>
                    <div class="text-xs text-muted">图片/文档内嵌图片 VLM 描述</div>
                  </div>
                  <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                </div>
              </div>
              <div class="form-group" style="margin-bottom:12px;">
                <div class="flex items-center justify-between">
                  <div>
                    <div class="text-sm font-medium">音频转写</div>
                    <div class="text-xs text-muted">音频/视频 ASR 语音转文本</div>
                  </div>
                  <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                </div>
              </div>
              <div class="form-group" style="margin-bottom:0;">
                <div class="flex items-center justify-between">
                  <div>
                    <div class="text-sm font-medium">向量化</div>
                    <div class="text-xs text-muted">建立语义检索索引</div>
                  </div>
                  <label class="switch"><input type="checkbox" checked><span class="switch-slider"></span></label>
                </div>
              </div>
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init() {
    // 拖拽上传交互
    const zone = document.getElementById('upload-zone');
    ['dragenter', 'dragover'].forEach(evt => {
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add('dragover'); });
    });
    ['dragleave', 'drop'].forEach(evt => {
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove('dragover'); });
    });
    zone.addEventListener('drop', () => { App.toast('文件已添加到上传队列', 'success'); });
    zone.addEventListener('click', () => { App.toast('文件选择器（原型演示）', 'info'); });

    // 标签输入回车添加
    const tagInput = document.getElementById('upload-tag-input');
    tagInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && tagInput.value.trim()) {
        e.preventDefault();
        const tagWrap = document.getElementById('upload-tags');
        const tag = document.createElement('span');
        tag.className = 'tag tag-removable';
        tag.innerHTML = `${tagInput.value.trim()}<span class="tag-close">×</span>`;
        tag.querySelector('.tag-close').onclick = () => tag.remove();
        tagWrap.appendChild(tag);
        tagInput.value = '';
        App.toast('标签已添加', 'success');
      }
    });

    // 模拟进度条增长
    const bars = document.querySelectorAll('.upload-file-item .progress-bar-fill');
    bars.forEach((bar, i) => {
      if (parseInt(bar.style.width) < 100) {
        const timer = setInterval(() => {
          let w = parseInt(bar.style.width) || 0;
          if (w >= 100) { clearInterval(timer); return; }
          w += Math.random() * 8;
          if (w > 100) w = 100;
          bar.style.width = w + '%';
          bar.parentElement.nextElementSibling.querySelector('.text-xs').textContent = Math.round(w) + '%';
        }, 1500 + i * 300);
      }
    });
  }
});

// ============================================================
// 页面3: 协同编辑 (路由 'manage/editor')
// ============================================================
App.registerPage('manage/editor', {
  title: '协同编辑',
  render() {
    // 工具栏按钮
    const toolbarBtns = [
      { icon: 'edit', label: '加粗', cmd: 'bold', active: true },
      { icon: 'edit', label: '斜体', cmd: 'italic' },
      { icon: 'edit', label: '下划线', cmd: 'underline' },
    ];
    const headingBtns = [
      { label: 'H1', cmd: 'h1' },
      { label: 'H2', cmd: 'h2' },
      { label: 'H3', cmd: 'h3' },
    ];
    const insertBtns = [
      { icon: 'link', label: '链接' },
      { icon: 'image', label: '图片' },
      { icon: 'fileText', label: '代码块' },
      { icon: 'grid', label: '表格' },
    ];
    // 协作者
    const collaborators = [
      { name: '张明', avatar: '张', color: '#4B3FE3' },
      { name: '李华', avatar: '李', color: '#00B884' },
      { name: '陈静', avatar: '陈', color: '#FF9500' },
    ];
    // 版本历史
    const versions = [
      { ver: 'v2.3', author: '张明', time: '刚刚', desc: '补充监控告警章节', current: true },
      { ver: 'v2.2', author: '李华', time: '2小时前', desc: '更新服务拆分原则', current: false },
      { ver: 'v2.1', author: '陈静', time: '昨天', desc: '新增部署策略', current: false },
      { ver: 'v2.0', author: '张明', time: '3天前', desc: '初版架构文档', current: false },
    ];

    return `
      <div class="page-header flex items-center justify-between">
        <div>
          <h1 class="page-title">协同编辑</h1>
          <p class="page-subtitle">多人实时协作编辑文档，自动保存与版本管理</p>
        </div>
        <button class="btn btn-primary" onclick="App.toast('已生成分享链接', 'success')">${Utils.icon('share', 16)} 分享</button>
      </div>

      <div class="flex gap-5">
        <!-- 编辑器主体 -->
        <div class="flex-1" style="min-width:0;">
          <div class="collab-editor">
            <!-- 工具栏 -->
            <div class="collab-toolbar">
              <button class="collab-toolbar-btn" data-cmd="undo" title="撤销">${Utils.icon('arrowLeft', 16)}</button>
              <button class="collab-toolbar-btn" data-cmd="redo" title="重做">${Utils.icon('arrowRight', 16)}</button>
              <span class="collab-toolbar-divider"></span>
              <button class="collab-toolbar-btn ${toolbarBtns[0].active ? 'active' : ''}" data-cmd="bold" title="加粗"><b>B</b></button>
              <button class="collab-toolbar-btn" data-cmd="italic" title="斜体"><i>I</i></button>
              <button class="collab-toolbar-btn" data-cmd="underline" title="下划线"><u>U</u></button>
              <span class="collab-toolbar-divider"></span>
              ${headingBtns.map(h => `<button class="collab-toolbar-btn" data-cmd="${h.cmd}" title="${h.label}" style="font-weight:600;">${h.label}</button>`).join('')}
              <span class="collab-toolbar-divider"></span>
              <button class="collab-toolbar-btn" data-cmd="ul" title="无序列表">•</button>
              <button class="collab-toolbar-btn" data-cmd="ol" title="有序列表">1.</button>
              <button class="collab-toolbar-btn" data-cmd="quote" title="引用">"</button>
              <span class="collab-toolbar-divider"></span>
              ${insertBtns.map(b => `<button class="collab-toolbar-btn" title="${b.label}">${Utils.icon(b.icon, 16)}</button>`).join('')}
              <div class="ml-auto flex items-center gap-2">
                <!-- 在线协作者头像 -->
                <div class="avatar-group">
                  ${collaborators.map(c => `<div class="avatar avatar-sm" style="background:${c.color};" title="${c.name}">${c.avatar}</div>`).join('')}
                </div>
                <span class="text-xs text-muted">${collaborators.length} 人在线</span>
              </div>
            </div>

            <!-- 编辑区域 -->
            <div style="position:relative;">
              <div class="collab-content" id="collab-content" contenteditable="true">
                <h1 style="font-size:24px;font-weight:700;margin-bottom:16px;">微服务架构设计规范 v2.0</h1>
                <p style="margin-bottom:16px;">本文档定义了基于 APISIX 网关的微服务架构设计规范，适用于公司所有微服务项目的开发与部署。</p>

                <h2 style="font-size:18px;font-weight:600;margin:20px 0 10px;">1. 整体架构</h2>
                <p style="margin-bottom:16px;">系统采用前后端分离架构，前端通过 Nginx 部署静态资源，后端服务通过 APISIX 网关统一对外暴露 API。</p>

                <h2 style="font-size:18px;font-weight:600;margin:20px 0 10px;">2. 服务拆分原则</h2>
                <ul style="padding-left:24px;list-style:disc;margin-bottom:16px;">
                  <li>按业务领域拆分，每个服务对应一个限界上下文</li>
                  <li>服务间通过 gRPC 通信，对外提供 RESTful API</li>
                  <li>每个服务拥有独立的数据存储</li>
                </ul>

                <h2 style="font-size:18px;font-weight:600;margin:20px 0 10px;">3. 通信协议</h2>
                <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
                  <thead><tr><th style="border:1px solid var(--border);padding:8px;background:var(--surface-muted);text-align:left;">场景</th><th style="border:1px solid var(--border);padding:8px;background:var(--surface-muted);text-align:left;">协议</th><th style="border:1px solid var(--border);padding:8px;background:var(--surface-muted);text-align:left;">说明</th></tr></thead>
                  <tbody>
                    <tr><td style="border:1px solid var(--border);padding:8px;">服务间</td><td style="border:1px solid var(--border);padding:8px;">gRPC</td><td style="border:1px solid var(--border);padding:8px;">低延迟、高吞吐</td></tr>
                    <tr><td style="border:1px solid var(--border);padding:8px;">对外 API</td><td style="border:1px solid var(--border);padding:8px;">HTTP/JSON</td><td style="border:1px solid var(--border);padding:8px;">浏览器友好</td></tr>
                    <tr><td style="border:1px solid var(--border);padding:8px;">事件通知</td><td style="border:1px solid var(--border);padding:8px;">Kafka</td><td style="border:1px solid var(--border);padding:8px;">异步解耦</td></tr>
                  </tbody>
                </table>

                <h2 style="font-size:18px;font-weight:600;margin:20px 0 10px;">4. 部署策略</h2>
                <p style="margin-bottom:16px;">采用 Kubernetes 容器化部署，支持蓝绿部署与金丝雀发布，确保零停机上线。</p>
              </div>

              <!-- 模拟协作者光标 -->
              <div class="collab-cursor" style="left:120px;top:180px;background:#00B884;">
                <div class="collab-cursor-label" style="background:#00B884;">李华</div>
              </div>
              <div class="collab-cursor" style="left:280px;top:340px;background:#FF9500;">
                <div class="collab-cursor-label" style="background:#FF9500;">陈静</div>
              </div>
            </div>

            <!-- 底部状态栏 -->
            <div class="collab-toolbar" style="border-top:1px solid var(--border-light);border-bottom:none;">
              <span class="text-xs text-muted">${Utils.icon('check', 12)} 已保存 · 2 秒前</span>
              <span class="collab-toolbar-divider"></span>
              <span class="text-xs text-muted">字数：1,248</span>
              <span class="collab-toolbar-divider"></span>
              <span class="text-xs text-muted">阅读时间：约 5 分钟</span>
              <div class="ml-auto flex items-center gap-2">
                <span class="text-xs text-success">${Utils.icon('users', 12)} ${collaborators.length} 人在线</span>
                <span class="collab-toolbar-divider"></span>
                <span class="text-xs text-muted">v2.3</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 右侧版本历史 -->
        <aside style="width:300px;flex-shrink:0;">
          <div class="card card-shadow" style="position:sticky;top:72px;">
            <div class="card-header">
              <span class="card-title">${Utils.icon('clock', 16)} 版本历史</span>
              <button class="btn-link" onclick="App.toast('已展开全部历史', 'info')">查看全部</button>
            </div>
            <div class="card-body">
              ${versions.map((v, i) => `
                <div class="audit-step" style="padding-bottom:16px;${i === versions.length - 1 ? 'padding-bottom:0;' : ''}">
                  <div class="audit-step-icon ${v.current ? 'current' : 'done'}">${v.current ? '●' : Utils.icon('check', 12)}</div>
                  <div class="audit-step-content">
                    <div class="flex items-center gap-2">
                      <span class="audit-step-title">${v.ver}</span>
                      ${v.current ? '<span class="badge badge-primary">当前</span>' : ''}
                    </div>
                    <div class="audit-step-desc">${v.author} · ${v.time}</div>
                    <div class="text-xs text-secondary mt-1" style="line-height:1.5;">${v.desc}</div>
                    ${!v.current ? `
                      <div class="flex gap-2 mt-2">
                        <button class="btn btn-secondary btn-sm" onclick="App.toast('版本对比中...', 'info')">${Utils.icon('eye', 12)} 对比</button>
                        <button class="btn btn-secondary btn-sm" onclick="App.toast('已恢复到 ${v.ver}', 'success')">${Utils.icon('refresh', 12)} 恢复</button>
                      </div>
                    ` : ''}
                  </div>
                </div>
              `).join('')}
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init() {
    // 工具栏按钮交互
    document.querySelectorAll('.collab-toolbar-btn[data-cmd]').forEach(btn => {
      btn.addEventListener('click', () => {
        btn.classList.toggle('active');
        const cmd = btn.dataset.cmd;
        if (['bold', 'italic', 'underline'].includes(cmd)) {
          try { document.execCommand(cmd, false, null); } catch (e) {}
        }
        App.toast('已应用 ' + (btn.title || cmd), 'info');
      });
    });

    // 内容编辑保存提示
    const content = document.getElementById('collab-content');
    let saveTimer;
    content.addEventListener('input', () => {
      clearTimeout(saveTimer);
      const status = content.parentElement.parentElement.querySelector('.collab-toolbar:last-child .text-muted');
      if (status) status.textContent = '编辑中...';
      saveTimer = setTimeout(() => {
        const stat = content.parentElement.parentElement.querySelector('.collab-toolbar:last-child .text-muted');
        if (stat) stat.innerHTML = `${Utils.icon('check', 12)} 已保存 · 刚刚`;
        App.toast('文档已自动保存', 'success');
      }, 2000);
    });

    // 模拟协作者光标移动
    const cursors = document.querySelectorAll('.collab-cursor');
    cursors.forEach((cursor, i) => {
      let x = parseInt(cursor.style.left);
      let y = parseInt(cursor.style.top);
      setInterval(() => {
        x += (Math.random() - 0.5) * 20;
        y += (Math.random() - 0.5) * 10;
        x = Math.max(40, Math.min(600, x));
        y = Math.max(150, Math.min(400, y));
        cursor.style.left = x + 'px';
        cursor.style.top = y + 'px';
      }, 2000 + i * 500);
    });
  }
});

// ============================================================
// 页面4: 会议纪要 (路由 'manage/minutes')
// ============================================================
App.registerPage('manage/minutes', {
  title: '会议纪要',
  render() {
    // 会议纪要数据
    const meetings = [
      {
        id: 1, title: 'Q3 产品规划评审会', date: '2026-07-04 14:00', duration: '1h 30m',
        location: '会议室 A · 线下', host: '张明', status: 'published',
        attendees: ['张明', '李华', '王芳', '陈静'],
        tags: ['产品规划', '评审'],
        summary: '会议围绕 Q3 产品路线图展开讨论，确定了三大核心方向的优先级，明确了各功能模块的负责人与交付节点。重点关注微服务架构升级和 AI 能力增强。',
        topics: [
          { title: 'Q3 核心方向确认', points: '讨论了微服务升级、RAG 优化、Agent 化三大方向的优先级', decision: '确定优先级：RAG 优化 > 微服务升级 > Agent 化', todos: [{ task: '输出详细路线图', owner: '张明', due: '07-10' }] },
          { title: '资源分配', points: '研发资源紧张，需要协调外部支持', decision: '从市场部借调 2 名前端工程师支援', todos: [{ task: '协调人员到岗', owner: '陈静', due: '07-08' }, { task: '安排工作环境', owner: '李华', due: '07-09' }] },
        ],
        audioDuration: '01:32:00',
      },
      {
        id: 2, title: '微服务架构技术评审', date: '2026-07-03 10:00', duration: '2h',
        location: '线上 · 飞书会议', host: '李华', status: 'published',
        attendees: ['李华', '张明', '陈静'],
        tags: ['技术方案', '架构'],
        summary: '评审微服务架构设计规范 v2.0，重点讨论了服务拆分边界、APISIX 网关配置策略和监控告警方案，达成一致意见。',
        topics: [
          { title: '服务拆分边界', points: '订单与支付服务的边界存在重叠', decision: '支付状态查询归入订单服务，支付动作归支付服务', todos: [{ task: '更新服务边界文档', owner: '李华', due: '07-05' }] },
        ],
        audioDuration: '02:00:00',
      },
      {
        id: 3, title: '新员工 Onboarding 培训', date: '2026-07-02 09:30', duration: '1h',
        location: '会议室 B · 线下', host: '孙莉', status: 'draft',
        attendees: ['孙莉', '王芳'],
        tags: ['培训', '入职'],
        summary: '为新入职员工介绍公司知识库的使用方法、AI 对话能力和协作流程，演示了文档上传与搜索功能。',
        topics: [],
        audioDuration: '01:00:00',
      },
      {
        id: 4, title: '客户 A 智能客服项目复盘', date: '2026-07-01 15:00', duration: '1h 15m',
        location: '线上 · 腾讯会议', host: '王芳', status: 'published',
        attendees: ['王芳', '张明', '李华', '陈静', '孙莉'],
        tags: ['客户案例', '复盘'],
        summary: '复盘客户 A 智能客服系统上线情况，系统运行稳定，客户满意度 92%，总结了可复用的最佳实践并讨论了改进点。',
        topics: [],
        audioDuration: '01:15:00',
      },
    ];

    return `
      <div class="page-header flex items-center justify-between">
        <div>
          <h1 class="page-title">会议纪要</h1>
          <p class="page-subtitle">AI 自动生成会议纪要，支持待办提取与音视频回放</p>
        </div>
        <button class="btn btn-primary" onclick="App.toast('开始录音（原型演示）', 'info')">${Utils.icon('plus', 16)} 新建纪要</button>
      </div>

      <!-- 会议列表 -->
      <div class="flex flex-col gap-4" id="minutes-list">
        ${meetings.map(m => `
          <div class="card card-shadow minutes-card" data-id="${m.id}">
            <div class="card-body">
              <div class="flex items-start gap-4">
                <!-- 会议图标 -->
                <div class="stat-card-icon" style="background:var(--primary-bg);color:var(--primary);width:48px;height:48px;flex-shrink:0;">
                  ${Utils.icon('video', 24)}
                </div>
                <div class="flex-1" style="min-width:0;">
                  <div class="flex items-center gap-2 mb-2 flex-wrap">
                    <span class="card-title">${m.title}</span>
                    <span class="badge badge-${m.status === 'published' ? 'success' : 'warning'}">${m.status === 'published' ? '已发布' : '草稿'}</span>
                    ${m.tags.map(t => `<span class="tag" style="font-size:11px;">${t}</span>`).join('')}
                  </div>
                  <div class="flex items-center gap-4 text-xs text-muted mb-3 flex-wrap">
                    <span class="flex items-center gap-1">${Utils.icon('calendar', 12)} ${m.date}</span>
                    <span class="flex items-center gap-1">${Utils.icon('clock', 12)} ${m.duration}</span>
                    <span class="flex items-center gap-1">${Utils.icon('building', 12)} ${m.location}</span>
                    <span class="flex items-center gap-1">${Utils.icon('users', 12)} 主持人：${m.host}</span>
                  </div>
                  <!-- 参会人 -->
                  <div class="flex items-center gap-3 mb-3">
                    <div class="avatar-group">
                      ${m.attendees.map(a => `<div class="avatar avatar-sm" style="background:${Utils.avatarColor(a)}">${a.charAt(0)}</div>`).join('')}
                    </div>
                    <span class="text-xs text-muted">${m.attendees.length} 位参会人</span>
                  </div>
                  <!-- AI 摘要 -->
                  <div class="p-3 mb-3" style="background:var(--primary-bg);border-radius:8px;border-left:3px solid var(--primary);">
                    <div class="flex items-center gap-2 mb-1">
                      ${Utils.icon('sparkles', 14)}
                      <span class="text-sm font-semibold text-primary">AI 摘要</span>
                    </div>
                    <div class="text-sm text-secondary" style="line-height:1.7;">${m.summary}</div>
                  </div>
                  <!-- 音视频回放时间轴 -->
                  <div class="flex items-center gap-3">
                    <span class="text-xs text-muted">${Utils.icon('mic', 12)} 音频回放</span>
                    <div class="flex-1" style="height:6px;background:var(--surface-hover);border-radius:3px;position:relative;cursor:pointer;">
                      <div style="position:absolute;left:0;top:0;height:100%;width:35%;background:var(--primary);border-radius:3px;"></div>
                      <div style="position:absolute;left:35%;top:-3px;width:12px;height:12px;border-radius:50%;background:var(--primary);transform:translateX(-50%);"></div>
                    </div>
                    <span class="text-xs text-muted">${m.audioDuration}</span>
                  </div>
                </div>
                <div class="flex flex-col gap-2">
                  <button class="btn btn-primary btn-sm minutes-expand" data-id="${m.id}">${Utils.icon('eye', 14)} 查看详情</button>
                  <button class="btn btn-secondary btn-sm" onclick="App.toast('已提取待办事项', 'success')">${Utils.icon('check', 14)} 提取待办</button>
                  <button class="btn btn-ghost btn-sm btn-icon" onclick="App.toast('分享链接已复制', 'success')">${Utils.icon('share', 14)}</button>
                </div>
              </div>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  },
  init() {
    // 展开会议纪要详情
    document.querySelectorAll('.minutes-expand').forEach(btn => {
      btn.addEventListener('click', () => {
        const id = parseInt(btn.dataset.id);
        this.openDetailModal(id);
      });
    });

    // 音频时间轴点击
    document.querySelectorAll('.minutes-card .flex-1[style*="border-radius:3px"]').forEach(bar => {
      bar.addEventListener('click', (e) => {
        const rect = bar.getBoundingClientRect();
        const percent = ((e.clientX - rect.left) / rect.width * 100).toFixed(0);
        const fill = bar.firstElementChild;
        const dot = bar.lastElementChild;
        fill.style.width = percent + '%';
        dot.style.left = percent + '%';
        App.toast('已跳转到 ' + Math.floor(percent / 100 * 60) + ':' + String(Math.floor(percent / 100 * 60) % 60).padStart(2, '0'), 'info');
      });
    });
  },
  openDetailModal(id) {
    const meetings = [
      {
        title: 'Q3 产品规划评审会', date: '2026-07-04 14:00', duration: '1h 30m',
        location: '会议室 A · 线下', host: '张明',
        topics: [
          { title: 'Q3 核心方向确认', points: '讨论了微服务升级、RAG 优化、Agent 化三大方向的优先级，综合考虑业务价值和开发成本', decision: '确定优先级：RAG 优化 > 微服务升级 > Agent 化', todos: [{ task: '输出详细路线图', owner: '张明', due: '07-10' }] },
          { title: '资源分配', points: '研发资源紧张，需要协调外部支持', decision: '从市场部借调 2 名前端工程师支援', todos: [{ task: '协调人员到岗', owner: '陈静', due: '07-08' }, { task: '安排工作环境', owner: '李华', due: '07-09' }] },
          { title: '交付节点', points: '确认各模块交付时间', decision: 'RAG 优化 8 月初，微服务升级 8 月底', todos: [{ task: '同步给各负责人', owner: '张明', due: '07-06' }] },
        ],
      },
      {
        title: '微服务架构技术评审', date: '2026-07-03 10:00', duration: '2h',
        location: '线上 · 飞书会议', host: '李华',
        topics: [
          { title: '服务拆分边界', points: '订单与支付服务的边界存在重叠，需要明确职责', decision: '支付状态查询归入订单服务，支付动作归支付服务', todos: [{ task: '更新服务边界文档', owner: '李华', due: '07-05' }] },
          { title: 'APISIX 网关配置', points: '讨论了路由、限流、鉴权策略', decision: '统一 JWT 鉴权，按服务维度限流', todos: [{ task: '编写网关配置模板', owner: '陈静', due: '07-07' }] },
        ],
      },
    ];
    const m = meetings[id - 1] || meetings[0];

    App.modal({
      title: m.title,
      size: 'modal-xl',
      footer: false,
      body: `
        <!-- 基本信息 -->
        <div class="card p-4 mb-4" style="background:var(--surface-muted);">
          <div class="grid grid-4 gap-3 text-sm">
            <div><div class="text-xs text-muted mb-1">时间</div><div class="font-medium">${m.date}</div></div>
            <div><div class="text-xs text-muted mb-1">时长</div><div class="font-medium">${m.duration}</div></div>
            <div><div class="text-xs text-muted mb-1">地点</div><div class="font-medium">${m.location}</div></div>
            <div><div class="text-xs text-muted mb-1">主持人</div><div class="font-medium">${m.host}</div></div>
          </div>
        </div>

        <!-- AI 摘要要点 -->
        <div class="p-4 mb-4" style="background:var(--primary-bg);border-radius:8px;border-left:3px solid var(--primary);">
          <div class="flex items-center gap-2 mb-2">${Utils.icon('sparkles', 16)} <span class="font-semibold text-primary">AI 自动摘要</span></div>
          <ul style="padding-left:20px;list-style:disc;line-height:1.8;color:var(--text-secondary);">
            <li>会议围绕 ${m.topics.length} 个核心议题展开讨论</li>
            <li>共形成 ${m.topics.reduce((s, t) => s + t.todos.length, 0)} 项待办事项</li>
            <li>关键决议：${m.topics[0].decision}</li>
            <li>建议下次跟进待办完成情况</li>
          </ul>
        </div>

        <!-- 议题列表 -->
        <h3 class="font-semibold text-md mb-3">${Utils.icon('fileText', 16)} 议题列表</h3>
        <div class="flex flex-col gap-4">
          ${m.topics.map((t, i) => `
            <div class="card p-4">
              <div class="flex items-center gap-2 mb-2">
                <span class="badge badge-primary">议题 ${i + 1}</span>
                <span class="font-semibold text-md">${t.title}</span>
              </div>
              <div class="mb-3">
                <div class="text-xs text-muted mb-1">讨论要点</div>
                <div class="text-sm text-secondary" style="line-height:1.7;">${t.points}</div>
              </div>
              <div class="mb-3">
                <div class="text-xs text-muted mb-1">决议</div>
                <div class="text-sm" style="line-height:1.7;color:var(--success);">${Utils.icon('check', 14)} ${t.decision}</div>
              </div>
              <div>
                <div class="text-xs text-muted mb-2">待办事项</div>
                ${t.todos.map(todo => `
                  <div class="flex items-center gap-3 p-2 mb-1" style="background:var(--warning-bg);border-radius:6px;">
                    <input type="checkbox" class="minutes-todo">
                    <span class="text-sm flex-1">${todo.task}</span>
                    <span class="badge badge-info">${todo.owner}</span>
                    <span class="text-xs text-muted">截止 ${todo.due}</span>
                  </div>
                `).join('')}
              </div>
            </div>
          `).join('')}
        </div>

        <div class="flex items-center justify-between mt-4">
          <span class="text-xs text-muted">${Utils.icon('mic', 12)} 音视频回放可用</span>
          <div class="flex gap-2">
            <button class="btn btn-secondary btn-sm" onclick="App.toast('已导出 PDF', 'success')">${Utils.icon('download', 14)} 导出</button>
            <button class="btn btn-primary btn-sm" onclick="App.toast('待办已同步到任务系统', 'success')">${Utils.icon('check', 14)} 同步待办</button>
          </div>
        </div>
      `,
      onMount: (overlay) => {
        overlay.querySelectorAll('.minutes-todo').forEach(cb => {
          cb.addEventListener('change', () => {
            if (cb.checked) {
              cb.nextElementSibling.style.textDecoration = 'line-through';
              cb.nextElementSibling.style.opacity = '0.6';
              App.toast('待办已完成', 'success');
            } else {
              cb.nextElementSibling.style.textDecoration = '';
              cb.nextElementSibling.style.opacity = '';
            }
          });
        });
      }
    });
  }
});

// ============================================================
// 页面5: 知识缺口面板 (路由 'manage/gaps')
// ============================================================
App.registerPage('manage/gaps', {
  title: '知识缺口分析',
  render() {
    // 缺口统计
    const priorityCount = { high: Mock.knowledgeGaps.filter(g => g.status === 'high').length, medium: Mock.knowledgeGaps.filter(g => g.status === 'medium').length, low: Mock.knowledgeGaps.filter(g => g.status === 'low').length };
    // 热力图数据 (6x4)，值越大颜色越深
    const heatmap = [
      [3, 8, 12, 5, 2, 1],
      [5, 15, 23, 18, 9, 4],
      [8, 22, 34, 28, 15, 7],
      [4, 11, 19, 12, 6, 3],
    ];
    // SVG 折线图数据点（近 30 天搜索量）
    const trendData = [120, 145, 132, 168, 195, 178, 210, 234, 220, 245, 268, 290, 275, 310, 332, 318, 345, 368, 355, 390, 412, 398, 425, 448, 432, 465, 488, 472, 510, 534];

    return `
      <div class="page-header flex items-center justify-between">
        <div>
          <h1 class="page-title">知识缺口分析</h1>
          <p class="page-subtitle">识别用户高频搜索但缺少文档覆盖的知识盲区</p>
        </div>
        <button class="btn btn-secondary btn-sm" onclick="App.toast('正在生成最新分析...', 'info')">${Utils.icon('refresh', 14)} 刷新分析</button>
      </div>

      <!-- 统计 -->
      <div class="grid grid-3 gap-4 mb-6">
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--danger-bg);color:var(--danger);">${Utils.icon('alert', 20)}</div>
            <span class="badge badge-danger">${priorityCount.high}</span>
          </div>
          <div class="stat-card-value text-danger">${priorityCount.high}</div>
          <div class="stat-card-label">高优先级缺口</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--warning-bg);color:var(--warning);">${Utils.icon('flag', 20)}</div>
            <span class="badge badge-warning">${priorityCount.medium}</span>
          </div>
          <div class="stat-card-value text-warning">${priorityCount.medium}</div>
          <div class="stat-card-label">中优先级缺口</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--surface-hover);color:var(--text-secondary);">${Utils.icon('bookmark', 20)}</div>
            <span class="badge badge-neutral">${priorityCount.low}</span>
          </div>
          <div class="stat-card-value">${priorityCount.low}</div>
          <div class="stat-card-label">低优先级缺口</div>
        </div>
      </div>

      <div class="flex gap-5">
        <!-- 左侧主区域 -->
        <div class="flex-1" style="min-width:0;">
          <!-- 热力图 -->
          <div class="card card-shadow mb-5">
            <div class="card-header">
              <span class="card-title">${Utils.icon('grid', 16)} 缺口热力图</span>
              <span class="text-xs text-muted">按主题 × 时间段统计搜索频率</span>
            </div>
            <div class="card-body">
              <div style="overflow-x:auto;">
                <div style="display:grid;grid-template-columns:80px repeat(6, 1fr);gap:4px;min-width:500px;">
                  <div></div>
                  ${['周一', '周二', '周三', '周四', '周五', '周六'].map(d => `<div class="text-xs text-muted text-center font-medium">${d}</div>`).join('')}
                  ${['上午', '下午', '晚间', '深夜'].map((row, ri) => `
                    <div class="text-xs text-muted font-medium flex items-center">${row}</div>
                    ${heatmap[ri].map(v => {
                      const intensity = Math.min(1, v / 34);
                      const bg = `rgba(255, 59, 92, ${0.15 + intensity * 0.85})`;
                      return `<div style="background:${bg};border-radius:4px;padding:12px 8px;text-align:center;font-size:11px;color:${intensity > 0.5 ? 'white' : 'var(--text-secondary)'};font-weight:600;" title="${v} 次搜索">${v}</div>`;
                    }).join('')}
                  `).join('')}
                </div>
              </div>
              <div class="flex items-center justify-end gap-2 mt-3">
                <span class="text-xs text-muted">少</span>
                <div style="width:120px;height:8px;background:linear-gradient(to right, rgba(255,59,92,0.15), rgba(255,59,92,1));border-radius:4px;"></div>
                <span class="text-xs text-muted">多</span>
              </div>
            </div>
          </div>

          <!-- 缺口卡片列表 -->
          <div class="section-header">
            <h2 class="section-title">缺口列表</h2>
            <span class="text-xs text-muted">${Mock.knowledgeGaps.length} 个待处理</span>
          </div>
          <div class="grid grid-2 gap-4">
            ${Mock.knowledgeGaps.map(g => `
              <div class="gap-card" data-id="${g.id}">
                <div class="gap-card-header">
                  <div class="flex items-center gap-2">
                    <span class="gap-card-title">${g.topic}</span>
                  </div>
                  <span class="badge badge-${g.status === 'high' ? 'danger' : g.status === 'medium' ? 'warning' : 'neutral'}">
                    ${g.status === 'high' ? '高优先级' : g.status === 'medium' ? '中优先级' : '低优先级'}
                  </span>
                </div>
                <div class="flex items-center gap-4 mb-2 text-xs text-muted">
                  <span class="flex items-center gap-1">${Utils.icon('search', 12)} ${g.searches} 次搜索</span>
                  <span class="flex items-center gap-1">${Utils.icon('trending', 12)} 持续增长</span>
                </div>
                <div class="gap-card-body">${g.desc}</div>
                <div class="p-3 mb-3" style="background:var(--success-bg);border-radius:6px;">
                  <div class="flex items-center gap-2 mb-1">
                    ${Utils.icon('sparkles', 12)}
                    <span class="text-xs font-semibold text-success">AI 建议</span>
                  </div>
                  <div class="text-xs text-secondary" style="line-height:1.6;">${g.suggestion}</div>
                </div>
                <div class="gap-card-footer">
                  <button class="btn btn-primary btn-sm gap-create" data-topic="${g.topic}">
                    ${Utils.icon('plus', 14)} 创建文档
                  </button>
                  <button class="btn btn-secondary btn-sm gap-assign" data-topic="${g.topic}">
                    ${Utils.icon('users', 14)} 指派负责人
                  </button>
                </div>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- 右侧趋势分析 -->
        <aside style="width:320px;flex-shrink:0;">
          <div class="card card-shadow" style="position:sticky;top:72px;">
            <div class="card-header">
              <span class="card-title">${Utils.icon('trending', 16)} 趋势分析</span>
              <span class="badge badge-danger">+18%</span>
            </div>
            <div class="card-body">
              <!-- SVG 折线图 -->
              <div class="chart-container mb-3">
                <svg viewBox="0 0 300 150" style="width:100%;height:150px;" id="gap-trend-chart">
                  <!-- 网格线 -->
                  <line x1="0" y1="30" x2="300" y2="30" stroke="var(--border-light)" stroke-width="1"/>
                  <line x1="0" y1="75" x2="300" y2="75" stroke="var(--border-light)" stroke-width="1"/>
                  <line x1="0" y1="120" x2="300" y2="120" stroke="var(--border-light)" stroke-width="1"/>
                  <!-- 渐变区域 -->
                  <defs>
                    <linearGradient id="trendGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stop-color="var(--primary)" stop-opacity="0.3"/>
                      <stop offset="100%" stop-color="var(--primary)" stop-opacity="0"/>
                    </linearGradient>
                  </defs>
                  ${(() => {
                    const max = Math.max(...trendData);
                    const points = trendData.map((v, i) => `${(i / (trendData.length - 1)) * 300},${150 - (v / max) * 130 - 10}`).join(' ');
                    const area = `0,140 ${points} 300,140`;
                    return `
                      <polygon points="${area}" fill="url(#trendGradient)"/>
                      <polyline points="${points}" fill="none" stroke="var(--primary)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                      <circle cx="${(trendData.length - 1) / (trendData.length - 1) * 300}" cy="${150 - (trendData[trendData.length - 1] / max) * 130 - 10}" r="4" fill="var(--primary)"/>
                    `;
                  })()}
                </svg>
              </div>
              <div class="flex items-center justify-between text-xs text-muted mb-4">
                <span>近 30 天</span>
                <span>搜索量趋势</span>
              </div>

              <div class="divider"></div>

              <!-- 关键指标 -->
              <div class="mb-3">
                <div class="flex items-center justify-between mb-2">
                  <span class="text-sm">总搜索量</span>
                  <span class="font-bold text-primary">7,842</span>
                </div>
                <div class="flex items-center justify-between mb-2">
                  <span class="text-sm">缺口覆盖率</span>
                  <span class="font-bold text-warning">42.6%</span>
                </div>
                <div class="flex items-center justify-between mb-2">
                  <span class="text-sm">已创建文档</span>
                  <span class="font-bold text-success">18</span>
                </div>
                <div class="flex items-center justify-between">
                  <span class="text-sm">待处理缺口</span>
                  <span class="font-bold text-danger">${Mock.knowledgeGaps.length}</span>
                </div>
              </div>

              <div class="divider"></div>

              <!-- 热门缺口主题 -->
              <div class="filter-label mb-2">热门缺口主题</div>
              <div class="flex flex-col gap-2">
                ${Mock.knowledgeGaps.slice(0, 4).map(g => `
                  <div class="flex items-center gap-2">
                    <span style="width:8px;height:8px;border-radius:50%;background:var(--${g.status === 'high' ? 'danger' : g.status === 'medium' ? 'warning' : 'text-disabled'});"></span>
                    <span class="text-sm flex-1 truncate">${g.topic}</span>
                    <span class="text-xs text-muted">${g.searches}</span>
                  </div>
                `).join('')}
              </div>
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init() {
    // 创建文档按钮 - 跳转到上传页面
    document.querySelectorAll('.gap-create').forEach(btn => {
      btn.addEventListener('click', () => {
        App.toast(`正在为「${btn.dataset.topic}」创建文档`, 'success');
        setTimeout(() => App.navigate('manage/upload'), 800);
      });
    });

    // 指派负责人
    document.querySelectorAll('.gap-assign').forEach(btn => {
      btn.addEventListener('click', () => {
        App.modal({
          title: '指派负责人',
          body: `
            <p class="text-sm text-muted mb-4">为缺口「${btn.dataset.topic}」指派负责人，将创建待办任务并通知对应人员。</p>
            <div class="form-group">
              <label class="form-label">选择负责人</label>
              <select class="form-select">
                ${Mock.users.filter(u => u.status === 'active').map(u => `<option>${u.name} - ${u.dept} (${u.role})</option>`).join('')}
              </select>
            </div>
            <div class="form-group">
              <label class="form-label">截止时间</label>
              <input class="form-input" type="date" value="2026-07-15">
            </div>
            <div class="form-group" style="margin-bottom:0;">
              <label class="form-label">备注</label>
              <textarea class="form-textarea" placeholder="补充说明...">请尽快补充相关文档，搜索量持续增长。</textarea>
            </div>
          `,
          confirmText: '指派',
          onConfirm: (overlay) => { overlay.remove(); App.toast('已指派负责人，任务已创建', 'success'); }
        });
      });
    });
  }
});
