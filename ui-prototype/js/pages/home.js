/* === 工作台首页 === */

App.registerPage('home', {
  title: '工作台',
  render() {
    const user = Mock.currentUser;
    const hour = new Date().getHours();
    const greeting = hour < 12 ? '上午好' : hour < 18 ? '下午好' : '晚上好';

    return `
      <!-- 欢迎横幅 -->
      <div style="background:linear-gradient(135deg,#1A1B2E 0%,#4B3FE3 100%);border-radius:16px;padding:32px;color:white;margin-bottom:24px;position:relative;overflow:hidden;">
        <div style="position:absolute;top:-40px;right:-30px;width:300px;height:300px;border-radius:50%;background:rgba(255,255,255,0.05);"></div>
        <div style="position:absolute;bottom:-60px;right:80px;width:200px;height:200px;border-radius:50%;background:rgba(255,255,255,0.03);"></div>
        <div style="position:relative;z-index:1;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:24px;">
          <div>
            <div style="font-size:14px;opacity:0.7;margin-bottom:8px;">${greeting}，欢迎回来 👋</div>
            <h1 style="font-size:28px;font-weight:700;margin-bottom:8px;">${user.name} · ${user.dept}</h1>
            <p style="font-size:14px;opacity:0.8;max-width:480px;line-height:1.6;">
              今天有 <strong style="color:#FFD700;">2</strong> 条待审核文档，<strong style="color:#FFD700;">3</strong> 条新反馈需要处理，<strong style="color:#FFD700;">6</strong> 个知识缺口等待补充。
            </p>
          </div>
          <div style="display:flex;gap:32px;">
            <div>
              <div style="font-size:28px;font-weight:700;">${Mock.stats.todayQueries}</div>
              <div style="font-size:12px;opacity:0.6;">今日AI问答</div>
            </div>
            <div>
              <div style="font-size:28px;font-weight:700;">${Mock.stats.avgResponseTime}</div>
              <div style="font-size:12px;opacity:0.6;">平均响应</div>
            </div>
            <div>
              <div style="font-size:28px;font-weight:700;">${Mock.stats.satisfactionRate}%</div>
              <div style="font-size:12px;opacity:0.6;">满意度</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 快捷操作入口 -->
      <div class="grid grid-4 gap-4 mb-6">
        <div class="card" style="padding:20px;cursor:pointer;transition:all 0.15s ease;" onmouseover="this.style.borderColor='var(--primary)';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='var(--border-light)';this.style.transform='none'" onclick="App.navigate('chat')">
          <div style="width:44px;height:44px;background:var(--primary-bg);border-radius:10px;display:flex;align-items:center;justify-content:center;color:var(--primary);margin-bottom:12px;">${Utils.icon('sparkles',24)}</div>
          <div style="font-size:15px;font-weight:600;margin-bottom:4px;">AI 智能对话</div>
          <div style="font-size:12px;color:var(--text-muted);">向 AI 提问，获取知识库智能解答</div>
        </div>
        <div class="card" style="padding:20px;cursor:pointer;transition:all 0.15s ease;" onmouseover="this.style.borderColor='var(--primary)';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='var(--border-light)';this.style.transform='none'" onclick="App.navigate('knowledge/search')">
          <div style="width:44px;height:44px;background:var(--info-bg);border-radius:10px;display:flex;align-items:center;justify-content:center;color:var(--info);margin-bottom:12px;">${Utils.icon('search',24)}</div>
          <div style="font-size:15px;font-weight:600;margin-bottom:4px;">知识搜索</div>
          <div style="font-size:12px;color:var(--text-muted);">全文检索 ${Mock.stats.totalDocs} 篇文档</div>
        </div>
        <div class="card" style="padding:20px;cursor:pointer;transition:all 0.15s ease;" onmouseover="this.style.borderColor='var(--primary)';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='var(--border-light)';this.style.transform='none'" onclick="App.navigate('manage/upload')">
          <div style="width:44px;height:44px;background:var(--success-bg);border-radius:10px;display:flex;align-items:center;justify-content:center;color:var(--success);margin-bottom:12px;">${Utils.icon('upload',24)}</div>
          <div style="font-size:15px;font-weight:600;margin-bottom:4px;">上传文档</div>
          <div style="font-size:12px;color:var(--text-muted);">支持 PDF/Word/Markdown 等格式</div>
        </div>
        <div class="card" style="padding:20px;cursor:pointer;transition:all 0.15s ease;" onmouseover="this.style.borderColor='var(--primary)';this.style.transform='translateY(-2px)'" onmouseout="this.style.borderColor='var(--border-light)';this.style.transform='none'" onclick="App.navigate('knowledge/graph')">
          <div style="width:44px;height:44px;background:var(--warning-bg);border-radius:10px;display:flex;align-items:center;justify-content:center;color:var(--warning);margin-bottom:12px;">${Utils.icon('graph',24)}</div>
          <div style="font-size:15px;font-weight:600;margin-bottom:4px;">知识图谱</div>
          <div style="font-size:12px;color:var(--text-muted);">可视化探索知识关联关系</div>
        </div>
      </div>

      <!-- 统计概览 -->
      <div class="grid grid-4 gap-4 mb-6">
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--primary-bg);color:var(--primary);">${Utils.icon('doc',20)}</div>
            <span class="stat-card-trend up">${Utils.icon('trending',14)} 12%</span>
          </div>
          <div class="stat-card-value">${Mock.stats.totalDocs.toLocaleString()}</div>
          <div class="stat-card-label">知识文档总数</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--success-bg);color:var(--success);">${Utils.icon('folder',20)}</div>
            <span class="stat-card-trend up">${Utils.icon('trending',14)} 8%</span>
          </div>
          <div class="stat-card-value">${Mock.stats.totalKB}</div>
          <div class="stat-card-label">知识库数量</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--info-bg);color:var(--info);">${Utils.icon('users',20)}</div>
            <span class="stat-card-trend up">${Utils.icon('trending',14)} 5%</span>
          </div>
          <div class="stat-card-value">${Mock.stats.totalUsers}</div>
          <div class="stat-card-label">活跃用户</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--warning-bg);color:var(--warning);">${Utils.icon('chat',20)}</div>
            <span class="stat-card-trend up">${Utils.icon('trending',14)} 23%</span>
          </div>
          <div class="stat-card-value">${Mock.stats.totalQueries.toLocaleString()}</div>
          <div class="stat-card-label">累计AI问答</div>
        </div>
      </div>

      <!-- 双列：待办事项 + 最近活动 -->
      <div class="grid gap-6 mb-6" style="grid-template-columns:1fr 1fr;">
        <!-- 左列：待办事项 -->
        <div>
          <div class="section-header">
            <div class="section-title">待办事项</div>
            <span class="badge badge-warning">5 项待处理</span>
          </div>
          <div class="card">
            ${[
              { icon: 'check', color: 'warning', title: 'API接口设计规范 v3.0 待审核', desc: '李华 提交 · 2小时前', action: 'admin/audit', actionText: '去审核' },
              { icon: 'alert', color: 'danger', title: '知识缺口：AI Agent开发实战', desc: '234次搜索无满意结果', action: 'manage/gaps', actionText: '去补充' },
              { icon: 'message', color: 'info', title: '3条用户反馈待处理', desc: '含1条高优先级Bug', action: 'admin/feedback', actionText: '去处理' },
              { icon: 'users', color: 'primary', title: '2个成员权限申请', desc: '刘强申请编辑权限', action: 'admin/users', actionText: '去审批' },
              { icon: 'clock', color: 'neutral', title: 'Q3路线图文档需要更新', desc: '上次更新于3天前', action: 'manage/editor', actionText: '去编辑' },
            ].map((item, i) => `
              <div class="list-item" style="border-bottom:1px solid var(--border-light);padding:16px 20px;" onclick="App.navigate('${item.action}')">
                <div class="stat-card-icon" style="width:36px;height:36px;background:var(--${item.color}-bg,var(--primary-bg));color:var(--${item.color},var(--primary));flex-shrink:0;">
                  ${Utils.icon(item.icon, 18)}
                </div>
                <div style="flex:1;min-width:0;">
                  <div style="font-size:14px;font-weight:500;margin-bottom:2px;">${item.title}</div>
                  <div style="font-size:12px;color:var(--text-muted);">${item.desc}</div>
                </div>
                <button class="btn btn-ghost btn-sm">${item.actionText} ${Utils.icon('arrowRight',14)}</button>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- 右列：最近活动 -->
        <div>
          <div class="section-header">
            <div class="section-title">最近活动</div>
            <a href="#knowledge/timeline" style="font-size:13px;color:var(--primary);">查看全部</a>
          </div>
          <div class="card" style="padding:20px;">
            <div class="timeline">
              ${Mock.timelineEvents.slice(0, 5).map(e => `
                <div class="timeline-item ${e.type}">
                  <div class="timeline-date">${e.date}</div>
                  <div class="timeline-title">${e.title}</div>
                  <div class="timeline-content">${e.content}</div>
                </div>
              `).join('')}
            </div>
          </div>
        </div>
      </div>

      <!-- AI推荐 + 最近文档 -->
      <div class="grid gap-6" style="grid-template-columns:1fr 1fr;">
        <!-- 左列：AI推荐阅读 -->
        <div>
          <div class="section-header">
            <div class="section-title flex items-center gap-2">${Utils.icon('sparkles',18)} <span>AI 推荐阅读</span></div>
            <button class="btn btn-ghost btn-sm" onclick="App.toast('已为你刷新推荐','info')">${Utils.icon('refresh',14)} 换一批</button>
          </div>
          <div class="flex flex-col gap-3">
            ${Mock.documents.slice(0, 3).map((doc, i) => `
              <div class="doc-item" onclick="App.navigate('knowledge/doc/${doc.id}')">
                <div class="doc-item-icon" style="background:var(--primary-bg);color:var(--primary);">${Utils.fileIcon(doc.type)}</div>
                <div style="flex:1;min-width:0;">
                  <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                    <span style="font-size:14px;font-weight:600;">${doc.title}</span>
                    ${i === 0 ? '<span class="badge badge-primary">推荐</span>' : ''}
                  </div>
                  <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px;">${Utils.truncate(doc.summary, 60)}</div>
                  <div style="display:flex;align-items:center;gap:12px;font-size:11px;color:var(--text-muted);">
                    <span>${doc.author}</span>
                    <span>·</span>
                    <span>${doc.updatedAt}</span>
                    <span>·</span>
                    <span>${doc.views} 次阅读</span>
                  </div>
                </div>
              </div>
            `).join('')}
          </div>
        </div>

        <!-- 右列：我的知识库 -->
        <div>
          <div class="section-header">
            <div class="section-title">我的知识库</div>
            <a href="#knowledge" style="font-size:13px;color:var(--primary);">查看全部</a>
          </div>
          <div class="flex flex-col gap-3">
            ${Mock.knowledgeBases.slice(0, 4).map(kb => `
              <div class="card" style="padding:16px;cursor:pointer;transition:all 0.15s ease;" onmouseover="this.style.borderColor='var(--primary)'" onmouseout="this.style.borderColor='var(--border-light)'" onclick="App.navigate('knowledge')">
                <div style="display:flex;align-items:center;gap:12px;">
                  <div style="width:40px;height:40px;border-radius:10px;background:${kb.color}15;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;">${kb.icon}</div>
                  <div style="flex:1;min-width:0;">
                    <div style="font-size:14px;font-weight:600;margin-bottom:2px;">${kb.name}</div>
                    <div style="font-size:12px;color:var(--text-muted);">${kb.docs} 篇文档 · ${kb.members} 位成员 · 更新于 ${kb.updatedAt}</div>
                  </div>
                  ${Utils.icon('arrowRight', 16)}
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      </div>
    `;
  },
  init() {
    // 快捷操作卡片点击波纹效果
    document.querySelectorAll('.stat-card').forEach(card => {
      card.addEventListener('mouseenter', function() {
        this.style.transform = 'translateY(-2px)';
        this.style.boxShadow = 'var(--shadow-md)';
        this.style.transition = 'all 0.2s ease';
      });
      card.addEventListener('mouseleave', function() {
        this.style.transform = 'none';
        this.style.boxShadow = 'none';
      });
    });
  }
});
