/* === AI 对话相关页面 === */

// ============================================
// 页面1: AI 对话主界面 (chat)
// ============================================
App.registerPage('chat', {
  title: 'AI 对话',
  render() {
    // 固定对话和普通对话分组
    const pinnedSessions = Mock.chatSessions.filter(s => s.pinned);
    const normalSessions = Mock.chatSessions.filter(s => !s.pinned);

    // 渲染单个会话项
    const renderSession = (s) => `
      <div class="chat-session ${s.active ? 'active' : ''}" data-id="${s.id}">
        <div class="chat-session-title">${s.title}</div>
        <div class="chat-session-preview">${s.preview}</div>
        <div class="chat-session-meta">
          <span class="text-xs text-muted">${s.time}</span>
          ${s.pinned ? `<span class="badge badge-primary">置顶</span>` : ''}
        </div>
      </div>
    `;

    return `
      <div class="chat-layout">
        <!-- 左列: 对话历史列表 -->
        <aside class="chat-sidebar">
          <div style="padding:12px;border-bottom:1px solid var(--border-light);">
            <button class="btn btn-primary btn-block" id="chatNewBtn">
              ${Utils.icon('plus', 16)}
              <span>新建对话</span>
            </button>
          </div>
          <div style="padding:12px;border-bottom:1px solid var(--border-light);">
            <div class="topbar-search" style="width:100%;">
              <span class="topbar-search-icon">${Utils.icon('search', 16)}</span>
              <input type="text" id="chatSessionSearch" placeholder="搜索对话..." />
            </div>
          </div>
          <div class="chat-session-list" id="chatSessionList">
            ${pinnedSessions.length > 0 ? `
              <div class="filter-label" style="padding:4px 12px;">置顶对话</div>
              ${pinnedSessions.map(renderSession).join('')}
              <div class="filter-label" style="padding:4px 12px;margin-top:8px;">最近对话</div>
            ` : ''}
            ${normalSessions.map(renderSession).join('')}
          </div>
          <div style="padding:12px;border-top:1px solid var(--border-light);font-size:12px;color:var(--text-muted);text-align:center;">
            共 ${Mock.chatSessions.length} 条对话
          </div>
        </aside>

        <!-- 中列: 对话区域 -->
        <main class="chat-main">
          <!-- 顶部对话标题 + Agent 状态 -->
          <div style="padding:12px 24px;border-bottom:1px solid var(--border-light);background:var(--surface);display:flex;align-items:center;justify-content:space-between;">
            <div>
              <div style="font-size:16px;font-weight:600;">产品 Q3 规划讨论</div>
              <div style="font-size:12px;color:var(--text-muted);display:flex;align-items:center;gap:6px;margin-top:2px;">
                <span style="width:8px;height:8px;border-radius:50%;background:var(--success);display:inline-block;"></span>
                <span>通用问答Agent · 在线</span>
                <span class="badge badge-primary" style="margin-left:4px;">GPT-4o</span>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <button class="icon-btn" onclick="App.navigate('chat/agent')" title="切换Agent">${Utils.icon('sparkles', 18)}</button>
              <button class="icon-btn" onclick="App.toast('已收藏当前对话', 'success')" title="收藏">${Utils.icon('bookmark', 18)}</button>
              <button class="icon-btn" onclick="App.toast('已复制对话链接', 'success')" title="分享">${Utils.icon('share', 18)}</button>
              <button class="icon-btn" id="chatToggleRight" title="引用面板">${Utils.icon('layers', 18)}</button>
            </div>
          </div>

          <!-- 消息区域 -->
          <div class="chat-messages" id="chatMessages">
            <!-- 系统提示 -->
            <div style="text-align:center;margin-bottom:24px;">
              <span class="badge badge-neutral" style="padding:4px 12px;">今天 14:30</span>
            </div>

            <!-- AI 消息 1: 带引用 -->
            <div class="chat-msg ai">
              <div class="chat-msg-avatar">
                <div class="avatar avatar-md" style="background:linear-gradient(135deg,var(--primary),var(--primary-light));">AI</div>
              </div>
              <div class="chat-msg-body">
                <div class="chat-msg-name">通用问答Agent · 刚刚</div>
                <div class="chat-msg-bubble">
                  您好！关于产品 Q3 规划，我已检索到相关资料。根据《2026年Q3产品路线图》<span class="chat-msg-citation" data-source="1">[1]</span> 和《微服务架构设计规范 v2.0》<span class="chat-msg-citation" data-source="2">[2]</span>，Q3 季度建议聚焦以下三个方向：
                  <br><br>
                  1. <strong>核心功能迭代</strong>：完成知识图谱可视化、多模态文档解析、Agentic RAG 工作流编排等核心能力；
                  <br>
                  2. <strong>技术债清理</strong>：重构检索引擎，将平均响应时间从 1.2s 降至 800ms 以内；
                  <br>
                  3. <strong>架构升级</strong>：引入 MCP 工具协议，支持外部系统调用<span class="chat-msg-citation" data-source="3">[3]</span>。
                  <br><br>
                  您希望我详细展开哪个方向？
                </div>
                <div class="chat-msg-sources">
                  <div class="chat-msg-source" data-source="1">
                    <span>📄</span>
                    <div>
                      <div style="font-weight:500;">2026年Q3产品路线图</div>
                      <div style="color:var(--text-muted);font-size:11px;">产品研发知识库 · 234 次查看</div>
                    </div>
                  </div>
                  <div class="chat-msg-source" data-source="2">
                    <span>📄</span>
                    <div>
                      <div style="font-weight:500;">微服务架构设计规范 v2.0</div>
                      <div style="color:var(--text-muted);font-size:11px;">产品研发知识库 · 456 次查看</div>
                    </div>
                  </div>
                  <div class="chat-msg-source" data-source="3">
                    <span>📄</span>
                    <div>
                      <div style="font-weight:500;">企业知识库RAG系统技术方案</div>
                      <div style="color:var(--text-muted);font-size:11px;">产品研发知识库 · 389 次查看</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- 用户消息 -->
            <div class="chat-msg user">
              <div class="chat-msg-avatar">
                <div class="avatar avatar-md" style="background:${Utils.avatarColor(Mock.currentUser.name)};">${Mock.currentUser.avatar}</div>
              </div>
              <div class="chat-msg-body">
                <div class="chat-msg-name">我 · 14:31</div>
                <div class="chat-msg-bubble">
                  详细讲一下 Agentic RAG 工作流编排这块，特别是和 MCP 的结合方式。
                </div>
              </div>
            </div>

            <!-- AI 消息 2: 带打字动画 -->
            <div class="chat-msg ai" id="typingMsg" style="display:none;">
              <div class="chat-msg-avatar">
                <div class="avatar avatar-md" style="background:linear-gradient(135deg,var(--primary),var(--primary-light));">AI</div>
              </div>
              <div class="chat-msg-body">
                <div class="chat-msg-name">通用问答Agent · 正在输入...</div>
                <div class="chat-msg-bubble">
                  <div class="typing-indicator"><span></span><span></span><span></span></div>
                </div>
              </div>
            </div>

            <!-- AI 消息 3: 详细回复 -->
            <div class="chat-msg ai">
              <div class="chat-msg-avatar">
                <div class="avatar avatar-md" style="background:linear-gradient(135deg,var(--primary),var(--primary-light));">AI</div>
              </div>
              <div class="chat-msg-body">
                <div class="chat-msg-name">通用问答Agent · 14:32</div>
                <div class="chat-msg-bubble">
                  Agentic RAG 工作流编排的核心是<strong>将检索、推理、工具调用组合成可编排的有向无环图（DAG）</strong>。结合 MCP 协议，整体架构如下：
                  <br><br>
                  <strong>1. 规划阶段</strong>：LLM 根据用户问题拆解子任务，生成执行计划；
                  <br>
                  <strong>2. 检索阶段</strong>：对每个子任务并行检索知识库，支持向量检索 + 关键词检索 + 图谱检索；
                  <br>
                  <strong>3. 工具调用</strong>：通过 MCP 协议调用外部工具（如数据库查询、API 调用、代码执行）<span class="chat-msg-citation" data-source="3">[3]</span>；
                  <br>
                  <strong>4. 反思与重试</strong>：若结果置信度低，自动触发补充检索或工具重试；
                  <br>
                  <strong>5. 答案生成</strong>：综合所有证据生成最终回答，并标注引用来源。
                  <br><br>
                  推荐技术栈：LangGraph（编排）+ LlamaIndex（检索）+ MCP SDK（工具协议）。
                </div>
                <div class="flex items-center gap-2 mt-2">
                  <button class="btn btn-ghost btn-sm" onclick="App.toast('已复制回答', 'success')">${Utils.icon('copy', 14)} 复制</button>
                  <button class="btn btn-ghost btn-sm" onclick="App.toast('已收藏到我的收藏', 'success')">${Utils.icon('bookmark', 14)} 收藏</button>
                  <button class="btn btn-ghost btn-sm" onclick="App.toast('反馈已提交，感谢！', 'success')">${Utils.icon('star', 14)} 点赞</button>
                </div>
              </div>
            </div>
          </div>

          <!-- 底部输入栏 -->
          <div class="chat-input-bar">
            <div class="chat-input-wrap">
              <div class="chat-input-tools">
                <div class="chat-input-tool" title="附件" onclick="App.toast('附件功能（原型演示）', 'info')">${Utils.icon('paperclip', 18)}</div>
                <div class="chat-input-tool" title="图片" onclick="App.toast('图片功能（原型演示）', 'info')">${Utils.icon('image', 18)}</div>
                <div class="chat-input-tool" title="语音" onclick="App.toast('语音功能（原型演示）', 'info')">${Utils.icon('mic', 18)}</div>
                <div class="chat-input-tool" title="知识库" onclick="App.toast('@知识库：将限定检索范围', 'info')">${Utils.icon('at', 18)}</div>
              </div>
              <textarea class="chat-input" id="chatInput" placeholder="输入您的问题，或按 / 唤起 Agent..." rows="1"></textarea>
              <button class="chat-send-btn" id="chatSendBtn" title="发送">${Utils.icon('send', 16)}</button>
            </div>
            <div style="margin-top:8px;font-size:12px;color:var(--text-muted);text-align:center;">
              按 Enter 发送 · Shift+Enter 换行 · 输入 / 唤起 Agent · 当前使用 GPT-4o
            </div>
          </div>
        </main>

        <!-- 右列: 引用面板 -->
        <aside class="chat-right" id="chatRight">
          <!-- 引用来源 -->
          <div style="padding:16px;border-bottom:1px solid var(--border-light);">
            <div class="section-header" style="margin-bottom:12px;">
              <div class="section-title" style="font-size:14px;">引用来源</div>
              <span class="badge badge-primary">3 条</span>
            </div>
            <div class="flex flex-col gap-2">
              <div class="chat-msg-source" data-source="1" style="width:100%;">
                <div class="doc-item-icon" style="background:var(--primary-bg);width:32px;height:32px;font-size:16px;">📄</div>
                <div style="flex:1;min-width:0;">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">2026年Q3产品路线图</div>
                  <div style="color:var(--text-muted);font-size:11px;margin-top:2px;">产品研发知识库</div>
                  <div style="margin-top:4px;">
                    <span style="font-size:11px;color:var(--success);">●</span>
                    <span style="font-size:11px;color:var(--text-muted);margin-left:4px;">相似度 94.2%</span>
                  </div>
                </div>
              </div>
              <div class="chat-msg-source" data-source="2" style="width:100%;">
                <div class="doc-item-icon" style="background:var(--info-bg);width:32px;height:32px;font-size:16px;">📄</div>
                <div style="flex:1;min-width:0;">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">微服务架构设计规范 v2.0</div>
                  <div style="color:var(--text-muted);font-size:11px;margin-top:2px;">产品研发知识库</div>
                  <div style="margin-top:4px;">
                    <span style="font-size:11px;color:var(--success);">●</span>
                    <span style="font-size:11px;color:var(--text-muted);margin-left:4px;">相似度 89.6%</span>
                  </div>
                </div>
              </div>
              <div class="chat-msg-source" data-source="3" style="width:100%;">
                <div class="doc-item-icon" style="background:var(--warning-bg);width:32px;height:32px;font-size:16px;">📄</div>
                <div style="flex:1;min-width:0;">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">企业知识库RAG系统技术方案</div>
                  <div style="color:var(--text-muted);font-size:11px;margin-top:2px;">产品研发知识库</div>
                  <div style="margin-top:4px;">
                    <span style="font-size:11px;color:var(--warning);">●</span>
                    <span style="font-size:11px;color:var(--text-muted);margin-left:4px;">相似度 85.3%</span>
                  </div>
                </div>
              </div>
              <div class="chat-msg-source" data-source="4" style="width:100%;">
                <div class="doc-item-icon" style="background:var(--success-bg);width:32px;height:32px;font-size:16px;">📋</div>
                <div style="flex:1;min-width:0;">
                  <div style="font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">API接口设计规范 v3.0</div>
                  <div style="color:var(--text-muted);font-size:11px;margin-top:2px;">产品研发知识库</div>
                  <div style="margin-top:4px;">
                    <span style="font-size:11px;color:var(--text-muted);">●</span>
                    <span style="font-size:11px;color:var(--text-muted);margin-left:4px;">相似度 72.1%</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- 相关实体 -->
          <div style="padding:16px;border-bottom:1px solid var(--border-light);">
            <div class="section-header" style="margin-bottom:12px;">
              <div class="section-title" style="font-size:14px;">相关实体</div>
              <span class="badge badge-info">4</span>
            </div>
            <div class="flex flex-wrap gap-2">
              <span class="tag">Agentic RAG</span>
              <span class="tag">MCP 协议</span>
              <span class="tag">LangGraph</span>
              <span class="tag">知识图谱</span>
              <span class="tag">向量检索</span>
              <span class="tag">工具调用</span>
            </div>
          </div>

          <!-- Agent 状态 -->
          <div style="padding:16px;">
            <div class="section-header" style="margin-bottom:12px;">
              <div class="section-title" style="font-size:14px;">Agent 状态</div>
              <span class="badge badge-success">运行中</span>
            </div>
            <div class="audit-step">
              <div class="audit-step-icon done">${Utils.icon('check', 14)}</div>
              <div class="audit-step-content">
                <div class="audit-step-title">检索知识库</div>
                <div class="audit-step-desc">检索到 4 篇相关文档 · 耗时 320ms</div>
              </div>
            </div>
            <div class="audit-step">
              <div class="audit-step-icon done">${Utils.icon('check', 14)}</div>
              <div class="audit-step-content">
                <div class="audit-step-title">分析与推理</div>
                <div class="audit-step-desc">调用 LLM 进行多步推理 · 耗时 1.2s</div>
              </div>
            </div>
            <div class="audit-step">
              <div class="audit-step-icon current">${Utils.icon('zap', 14)}</div>
              <div class="audit-step-content">
                <div class="audit-step-title">生成回答</div>
                <div class="audit-step-desc">正在组织答案并标注引用...</div>
              </div>
            </div>
            <div class="audit-step">
              <div class="audit-step-icon pending">4</div>
              <div class="audit-step-content">
                <div class="audit-step-title">质量校验</div>
                <div class="audit-step-desc">等待中</div>
              </div>
            </div>
            <div style="margin-top:12px;padding:12px;background:var(--surface-muted);border-radius:8px;font-size:12px;color:var(--text-muted);">
              <div class="flex items-center justify-between mb-1">
                <span>Token 使用</span>
                <span style="color:var(--text);font-weight:500;">1,284 / 8,192</span>
              </div>
              <div class="progress-bar"><div class="progress-bar-fill primary" style="width:15.7%;"></div></div>
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init() {
    const self = this;

    // 发送消息函数
    function sendMessage() {
      const input = document.getElementById('chatInput');
      const text = input.value.trim();
      if (!text) return;

      const messages = document.getElementById('chatMessages');

      // 添加用户消息
      const userMsg = document.createElement('div');
      userMsg.className = 'chat-msg user';
      userMsg.innerHTML = `
        <div class="chat-msg-avatar">
          <div class="avatar avatar-md" style="background:${Utils.avatarColor(Mock.currentUser.name)};">${Mock.currentUser.avatar}</div>
        </div>
        <div class="chat-msg-body">
          <div class="chat-msg-name">我 · 刚刚</div>
          <div class="chat-msg-bubble">${Utils.escape(text)}</div>
        </div>
      `;
      messages.appendChild(userMsg);
      input.value = '';
      input.style.height = 'auto';
      messages.scrollTop = messages.scrollHeight;

      // 显示打字动画
      const typingMsg = document.getElementById('typingMsg');
      if (typingMsg) {
        typingMsg.style.display = 'flex';
        messages.scrollTop = messages.scrollHeight;
      }

      // 1.5 秒后添加 AI 回复
      setTimeout(() => {
        if (typingMsg) typingMsg.style.display = 'none';
        const aiMsg = document.createElement('div');
        aiMsg.className = 'chat-msg ai';
        const replies = [
          '好的，已为您检索到相关信息。根据企业知识库中的资料，针对您的问题回答如下：这是一个典型的企业知识管理场景，建议从内容治理、检索增强、用户激励三个维度系统性推进。',
          '根据您的问题，我查询了相关文档。答案是：建议采用「检索-推理-生成」三段式架构，结合知识图谱增强检索准确率，并通过 MCP 协议接入企业内部工具系统。',
          '已为您分析完成。从现有资料看，关键在于平衡检索召回率与精度，推荐使用混合检索策略（向量 + 关键词 + 图谱），并将置信度阈值设为 0.75。',
        ];
        const reply = replies[Math.floor(Math.random() * replies.length)];
        aiMsg.innerHTML = `
          <div class="chat-msg-avatar">
            <div class="avatar avatar-md" style="background:linear-gradient(135deg,var(--primary),var(--primary-light));">AI</div>
          </div>
          <div class="chat-msg-body">
            <div class="chat-msg-name">通用问答Agent · 刚刚</div>
            <div class="chat-msg-bubble">${reply}<br><br>已参考 <span class="chat-msg-citation" data-source="1">[1]</span><span class="chat-msg-citation" data-source="2">[2]</span> 等文档。</div>
            <div class="flex items-center gap-2 mt-2">
              <button class="btn btn-ghost btn-sm" onclick="App.toast('已复制回答', 'success')">${Utils.icon('copy', 14)} 复制</button>
              <button class="btn btn-ghost btn-sm" onclick="App.toast('已收藏', 'success')">${Utils.icon('bookmark', 14)} 收藏</button>
            </div>
          </div>
        `;
        messages.appendChild(aiMsg);
        messages.scrollTop = messages.scrollHeight;
      }, 1500);
    }

    // 发送按钮
    const sendBtn = document.getElementById('chatSendBtn');
    if (sendBtn) sendBtn.addEventListener('click', sendMessage);

    // 输入框：Enter 发送，Shift+Enter 换行，自动增高
    const chatInput = document.getElementById('chatInput');
    if (chatInput) {
      chatInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          sendMessage();
        }
      });
      chatInput.addEventListener('input', () => {
        chatInput.style.height = 'auto';
        chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
      });
    }

    // 新建对话
    const newBtn = document.getElementById('chatNewBtn');
    if (newBtn) newBtn.addEventListener('click', () => {
      App.toast('已创建新对话', 'success');
    });

    // 会话项切换 active
    const sessions = document.querySelectorAll('.chat-session');
    sessions.forEach(s => {
      s.addEventListener('click', () => {
        sessions.forEach(x => x.classList.remove('active'));
        s.classList.add('active');
        App.toast('已切换到对话：' + s.querySelector('.chat-session-title').textContent, 'info');
      });
    });

    // 搜索过滤
    const searchInput = document.getElementById('chatSessionSearch');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        const kw = e.target.value.toLowerCase();
        sessions.forEach(s => {
          const title = s.querySelector('.chat-session-title').textContent.toLowerCase();
          const preview = s.querySelector('.chat-session-preview').textContent.toLowerCase();
          s.style.display = (title.includes(kw) || preview.includes(kw)) ? '' : 'none';
        });
      });
    }

    // 引用来源点击 → toast
    document.querySelectorAll('.chat-msg-source, .chat-msg-citation').forEach(el => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const src = el.dataset.source;
        const sources = {
          '1': '《2026年Q3产品路线图》· 产品研发知识库',
          '2': '《微服务架构设计规范 v2.0》· 产品研发知识库',
          '3': '《企业知识库RAG系统技术方案》· 产品研发知识库',
          '4': '《API接口设计规范 v3.0》· 产品研发知识库',
        };
        App.toast('查看引用：' + (sources[src] || '引用来源'), 'info');
      });
    });

    // 相关实体点击
    document.querySelectorAll('.chat-right .tag').forEach(t => {
      t.addEventListener('click', () => {
        App.toast('搜索实体：' + t.textContent, 'info');
      });
    });

    // 折叠右列面板
    const toggleBtn = document.getElementById('chatToggleRight');
    if (toggleBtn) {
      toggleBtn.addEventListener('click', () => {
        const right = document.getElementById('chatRight');
        if (right) {
          const hidden = right.style.display === 'none';
          right.style.display = hidden ? '' : 'none';
          App.toast(hidden ? '已展开引用面板' : '已折叠引用面板', 'info');
        }
      });
    }
  }
});

// ============================================
// 页面2: 对话历史 (chat/history)
// ============================================
App.registerPage('chat/history', {
  title: '对话历史',
  render() {
    // 扩展会话数据为更丰富的卡片
    const enriched = Mock.chatSessions.map((s, i) => ({
      ...s,
      msgCount: 8 + i * 3 + (s.pinned ? 5 : 0),
      agent: ['通用问答Agent', '报销助手Agent', 'IT运维Agent', '文档审核Agent', '新人导师Agent'][i % 5],
      agentIcon: ['💬', '💰', '⚙️', '✅', '🎓'][i % 5],
      duration: Math.floor(Math.random() * 30 + 5) + ' 分钟',
      category: ['产品规划', '架构咨询', '流程查询', '技术方案', '入职指引', '竞品分析', '规范讨论', '运维方案'][i % 8],
    }));

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <h1 class="page-title">对话历史</h1>
            <p class="page-subtitle">查看所有 AI 对话记录，支持搜索与时间筛选</p>
          </div>
          <div class="flex items-center gap-3">
            <button class="btn btn-secondary" onclick="App.toast('正在导出对话记录...', 'info')">${Utils.icon('download', 16)} 导出</button>
            <button class="btn btn-primary" onclick="App.navigate('chat')">${Utils.icon('plus', 16)} 新建对话</button>
          </div>
        </div>
      </div>

      <!-- 搜索 + 时间筛选 -->
      <div class="card mb-5">
        <div class="card-body" style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
          <div class="topbar-search" style="flex:1;min-width:240px;">
            <span class="topbar-search-icon">${Utils.icon('search', 16)}</span>
            <input type="text" id="historySearch" placeholder="搜索对话标题或内容..." />
          </div>
          <div class="tabs-pill" id="historyFilter">
            <div class="tab-pill active" data-filter="all">全部</div>
            <div class="tab-pill" data-filter="today">今天</div>
            <div class="tab-pill" data-filter="yesterday">昨天</div>
            <div class="tab-pill" data-filter="week">本周</div>
          </div>
          <div class="flex items-center gap-2">
            <button class="btn btn-ghost btn-sm" onclick="App.toast('已按 Agent 筛选', 'info')">${Utils.icon('filter', 14)} Agent</button>
            <button class="btn btn-ghost btn-sm" onclick="App.toast('排序方式：最新优先', 'info')">${Utils.icon('clock', 14)} 最新</button>
          </div>
        </div>
      </div>

      <!-- 统计概览 -->
      <div class="grid grid-4 gap-4 mb-6">
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--primary-bg);color:var(--primary);">${Utils.icon('chat', 20)}</div>
            <span class="badge badge-primary">${Mock.chatSessions.length} 条</span>
          </div>
          <div class="stat-card-value">${Mock.chatSessions.length}</div>
          <div class="stat-card-label">对话总数</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--success-bg);color:var(--success);">${Utils.icon('message', 20)}</div>
            <span class="stat-card-trend up">↑ 12.5%</span>
          </div>
          <div class="stat-card-value">128</div>
          <div class="stat-card-label">今日消息数</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--warning-bg);color:var(--warning);">${Utils.icon('clock', 20)}</div>
            <span class="stat-card-trend up">↑ 5.2%</span>
          </div>
          <div class="stat-card-value">3.2h</div>
          <div class="stat-card-label">累计对话时长</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--info-bg);color:var(--info);">${Utils.icon('sparkles', 20)}</div>
            <span class="badge badge-info">5 个</span>
          </div>
          <div class="stat-card-value">5</div>
          <div class="stat-card-label">使用过的 Agent</div>
        </div>
      </div>

      <!-- 对话卡片网格 -->
      <div class="section-header">
        <div class="section-title">对话列表</div>
        <span class="text-sm text-muted">显示 ${enriched.length} 条对话</span>
      </div>
      <div class="grid grid-auto gap-4" id="historyGrid">
        ${enriched.map(s => `
          <div class="card history-card" data-title="${s.title}" data-time="${s.time}" style="cursor:pointer;transition:all 0.15s ease;">
            <div class="card-body">
              <div class="flex items-center justify-between mb-3">
                <div class="flex items-center gap-2">
                  <div style="width:32px;height:32px;border-radius:8px;background:var(--primary-bg);display:flex;align-items:center;justify-content:center;font-size:18px;">${s.agentIcon}</div>
                  <div>
                    <div style="font-weight:600;font-size:14px;">${s.title}</div>
                    <div style="font-size:11px;color:var(--text-muted);">${s.agent}</div>
                  </div>
                </div>
                ${s.pinned ? `<span class="badge badge-primary">置顶</span>` : ''}
              </div>
              <div style="font-size:13px;color:var(--text-secondary);line-height:1.6;margin-bottom:12px;min-height:40px;">
                ${Utils.truncate(s.preview, 60)}
              </div>
              <div class="flex items-center gap-2 mb-3 flex-wrap">
                <span class="tag">${s.category}</span>
                <span class="badge badge-neutral">${Utils.icon('message', 12)} ${s.msgCount} 条</span>
                <span class="badge badge-neutral">${Utils.icon('clock', 12)} ${s.duration}</span>
              </div>
              <div class="flex items-center justify-between" style="padding-top:12px;border-top:1px solid var(--border-light);">
                <span class="text-xs text-muted">${Utils.icon('clock', 12)} ${s.time}</span>
                <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();App.navigate('chat');">${Utils.icon('chat', 14)} 继续</button>
              </div>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  },
  init() {
    // 卡片点击跳转
    document.querySelectorAll('.history-card').forEach(card => {
      card.addEventListener('click', () => {
        App.navigate('chat');
      });
      card.addEventListener('mouseenter', () => {
        card.style.borderColor = 'var(--primary)';
        card.style.boxShadow = 'var(--shadow-md)';
        card.style.transform = 'translateY(-2px)';
      });
      card.addEventListener('mouseleave', () => {
        card.style.borderColor = '';
        card.style.boxShadow = '';
        card.style.transform = '';
      });
    });

    // 时间筛选
    const filters = document.querySelectorAll('#historyFilter .tab-pill');
    filters.forEach(f => {
      f.addEventListener('click', () => {
        filters.forEach(x => x.classList.remove('active'));
        f.classList.add('active');
        const filter = f.dataset.filter;
        const labels = { all: '全部', today: '今天', yesterday: '昨天', week: '本周' };
        App.toast('已筛选：' + labels[filter], 'info');
      });
    });

    // 搜索过滤
    const search = document.getElementById('historySearch');
    if (search) {
      search.addEventListener('input', (e) => {
        const kw = e.target.value.toLowerCase();
        document.querySelectorAll('.history-card').forEach(card => {
          const title = (card.dataset.title || '').toLowerCase();
          card.style.display = title.includes(kw) ? '' : 'none';
        });
      });
    }
  }
});

// ============================================
// 页面3: Agent 详情 (chat/agent)
// ============================================
App.registerPage('chat/agent', {
  title: 'Agent 列表',
  render() {
    // 类型映射
    const typeMap = {
      qa: { label: '问答型', color: 'primary' },
      workflow: { label: '工作流型', color: 'info' },
      action: { label: '执行型', color: 'warning' },
    };

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <h1 class="page-title">Agent 列表</h1>
            <p class="page-subtitle">管理企业 AI Agent，配置工具与运行参数</p>
          </div>
          <div class="flex items-center gap-3">
            <button class="btn btn-secondary" onclick="App.toast('Agent 市场敬请期待', 'info')">${Utils.icon('grid', 16)} Agent 市场</button>
            <button class="btn btn-primary" onclick="App.toast('请填写 Agent 配置', 'info')">${Utils.icon('plus', 16)} 创建 Agent</button>
          </div>
        </div>
      </div>

      <!-- 统计 -->
      <div class="grid grid-4 gap-4 mb-6">
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--primary-bg);color:var(--primary);">${Utils.icon('sparkles', 20)}</div>
            <span class="badge badge-success">运行中</span>
          </div>
          <div class="stat-card-value">${Mock.agents.length}</div>
          <div class="stat-card-label">Agent 总数</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--success-bg);color:var(--success);">${Utils.icon('check', 20)}</div>
            <span class="stat-card-trend up">↑ 8.3%</span>
          </div>
          <div class="stat-card-value">93.5%</div>
          <div class="stat-card-label">平均成功率</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--warning-bg);color:var(--warning);">${Utils.icon('zap', 20)}</div>
            <span class="stat-card-trend up">↑ 15.2%</span>
          </div>
          <div class="stat-card-value">6,463</div>
          <div class="stat-card-label">累计调用次数</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--info-bg);color:var(--info);">${Utils.icon('clock', 20)}</div>
            <span class="badge badge-info">1.2s</span>
          </div>
          <div class="stat-card-value">1.2s</div>
          <div class="stat-card-label">平均响应时间</div>
        </div>
      </div>

      <!-- 类型筛选 -->
      <div class="section-header">
        <div class="section-title">所有 Agent</div>
        <div class="tabs-pill" id="agentFilter">
          <div class="tab-pill active" data-type="all">全部</div>
          <div class="tab-pill" data-type="qa">问答型</div>
          <div class="tab-pill" data-type="workflow">工作流型</div>
          <div class="tab-pill" data-type="action">执行型</div>
        </div>
      </div>

      <!-- Agent 网格 -->
      <div class="grid grid-auto gap-4" id="agentGrid">
        ${Mock.agents.map(a => {
          const typeInfo = typeMap[a.type] || typeMap.qa;
          const statusColor = a.status === 'active' ? 'var(--success)' : 'var(--text-muted)';
          return `
            <div class="card agent-card" data-id="${a.id}" data-type="${a.type}" style="cursor:pointer;transition:all 0.15s ease;">
              <div class="card-body">
                <div class="flex items-start justify-between mb-3">
                  <div style="width:48px;height:48px;border-radius:12px;background:var(--primary-bg);display:flex;align-items:center;justify-content:center;font-size:28px;">${a.icon}</div>
                  <label class="switch" onclick="event.stopPropagation();">
                    <input type="checkbox" ${a.status === 'active' ? 'checked' : ''} data-agent-id="${a.id}">
                    <span class="switch-slider"></span>
                  </label>
                </div>
                <div style="font-size:16px;font-weight:600;margin-bottom:4px;">${a.name}</div>
                <div style="font-size:13px;color:var(--text-secondary);line-height:1.6;margin-bottom:12px;min-height:42px;">${a.desc}</div>
                <div class="flex items-center gap-2 mb-3 flex-wrap">
                  <span class="badge badge-${typeInfo.color}">${typeInfo.label}</span>
                  ${a.tools.slice(0, 3).map(t => `<span class="tag">${t}</span>`).join('')}
                  ${a.tools.length > 3 ? `<span class="badge badge-neutral">+${a.tools.length - 3}</span>` : ''}
                </div>
                <div style="margin-bottom:8px;">
                  <div class="flex items-center justify-between mb-1">
                    <span class="text-xs text-muted">成功率</span>
                    <span class="text-xs font-medium" style="color:${a.successRate >= 95 ? 'var(--success)' : a.successRate >= 90 ? 'var(--warning)' : 'var(--danger)'};">${a.successRate}%</span>
                  </div>
                  <div class="progress-bar"><div class="progress-bar-fill ${a.successRate >= 95 ? 'success' : a.successRate >= 90 ? 'warning' : 'danger'}" style="width:${a.successRate}%;"></div></div>
                </div>
                <div class="flex items-center justify-between" style="padding-top:12px;border-top:1px solid var(--border-light);">
                  <span class="text-xs text-muted">${Utils.icon('zap', 12)} ${a.calls.toLocaleString()} 次调用</span>
                  <span class="text-xs" style="color:${statusColor};">● ${a.status === 'active' ? '运行中' : '已停用'}</span>
                </div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;
  },
  init() {
    // 卡片点击 → 打开模态框
    document.querySelectorAll('.agent-card').forEach(card => {
      card.addEventListener('click', () => {
        const id = parseInt(card.dataset.id);
        const agent = Mock.agents.find(a => a.id === id);
        if (agent) this.openAgentModal(agent);
      });
      card.addEventListener('mouseenter', () => {
        card.style.borderColor = 'var(--primary)';
        card.style.boxShadow = 'var(--shadow-md)';
        card.style.transform = 'translateY(-2px)';
      });
      card.addEventListener('mouseleave', () => {
        card.style.borderColor = '';
        card.style.boxShadow = '';
        card.style.transform = '';
      });
    });

    // 开关切换
    document.querySelectorAll('.agent-card .switch input').forEach(sw => {
      sw.addEventListener('change', (e) => {
        e.stopPropagation();
        const id = sw.dataset.agentId;
        const agent = Mock.agents.find(a => a.id == id);
        App.toast(`Agent「${agent.name}」已${sw.checked ? '启用' : '停用'}`, sw.checked ? 'success' : 'warning');
      });
    });

    // 类型筛选
    const filters = document.querySelectorAll('#agentFilter .tab-pill');
    filters.forEach(f => {
      f.addEventListener('click', () => {
        filters.forEach(x => x.classList.remove('active'));
        f.classList.add('active');
        const type = f.dataset.type;
        document.querySelectorAll('.agent-card').forEach(card => {
          card.style.display = (type === 'all' || card.dataset.type === type) ? '' : 'none';
        });
      });
    });
  },
  // 打开 Agent 详情模态框
  openAgentModal(agent) {
    const typeMap = { qa: '问答型', workflow: '工作流型', action: '执行型' };
    App.modal({
      title: agent.icon + ' ' + agent.name,
      size: 'modal-lg',
      body: `
        <div class="flex items-center gap-3 mb-5">
          <div style="width:56px;height:56px;border-radius:14px;background:var(--primary-bg);display:flex;align-items:center;justify-content:center;font-size:32px;">${agent.icon}</div>
          <div style="flex:1;">
            <div style="font-size:18px;font-weight:600;">${agent.name}</div>
            <div style="font-size:13px;color:var(--text-muted);margin-top:4px;">${agent.desc}</div>
            <div class="flex items-center gap-2 mt-2">
              <span class="badge badge-primary">${typeMap[agent.type]}</span>
              <span class="badge badge-${agent.status === 'active' ? 'success' : 'neutral'}">${agent.status === 'active' ? '运行中' : '已停用'}</span>
              <span class="badge badge-info">${agent.tools.length} 个工具</span>
            </div>
          </div>
        </div>

        <!-- Tabs -->
        <div class="tabs mb-4" id="agentModalTabs">
          <div class="tab active" data-tab="config">配置参数</div>
          <div class="tab" data-tab="logs">工具调用日志</div>
          <div class="tab" data-tab="samples">最近对话样例</div>
        </div>

        <!-- Tab: 配置参数 -->
        <div id="tab-config" class="tab-content active">
          <div class="grid grid-2 gap-4">
            <div class="form-group">
              <label class="form-label">模型</label>
              <select class="form-select"><option>GPT-4o</option><option>Claude-3.5-Sonnet</option><option>Qwen-VL-Max</option><option>DeepSeek-V3</option></select>
            </div>
            <div class="form-group">
              <label class="form-label">温度 (Temperature)</label>
              <input class="form-input" type="number" value="0.3" step="0.1" min="0" max="2">
            </div>
            <div class="form-group">
              <label class="form-label">最大 Token</label>
              <input class="form-input" type="number" value="4096">
            </div>
            <div class="form-group">
              <label class="form-label">检索知识库</label>
              <select class="form-select"><option>全部知识库</option><option>产品研发知识库</option><option>人力资源制度库</option></select>
            </div>
            <div class="form-group">
              <label class="form-label">检索 Top-K</label>
              <input class="form-input" type="number" value="5">
            </div>
            <div class="form-group">
              <label class="form-label">相似度阈值</label>
              <input class="form-input" type="number" value="0.75" step="0.05">
            </div>
          </div>
          <div class="form-group mt-4">
            <label class="form-label">系统提示词 (System Prompt)</label>
            <textarea class="form-textarea" rows="4">你是企业知识库的${agent.name}，负责${agent.desc}。请基于知识库内容准确回答用户问题，并在引用处标注来源。如果知识库中没有相关信息，请如实告知。</textarea>
          </div>
          <div class="form-group">
            <label class="form-label">绑定工具</label>
            <div class="flex flex-wrap gap-2">
              ${agent.tools.map(t => `<span class="tag">${t}</span>`).join('')}
              <button class="btn btn-ghost btn-sm" onclick="App.toast('添加工具（原型演示）', 'info')">${Utils.icon('plus', 14)} 添加</button>
            </div>
          </div>
        </div>

        <!-- Tab: 工具调用日志 -->
        <div id="tab-logs" class="tab-content" style="display:none;">
          <div class="table-wrap">
            <table class="data-table">
              <thead><tr><th>时间</th><th>工具</th><th>输入</th><th>状态</th><th>耗时</th></tr></thead>
              <tbody>
                ${[
                  { time: '14:32:15', tool: '知识检索', input: 'Agentic RAG 工作流', status: 'success', dur: '320ms' },
                  { time: '14:32:16', tool: '文档总结', input: '2026年Q3产品路线图', status: 'success', dur: '180ms' },
                  { time: '14:31:08', tool: '知识检索', input: '报销流程材料', status: 'success', dur: '290ms' },
                  { time: '14:30:55', tool: '表单填写', input: '差旅报销单', status: 'failed', dur: '1.2s' },
                  { time: '14:25:30', tool: '知识检索', input: '新员工入职指引', status: 'success', dur: '350ms' },
                ].map(l => `
                  <tr>
                    <td><span class="text-xs text-muted">${l.time}</span></td>
                    <td><span class="tag">${l.tool}</span></td>
                    <td><span class="text-xs">${Utils.truncate(l.input, 20)}</span></td>
                    <td><span class="badge badge-${l.status === 'success' ? 'success' : 'danger'}">${l.status === 'success' ? '成功' : '失败'}</span></td>
                    <td><span class="text-xs text-muted">${l.dur}</span></td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>

        <!-- Tab: 最近对话样例 -->
        <div id="tab-samples" class="tab-content" style="display:none;">
          <div class="flex flex-col gap-3">
            ${[
              { q: 'Q3 产品规划的核心方向是什么？', a: '根据路线图，Q3 聚焦核心功能迭代、技术债清理和架构升级三个方向...', time: '14:32' },
              { q: '微服务架构如何选型？', a: '推荐基于 APISIX 网关的微服务架构，结合服务拆分原则...', time: '13:15' },
              { q: 'RAG 系统的技术方案？', a: '采用 LangGraph + LlamaIndex 的 Agentic RAG 架构...', time: '昨天' },
            ].map(s => `
              <div class="card">
                <div class="card-body">
                  <div class="flex items-center justify-between mb-2">
                    <span class="badge badge-primary">用户提问</span>
                    <span class="text-xs text-muted">${s.time}</span>
                  </div>
                  <div style="font-size:14px;margin-bottom:12px;">${s.q}</div>
                  <div class="badge badge-success mb-2">AI 回答</div>
                  <div style="font-size:13px;color:var(--text-secondary);line-height:1.6;">${s.a}</div>
                </div>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- 统计 -->
        <div class="grid grid-3 gap-4 mt-5" style="padding-top:16px;border-top:1px solid var(--border-light);">
          <div style="text-align:center;">
            <div style="font-size:20px;font-weight:700;color:var(--primary);">${agent.calls.toLocaleString()}</div>
            <div class="text-xs text-muted">累计调用</div>
          </div>
          <div style="text-align:center;">
            <div style="font-size:20px;font-weight:700;color:var(--success);">${agent.successRate}%</div>
            <div class="text-xs text-muted">成功率</div>
          </div>
          <div style="text-align:center;">
            <div style="font-size:20px;font-weight:700;color:var(--info);">1.2s</div>
            <div class="text-xs text-muted">平均响应</div>
          </div>
        </div>
      `,
      footer: true,
      confirmText: '保存配置',
      onMount: (overlay) => {
        // 模态框内 Tabs 切换
        overlay.querySelectorAll('#agentModalTabs .tab').forEach(tab => {
          tab.addEventListener('click', () => {
            overlay.querySelectorAll('#agentModalTabs .tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            overlay.querySelectorAll('.tab-content').forEach(c => { c.style.display = 'none'; c.classList.remove('active'); });
            const target = overlay.querySelector('#tab-' + tab.dataset.tab);
            target.style.display = 'block';
            target.classList.add('active');
          });
        });
      },
      onConfirm: (overlay) => {
        App.toast(`Agent「${agent.name}」配置已保存`, 'success');
        overlay.remove();
      }
    });
  }
});

