/* === 场景应用页面 === */

// ============================================
// 页面1: 入职助手 (scenes/onboarding)
// ============================================
App.registerPage('scenes/onboarding', {
  title: '入职助手',
  render() {
    // 今日任务数据
    const tasks = [
      { id: 1, title: '账号设置', desc: '完善个人信息、设置密码、绑定邮箱', status: 'done', icon: 'settings', progress: 100 },
      { id: 2, title: '阅读员工手册', desc: '了解公司制度、考勤规范、福利政策', status: 'doing', icon: 'book', progress: 60 },
      { id: 3, title: '认识团队成员', desc: '加入团队群组、了解组织架构', status: 'todo', icon: 'users', progress: 0 },
      { id: 4, title: '配置开发环境', desc: '安装开发工具、配置 Git、获取代码权限', status: 'todo', icon: 'cpu', progress: 0 },
    ];

    // 常用知识卡片
    const quickKnowledge = [
      { title: '员工手册（2026修订版）', kb: '人力资源制度库', icon: '👥', views: 678, color: '#00B884' },
      { title: '新员工入职Onboarding清单', kb: '人力资源制度库', icon: '📋', views: 432, color: '#00B884' },
      { title: '差旅费用报销操作指南', kb: '财务报销指南', icon: '💰', color: '#FF3B5C', views: 567 },
      { title: 'API接口设计规范 v3.0', kb: '产品研发知识库', icon: '🚀', color: '#4B3FE3', views: 89 },
    ];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <h1 class="page-title">入职助手</h1>
            <p class="page-subtitle">欢迎加入企业知识库团队，让我们用几天时间完成入职</p>
          </div>
          <div class="flex items-center gap-3">
            <button class="btn btn-secondary" onclick="App.toast('入职指南（原型演示）', 'info')">${Utils.icon('help', 16)} 入职指南</button>
            <button class="btn btn-primary" onclick="App.navigate('chat')">${Utils.icon('chat', 16)} 问 AI 助手</button>
          </div>
        </div>
      </div>

      <!-- 欢迎横幅 + 入职进度 -->
      <div class="card mb-6" style="background:linear-gradient(135deg,#4B3FE3 0%,#6B5FF7 100%);color:white;border:none;overflow:hidden;position:relative;">
        <div style="position:absolute;top:-40px;right:-40px;width:240px;height:240px;border-radius:50%;background:rgba(255,255,255,0.08);"></div>
        <div style="position:absolute;bottom:-60px;right:80px;width:160px;height:160px;border-radius:50%;background:rgba(255,255,255,0.05);"></div>
        <div class="card-body" style="padding:32px;position:relative;z-index:1;">
          <div class="flex items-center justify-between">
            <div style="flex:1;">
              <div style="font-size:13px;opacity:0.8;margin-bottom:8px;">欢迎，${Mock.currentUser.name}！</div>
              <h2 style="font-size:24px;font-weight:700;margin-bottom:8px;">开启您的企业知识库之旅 🎉</h2>
              <p style="font-size:14px;opacity:0.85;line-height:1.6;max-width:480px;">
                完成入职任务，快速融入团队。已加入第 3 天，已完成 3/8 个步骤，继续加油！
              </p>
              <div class="flex items-center gap-3 mt-5">
                <div style="flex:1;max-width:300px;">
                  <div class="flex items-center justify-between mb-2">
                    <span style="font-size:13px;opacity:0.9;">入职进度</span>
                    <span style="font-size:13px;font-weight:600;">3/8 · 37.5%</span>
                  </div>
                  <div class="progress-bar" style="background:rgba(255,255,255,0.2);">
                    <div class="progress-bar-fill" style="width:37.5%;background:white;"></div>
                  </div>
                </div>
                <button class="btn btn-sm" style="background:rgba(255,255,255,0.2);color:white;backdrop-filter:blur(10px);" onclick="App.toast('查看完整入职清单', 'info')">
                  ${Utils.icon('check', 14)} 查看清单
                </button>
              </div>
            </div>
            <div style="font-size:96px;line-height:1;opacity:0.9;">🎯</div>
          </div>
        </div>
      </div>

      <div class="grid gap-6" style="grid-template-columns:1fr 320px;">
        <!-- 左侧主区域 -->
        <div>
          <!-- 今日任务 -->
          <div class="section-header">
            <div class="section-title">今日任务</div>
            <span class="text-sm text-muted">完成 ${tasks.filter(t => t.status === 'done').length}/${tasks.length} 项</span>
          </div>
          <div class="flex flex-col gap-3 mb-6">
            ${tasks.map(t => {
              const statusMap = {
                done: { badge: 'badge-success', label: '已完成', icon: 'check', color: 'var(--success)' },
                doing: { badge: 'badge-warning', label: '进行中', icon: 'clock', color: 'var(--warning)' },
                todo: { badge: 'badge-neutral', label: '待完成', icon: 'circle', color: 'var(--text-muted)' },
              };
              const info = statusMap[t.status];
              // 预计算按钮与提示文案，避免模板字符串嵌套解析问题
              const bg = t.status === 'done' ? 'var(--success-bg)' : (t.status === 'doing' ? 'var(--warning-bg)' : 'var(--surface-hover)');
              const iconHtml = t.status === 'done' ? Utils.icon('check', 24) : Utils.icon(t.icon, 22);
              const titleStyle = t.status === 'done' ? 'color:var(--text-muted);text-decoration:line-through;' : '';
              const btnClass = t.status === 'done' ? 'ghost' : 'secondary';
              const btnLabel = t.status === 'done' ? '查看' : (t.status === 'doing' ? '继续' : '开始');
              const toastMsg = t.status === 'done' ? '该任务已完成' : ('开始任务：' + t.title);
              const toastType = t.status === 'done' ? 'info' : 'success';
              return `
                <div class="card onboarding-task" data-id="${t.id}" style="cursor:pointer;transition:all 0.15s ease;">
                  <div class="card-body" style="display:flex;align-items:center;gap:16px;">
                    <div style="width:48px;height:48px;border-radius:12px;background:${bg};display:flex;align-items:center;justify-content:center;color:${info.color};flex-shrink:0;">
                      ${iconHtml}
                    </div>
                    <div style="flex:1;min-width:0;">
                      <div class="flex items-center gap-2 mb-1">
                        <span style="font-size:15px;font-weight:600;${titleStyle}">${t.title}</span>
                        <span class="badge ${info.badge}">${info.label}</span>
                      </div>
                      <div style="font-size:13px;color:var(--text-muted);">${t.desc}</div>
                      ${t.status === 'doing' ? `
                        <div style="margin-top:8px;max-width:240px;">
                          <div class="progress-bar"><div class="progress-bar-fill warning" style="width:${t.progress}%;"></div></div>
                        </div>
                      ` : ''}
                    </div>
                    <button class="btn btn-${btnClass} btn-sm" data-toast="${Utils.escape(toastMsg)}" data-toast-type="${toastType}">
                      ${btnLabel}
                    </button>
                  </div>
                </div>
              `;
            }).join('')}
          </div>

          <!-- 常用知识 -->
          <div class="section-header">
            <div class="section-title">常用知识</div>
            <a href="#knowledge" class="btn-link">查看全部 →</a>
          </div>
          <div class="grid grid-auto gap-4 mb-6">
            ${quickKnowledge.map(k => `
              <div class="card quick-knowledge-card" style="cursor:pointer;transition:all 0.15s ease;">
                <div class="card-body">
                  <div class="flex items-center gap-3 mb-3">
                    <div style="width:40px;height:40px;border-radius:10px;background:${k.color}20;color:${k.color};display:flex;align-items:center;justify-content:center;font-size:22px;">${k.icon}</div>
                    <div style="flex:1;min-width:0;">
                      <div style="font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${k.title}</div>
                      <div style="font-size:12px;color:var(--text-muted);margin-top:2px;">${k.kb}</div>
                    </div>
                  </div>
                  <div class="flex items-center justify-between" style="padding-top:12px;border-top:1px solid var(--border-light);">
                    <span class="text-xs text-muted">${Utils.icon('eye', 12)} ${k.views} 次查看</span>
                    <span class="text-xs text-primary">${Utils.icon('arrowRight', 12)} 阅读</span>
                  </div>
                </div>
              </div>
            `).join('')}
          </div>

          <!-- AI 助手入口卡片 -->
          <div class="card" style="background:linear-gradient(135deg,#EEEBFE 0%,#E0DBFC 100%);border-color:var(--primary-bg-hover);">
            <div class="card-body" style="display:flex;align-items:center;gap:20px;padding:24px;">
              <div style="width:56px;height:56px;border-radius:14px;background:linear-gradient(135deg,var(--primary),var(--primary-light));display:flex;align-items:center;justify-content:center;font-size:32px;flex-shrink:0;">🤖</div>
              <div style="flex:1;">
                <div style="font-size:16px;font-weight:600;margin-bottom:4px;">有问题随时问 AI 助手</div>
                <div style="font-size:13px;color:var(--text-secondary);line-height:1.6;">
                  入职过程中遇到任何问题，AI 助手可基于企业知识库为您即时解答。支持文档检索、流程指引、人员查找等。
                </div>
              </div>
              <button class="btn btn-primary" onclick="App.navigate('chat')">
                ${Utils.icon('chat', 16)}
                <span>开始对话</span>
              </button>
            </div>
          </div>
        </div>

        <!-- 右侧: 入职导师 -->
        <aside>
          <div class="card mb-4" style="position:sticky;top:72px;">
            <div class="card-body">
              <div class="section-header" style="margin-bottom:16px;">
                <div class="section-title" style="font-size:15px;">入职导师</div>
                <span class="badge badge-success">在线</span>
              </div>
              <div class="flex items-center gap-3 mb-4">
                <div class="avatar avatar-xl" style="background:${Utils.avatarColor('陈静')};">陈</div>
                <div style="flex:1;">
                  <div style="font-size:16px;font-weight:600;">陈静</div>
                  <div style="font-size:12px;color:var(--text-muted);margin-top:2px;">IT部 · 系统管理员</div>
                  <div style="font-size:12px;color:var(--text-muted);margin-top:2px;">入职导师 · 工龄 5 年</div>
                </div>
              </div>
              <div style="padding:12px;background:var(--surface-muted);border-radius:8px;margin-bottom:16px;">
                <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px;">导师寄语</div>
                <div style="font-size:13px;line-height:1.6;color:var(--text-secondary);">
                  欢迎加入团队！有任何问题随时找我，或直接问 AI 助手。期待和你一起共事。
                </div>
              </div>
              <div class="flex flex-col gap-2 mb-4">
                <button class="btn btn-secondary btn-block btn-sm" onclick="App.navigate('knowledge/qa')">
                  ${Utils.icon('message', 14)} 问答社区
                </button>
                <button class="btn btn-secondary btn-block btn-sm" onclick="App.toast('已预约 1v1 沟通', 'success')">
                  ${Utils.icon('calendar', 14)} 预约 1v1
                </button>
                <button class="btn btn-ghost btn-block btn-sm" onclick="App.toast('已查看导师主页', 'info')">
                  ${Utils.icon('users', 14)} 查看主页
                </button>
              </div>

              <div style="padding-top:16px;border-top:1px solid var(--border-light);">
                <div class="filter-label mb-2">入职进度详情</div>
                <div class="flex flex-col gap-2">
                  ${[
                    { label: '账号设置', done: true },
                    { label: '阅读员工手册', done: true },
                    { label: '配置开发环境', done: true },
                    { label: '认识团队成员', done: false },
                    { label: '完成首次代码提交', done: false },
                    { label: '参加团队周会', done: false },
                    { label: '提交首周总结', done: false },
                    { label: '完成 30 天回顾', done: false },
                  ].map(step => `
                    <div class="flex items-center gap-2 text-sm">
                      <div style="width:18px;height:18px;border-radius:50%;background:${step.done ? 'var(--success)' : 'var(--surface-hover)'};color:white;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
                        ${step.done ? Utils.icon('check', 12) : ''}
                      </div>
                      <span style="${step.done ? 'color:var(--text-muted);text-decoration:line-through;' : ''}">${step.label}</span>
                    </div>
                  `).join('')}
                </div>
              </div>
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  init() {
    // 任务卡片：按钮点击（停止冒泡，触发对应 toast）
    document.querySelectorAll('.onboarding-task').forEach(card => {
      const btn = card.querySelector('button[data-toast]');
      if (btn) {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const msg = btn.dataset.toast || '';
          const type = btn.dataset.toastType || 'info';
          App.toast(msg, type);
        });
      }
      card.addEventListener('mouseenter', () => {
        card.style.borderColor = 'var(--primary)';
        card.style.boxShadow = 'var(--shadow-sm)';
      });
      card.addEventListener('mouseleave', () => {
        card.style.borderColor = '';
        card.style.boxShadow = '';
      });
      card.addEventListener('click', () => {
        const title = card.dataset.id;
        App.toast('打开任务详情 #' + title, 'info');
      });
    });

    // 常用知识卡片点击
    document.querySelectorAll('.quick-knowledge-card').forEach(card => {
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
      card.addEventListener('click', () => {
        App.navigate('knowledge/doc');
      });
    });
  }
});

// ============================================
// 页面2: IT 服务台 (scenes/it-helpdesk)
// ============================================
App.registerPage('scenes/it-helpdesk', {
  title: 'IT 服务台',
  render() {
    // 预设工单数据
    const tickets = [
      {
        id: 'IT-2026-0781', title: 'VPN连接异常，无法访问内网',
        type: '网络', typeColor: 'info', priority: 'high', priorityColor: 'danger',
        status: '处理中', statusColor: 'warning', time: '2026-07-04 14:30',
        submitter: '李华', dept: '研发部',
        desc: '使用公司 VPN 连接内网时频繁断开，已尝试重连多次无效。系统提示「认证超时」，影响正常办公。',
        steps: [
          { title: '工单提交', time: '14:30', status: 'done', desc: '李华 提交工单' },
          { title: '已分配', time: '14:35', status: 'done', desc: '分配给 IT运维组 · 陈静' },
          { title: '正在处理', time: '14:42', status: 'current', desc: '陈静 正在排查 VPN 网关日志' },
          { title: '等待验证', time: '-', status: 'pending', desc: '待处理完成后验证' },
        ],
        aiSuggestion: '根据日志分析，疑似 VPN 网关证书过期导致。建议：1) 检查网关证书有效期；2) 临时切换备用网关；3) 通知受影响用户重置密码后重连。参考文档《服务器故障排查SOP》第3章。',
      },
      {
        id: 'IT-2026-0780', title: '邮箱空间不足，无法收发邮件',
        type: '邮箱', typeColor: 'primary', priority: 'medium', priorityColor: 'warning',
        status: '待处理', statusColor: 'neutral', time: '2026-07-04 13:15',
        submitter: '王芳', dept: '市场部',
        desc: '邮箱提示空间已满（已用 9.8GB / 10GB），无法接收新邮件，需要清理或扩容。',
        steps: [
          { title: '工单提交', time: '13:15', status: 'done', desc: '王芳 提交工单' },
          { title: '已分配', time: '-', status: 'pending', desc: '等待分配处理人' },
        ],
        aiSuggestion: '建议先引导用户清理「已删除」「垃圾邮件」「已发送」文件夹中的大附件。如仍不足，可临时扩容至 15GB。长期方案：启用邮件归档策略，自动归档 90 天前邮件。',
      },
      {
        id: 'IT-2026-0779', title: '软件安装申请 - WebStorm 2026',
        type: '软件', typeColor: 'success', priority: 'low', priorityColor: 'neutral',
        status: '已批准', statusColor: 'success', time: '2026-07-04 10:20',
        submitter: '刘强', dept: '研发部',
        desc: '申请安装 WebStorm 2026 正版授权，用于前端开发。',
        steps: [
          { title: '工单提交', time: '10:20', status: 'done', desc: '刘强 提交申请' },
          { title: '主管审批', time: '10:45', status: 'done', desc: '张明 已批准' },
          { title: 'IT处理', time: '11:00', status: 'done', desc: '陈静 已分配授权码' },
          { title: '已完成', time: '11:10', status: 'done', desc: '授权码已发送至邮箱' },
        ],
        aiSuggestion: '该申请符合研发部软件采购规范，已自动匹配授权池中的空闲 License。建议后续建立 License 自动回收机制，离职或转岗时自动释放。',
      },
      {
        id: 'IT-2026-0778', title: '打印机故障 - 3楼打印机无法打印',
        type: '硬件', typeColor: 'warning', priority: 'medium', priorityColor: 'warning',
        status: '已解决', statusColor: 'success', time: '2026-07-04 09:00',
        submitter: '赵伟', dept: '财务部',
        desc: '3楼共享打印机 HP LaserJet 无法打印，状态灯闪烁红灯，已重启无效。',
        steps: [
          { title: '工单提交', time: '09:00', status: 'done', desc: '赵伟 提交工单' },
          { title: '已分配', time: '09:10', status: 'done', desc: '分配给 IT运维组' },
          { title: '现场处理', time: '09:30', status: 'done', desc: '更换硒鼓，清理卡纸' },
          { title: '已解决', time: '09:45', status: 'done', desc: '验证打印正常，关闭工单' },
        ],
        aiSuggestion: '本次故障为硒鼓耗尽 + 卡纸。建议：1) 在打印机上贴耗材余量提示；2) 设置硒鼓低于10%时自动告警；3) 建立耗材备件库存。',
      },
      {
        id: 'IT-2026-0777', title: '账号权限申请 - 知识库编辑权限',
        type: '账号', typeColor: 'info', priority: 'low', priorityColor: 'neutral',
        status: '已解决', statusColor: 'success', time: '2026-07-03 16:00',
        submitter: '孙莉', dept: '人力资源',
        desc: '申请「产品研发知识库」的编辑权限，用于协作维护入职相关文档。',
        steps: [
          { title: '工单提交', time: '16:00', status: 'done', desc: '孙莉 提交申请' },
          { title: '知识库管理员审批', time: '16:30', status: 'done', desc: '张明 已批准' },
          { title: '权限开通', time: '17:00', status: 'done', desc: '已开通编辑权限' },
        ],
        aiSuggestion: '该权限申请符合最小权限原则。建议建立知识库权限定期审计机制，每季度复查一次，回收不再需要的权限。',
      },
      {
        id: 'IT-2026-0776', title: '系统访问缓慢 - 知识库平台加载慢',
        type: '系统', typeColor: 'danger', priority: 'high', priorityColor: 'danger',
        status: '已解决', statusColor: 'success', time: '2026-07-03 14:00',
        submitter: '周杰', dept: '运营部',
        desc: '访问企业知识库平台时页面加载缓慢，平均 5 秒以上，影响使用体验。',
        steps: [
          { title: '工单提交', time: '14:00', status: 'done', desc: '周杰 提交工单' },
          { title: '已分配', time: '14:05', status: 'done', desc: '分配给 IT运维组' },
          { title: '问题定位', time: '14:30', status: 'done', desc: 'CDN 缓存命中率下降至 42%' },
          { title: '已解决', time: '15:00', status: 'done', desc: '优化缓存策略，命中率回升至 89%' },
        ],
        aiSuggestion: '根因：CDN 缓存策略配置不当导致命中率下降。已优化 vary: Cookie 策略。建议增加缓存命中率监控告警，阈值设为 70%。',
      },
    ];

    return `
      <div class="page-header">
        <div class="flex items-center justify-between">
          <div>
            <h1 class="page-title">IT 服务台</h1>
            <p class="page-subtitle">提交和处理 IT 工单，AI 助手提供智能解决方案</p>
          </div>
          <div class="flex items-center gap-3">
            <button class="btn btn-secondary" onclick="App.toast('已导出工单报表', 'success')">${Utils.icon('download', 16)} 导出报表</button>
            <button class="btn btn-primary" id="newTicketBtn">${Utils.icon('plus', 16)} 提交工单</button>
          </div>
        </div>
      </div>

      <!-- 统计卡片 -->
      <div class="grid grid-4 gap-4 mb-6">
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--warning-bg);color:var(--warning);">${Utils.icon('clock', 20)}</div>
            <span class="badge badge-warning">待处理</span>
          </div>
          <div class="stat-card-value">${tickets.filter(t => t.status === '待处理').length}</div>
          <div class="stat-card-label">待处理工单</div>
          <div class="stat-card-trend up">↑ 2 较昨日</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--primary-bg);color:var(--primary);">${Utils.icon('plus', 20)}</div>
            <span class="stat-card-trend up">↑ 15%</span>
          </div>
          <div class="stat-card-value">${tickets.filter(t => t.time.includes('07-04')).length}</div>
          <div class="stat-card-label">今日新增</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--info-bg);color:var(--info);">${Utils.icon('zap', 20)}</div>
            <span class="stat-card-trend down">↓ 8%</span>
          </div>
          <div class="stat-card-value">2.4h</div>
          <div class="stat-card-label">平均响应时间</div>
        </div>
        <div class="stat-card">
          <div class="flex items-center justify-between">
            <div class="stat-card-icon" style="background:var(--success-bg);color:var(--success);">${Utils.icon('star', 20)}</div>
            <span class="stat-card-trend up">↑ 3.2%</span>
          </div>
          <div class="stat-card-value">94.6%</div>
          <div class="stat-card-label">满意度</div>
        </div>
      </div>

      <div class="grid gap-5" style="grid-template-columns:1fr 380px;">
        <!-- 左侧: 工单列表 -->
        <div>
          <div class="card">
            <div class="card-header">
              <div class="flex items-center gap-3">
                <div class="card-title">工单列表</div>
                <span class="badge badge-neutral">${tickets.length} 条</span>
              </div>
              <div class="flex items-center gap-2">
                <div class="topbar-search" style="width:200px;">
                  <span class="topbar-search-icon">${Utils.icon('search', 14)}</span>
                  <input type="text" id="ticketSearch" placeholder="搜索工单..." style="height:32px;font-size:13px;padding-left:30px;" />
                </div>
                <div class="tabs-pill" id="ticketFilter">
                  <div class="tab-pill active" data-status="all" style="padding:4px 12px;font-size:12px;">全部</div>
                  <div class="tab-pill" data-status="待处理" style="padding:4px 12px;font-size:12px;">待处理</div>
                  <div class="tab-pill" data-status="处理中" style="padding:4px 12px;font-size:12px;">处理中</div>
                  <div class="tab-pill" data-status="已解决" style="padding:4px 12px;font-size:12px;">已解决</div>
                </div>
              </div>
            </div>
            <div class="table-wrap" style="border:none;border-radius:0;">
              <table class="data-table" id="ticketTable">
                <thead>
                  <tr>
                    <th>工单号</th>
                    <th>标题</th>
                    <th>类型</th>
                    <th>优先级</th>
                    <th>状态</th>
                    <th>提交时间</th>
                    <th>提交人</th>
                  </tr>
                </thead>
                <tbody>
                  ${tickets.map(t => `
                    <tr class="ticket-row" data-id="${t.id}" data-status="${t.status}" style="cursor:pointer;">
                      <td><span style="font-family:var(--font-mono);font-size:12px;color:var(--primary);">${t.id}</span></td>
                      <td style="max-width:240px;"><div style="font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${t.title}</div></td>
                      <td><span class="badge badge-${t.typeColor}">${t.type}</span></td>
                      <td><span class="badge badge-${t.priorityColor}">${t.priority === 'high' ? '高' : t.priority === 'medium' ? '中' : '低'}</span></td>
                      <td><span class="badge badge-${t.statusColor}">${t.status}</span></td>
                      <td><span class="text-xs text-muted">${t.time}</span></td>
                      <td><span class="text-sm">${t.submitter}</span><div class="text-xs text-muted">${t.dept}</div></td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>

          <!-- 底部提交按钮 -->
          <div class="flex items-center justify-between mt-4">
            <span class="text-sm text-muted">共 ${tickets.length} 条工单 · 显示全部</span>
            <button class="btn btn-primary btn-lg" id="newTicketBtn2">${Utils.icon('plus', 16)} 提交新工单</button>
          </div>
        </div>

        <!-- 右侧: 工单详情面板 -->
        <aside>
          <div class="card" id="ticketDetail" style="position:sticky;top:72px;">
            <div class="card-body" id="ticketDetailBody">
              ${this.renderTicketDetail(tickets[0])}
            </div>
          </div>
        </aside>
      </div>
    `;
  },
  // 渲染工单详情
  renderTicketDetail(t) {
    return `
      <div class="flex items-center justify-between mb-3">
        <span style="font-family:var(--font-mono);font-size:12px;color:var(--primary);">${t.id}</span>
        <div class="flex items-center gap-2">
          <span class="badge badge-${t.statusColor}">${t.status}</span>
          <span class="badge badge-${t.priorityColor}">${t.priority === 'high' ? '高优先级' : t.priority === 'medium' ? '中优先级' : '低优先级'}</span>
        </div>
      </div>
      <h3 style="font-size:16px;font-weight:600;margin-bottom:12px;line-height:1.5;">${t.title}</h3>
      <div class="flex items-center gap-3 mb-4 text-xs text-muted">
        <span>${Utils.icon('users', 12)} ${t.submitter} · ${t.dept}</span>
        <span>${Utils.icon('clock', 12)} ${t.time}</span>
        <span class="badge badge-${t.typeColor}">${t.type}</span>
      </div>

      <div class="filter-label mb-2">问题描述</div>
      <div style="padding:12px;background:var(--surface-muted);border-radius:8px;font-size:13px;line-height:1.7;color:var(--text-secondary);margin-bottom:20px;">
        ${t.desc}
      </div>

      <div class="filter-label mb-3">处理步骤</div>
      <div class="mb-5">
        ${t.steps.map(step => `
          <div class="audit-step">
            <div class="audit-step-icon ${step.status}">${step.status === 'done' ? Utils.icon('check', 14) : step.status === 'current' ? Utils.icon('zap', 14) : step.status === 'pending' ? '·' : ''}</div>
            <div class="audit-step-content">
              <div class="audit-step-title">${step.title}</div>
              <div class="audit-step-desc">${step.time} · ${step.desc}</div>
            </div>
          </div>
        `).join('')}
      </div>

      <!-- AI 建议 -->
      <div class="card" style="background:linear-gradient(135deg,#EEEBFE 0%,#F5F3FE 100%);border-color:var(--primary-bg-hover);">
        <div class="card-body">
          <div class="flex items-center gap-2 mb-3">
            <div style="width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,var(--primary),var(--primary-light));display:flex;align-items:center;justify-content:center;color:white;">${Utils.icon('sparkles', 14)}</div>
            <span style="font-weight:600;font-size:14px;">AI 建议方案</span>
            <span class="badge badge-primary" style="margin-left:auto;">置信度 92%</span>
          </div>
          <div style="font-size:13px;line-height:1.7;color:var(--text-secondary);">
            ${t.aiSuggestion}
          </div>
          <div class="flex items-center gap-2 mt-4">
            <button class="btn btn-primary btn-sm" onclick="App.toast('已采纳 AI 建议', 'success')">${Utils.icon('check', 14)} 采纳</button>
            <button class="btn btn-ghost btn-sm" onclick="App.toast('已反馈建议无用', 'info')">${Utils.icon('close', 14)} 不相关</button>
            <button class="btn btn-ghost btn-sm" onclick="App.navigate('chat')">${Utils.icon('chat', 14)} 追问</button>
          </div>
        </div>
      </div>

      <div class="flex items-center gap-2 mt-4">
        ${t.status !== '已解决' ? `
          <button class="btn btn-primary btn-sm btn-block" onclick="App.toast('工单已更新处理状态', 'success')">${Utils.icon('check', 14)} 标记为已解决</button>
          <button class="btn btn-secondary btn-sm" onclick="App.toast('已分配给其他处理人', 'info')">${Utils.icon('users', 14)} 转派</button>
        ` : `
          <button class="btn btn-secondary btn-sm btn-block" onclick="App.toast('已重新打开工单', 'warning')">${Utils.icon('refresh', 14)} 重新打开</button>
        `}
      </div>
    `;
  },
  init() {
    const self = this;
    // 工单数据缓存（用于详情切换）
    const tickets = [
      {
        id: 'IT-2026-0781', title: 'VPN连接异常，无法访问内网',
        type: '网络', typeColor: 'info', priority: 'high', priorityColor: 'danger',
        status: '处理中', statusColor: 'warning', time: '2026-07-04 14:30',
        submitter: '李华', dept: '研发部',
        desc: '使用公司 VPN 连接内网时频繁断开，已尝试重连多次无效。系统提示「认证超时」，影响正常办公。',
        steps: [
          { title: '工单提交', time: '14:30', status: 'done', desc: '李华 提交工单' },
          { title: '已分配', time: '14:35', status: 'done', desc: '分配给 IT运维组 · 陈静' },
          { title: '正在处理', time: '14:42', status: 'current', desc: '陈静 正在排查 VPN 网关日志' },
          { title: '等待验证', time: '-', status: 'pending', desc: '待处理完成后验证' },
        ],
        aiSuggestion: '根据日志分析，疑似 VPN 网关证书过期导致。建议：1) 检查网关证书有效期；2) 临时切换备用网关；3) 通知受影响用户重置密码后重连。参考文档《服务器故障排查SOP》第3章。',
      },
      {
        id: 'IT-2026-0780', title: '邮箱空间不足，无法收发邮件',
        type: '邮箱', typeColor: 'primary', priority: 'medium', priorityColor: 'warning',
        status: '待处理', statusColor: 'neutral', time: '2026-07-04 13:15',
        submitter: '王芳', dept: '市场部',
        desc: '邮箱提示空间已满（已用 9.8GB / 10GB），无法接收新邮件，需要清理或扩容。',
        steps: [
          { title: '工单提交', time: '13:15', status: 'done', desc: '王芳 提交工单' },
          { title: '已分配', time: '-', status: 'pending', desc: '等待分配处理人' },
        ],
        aiSuggestion: '建议先引导用户清理「已删除」「垃圾邮件」「已发送」文件夹中的大附件。如仍不足，可临时扩容至 15GB。长期方案：启用邮件归档策略，自动归档 90 天前邮件。',
      },
      {
        id: 'IT-2026-0779', title: '软件安装申请 - WebStorm 2026',
        type: '软件', typeColor: 'success', priority: 'low', priorityColor: 'neutral',
        status: '已批准', statusColor: 'success', time: '2026-07-04 10:20',
        submitter: '刘强', dept: '研发部',
        desc: '申请安装 WebStorm 2026 正版授权，用于前端开发。',
        steps: [
          { title: '工单提交', time: '10:20', status: 'done', desc: '刘强 提交申请' },
          { title: '主管审批', time: '10:45', status: 'done', desc: '张明 已批准' },
          { title: 'IT处理', time: '11:00', status: 'done', desc: '陈静 已分配授权码' },
          { title: '已完成', time: '11:10', status: 'done', desc: '授权码已发送至邮箱' },
        ],
        aiSuggestion: '该申请符合研发部软件采购规范，已自动匹配授权池中的空闲 License。建议后续建立 License 自动回收机制，离职或转岗时自动释放。',
      },
      {
        id: 'IT-2026-0778', title: '打印机故障 - 3楼打印机无法打印',
        type: '硬件', typeColor: 'warning', priority: 'medium', priorityColor: 'warning',
        status: '已解决', statusColor: 'success', time: '2026-07-04 09:00',
        submitter: '赵伟', dept: '财务部',
        desc: '3楼共享打印机 HP LaserJet 无法打印，状态灯闪烁红灯，已重启无效。',
        steps: [
          { title: '工单提交', time: '09:00', status: 'done', desc: '赵伟 提交工单' },
          { title: '已分配', time: '09:10', status: 'done', desc: '分配给 IT运维组' },
          { title: '现场处理', time: '09:30', status: 'done', desc: '更换硒鼓，清理卡纸' },
          { title: '已解决', time: '09:45', status: 'done', desc: '验证打印正常，关闭工单' },
        ],
        aiSuggestion: '本次故障为硒鼓耗尽 + 卡纸。建议：1) 在打印机上贴耗材余量提示；2) 设置硒鼓低于10%时自动告警；3) 建立耗材备件库存。',
      },
      {
        id: 'IT-2026-0777', title: '账号权限申请 - 知识库编辑权限',
        type: '账号', typeColor: 'info', priority: 'low', priorityColor: 'neutral',
        status: '已解决', statusColor: 'success', time: '2026-07-03 16:00',
        submitter: '孙莉', dept: '人力资源',
        desc: '申请「产品研发知识库」的编辑权限，用于协作维护入职相关文档。',
        steps: [
          { title: '工单提交', time: '16:00', status: 'done', desc: '孙莉 提交申请' },
          { title: '知识库管理员审批', time: '16:30', status: 'done', desc: '张明 已批准' },
          { title: '权限开通', time: '17:00', status: 'done', desc: '已开通编辑权限' },
        ],
        aiSuggestion: '该权限申请符合最小权限原则。建议建立知识库权限定期审计机制，每季度复查一次，回收不再需要的权限。',
      },
      {
        id: 'IT-2026-0776', title: '系统访问缓慢 - 知识库平台加载慢',
        type: '系统', typeColor: 'danger', priority: 'high', priorityColor: 'danger',
        status: '已解决', statusColor: 'success', time: '2026-07-03 14:00',
        submitter: '周杰', dept: '运营部',
        desc: '访问企业知识库平台时页面加载缓慢，平均 5 秒以上，影响使用体验。',
        steps: [
          { title: '工单提交', time: '14:00', status: 'done', desc: '周杰 提交工单' },
          { title: '已分配', time: '14:05', status: 'done', desc: '分配给 IT运维组' },
          { title: '问题定位', time: '14:30', status: 'done', desc: 'CDN 缓存命中率下降至 42%' },
          { title: '已解决', time: '15:00', status: 'done', desc: '优化缓存策略，命中率回升至 89%' },
        ],
        aiSuggestion: '根因：CDN 缓存策略配置不当导致命中率下降。已优化 vary: Cookie 策略。建议增加缓存命中率监控告警，阈值设为 70%。',
      },
    ];

    // 工单行点击 → 切换详情
    document.querySelectorAll('.ticket-row').forEach(row => {
      row.addEventListener('click', () => {
        // 高亮当前行
        document.querySelectorAll('.ticket-row').forEach(r => r.style.background = '');
        row.style.background = 'var(--primary-bg)';

        const id = row.dataset.id;
        const ticket = tickets.find(t => t.id === id);
        if (ticket) {
          const body = document.getElementById('ticketDetailBody');
          if (body) {
            body.innerHTML = self.renderTicketDetail(ticket);
            body.classList.add('fade-in');
            setTimeout(() => body.classList.remove('fade-in'), 300);
          }
        }
      });
    });

    // 默认高亮第一行
    const firstRow = document.querySelector('.ticket-row');
    if (firstRow) firstRow.style.background = 'var(--primary-bg)';

    // 状态筛选
    const filters = document.querySelectorAll('#ticketFilter .tab-pill');
    filters.forEach(f => {
      f.addEventListener('click', () => {
        filters.forEach(x => x.classList.remove('active'));
        f.classList.add('active');
        const status = f.dataset.status;
        document.querySelectorAll('.ticket-row').forEach(row => {
          row.style.display = (status === 'all' || row.dataset.status === status) ? '' : 'none';
        });
      });
    });

    // 搜索
    const search = document.getElementById('ticketSearch');
    if (search) {
      search.addEventListener('input', (e) => {
        const kw = e.target.value.toLowerCase();
        document.querySelectorAll('.ticket-row').forEach(row => {
          const text = row.textContent.toLowerCase();
          row.style.display = text.includes(kw) ? '' : 'none';
        });
      });
    }

    // 提交工单按钮 → 打开模态框
    const openTicketModal = () => {
      App.modal({
        title: '提交新工单',
        size: 'modal-lg',
        body: `
          <div class="grid grid-2 gap-4">
            <div class="form-group">
              <label class="form-label">工单类型 <span class="required">*</span></label>
              <select class="form-select">
                <option>网络</option><option>邮箱</option><option>软件</option>
                <option>硬件</option><option>账号</option><option>系统</option><option>其他</option>
              </select>
            </div>
            <div class="form-group">
              <label class="form-label">优先级 <span class="required">*</span></label>
              <select class="form-select">
                <option value="high">高 - 影响业务运行</option>
                <option value="medium" selected>中 - 影响工作效率</option>
                <option value="low">低 - 一般咨询</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">工单标题 <span class="required">*</span></label>
            <input class="form-input" placeholder="简要描述您的问题，如：VPN连接异常">
          </div>
          <div class="form-group">
            <label class="form-label">详细描述 <span class="required">*</span></label>
            <textarea class="form-textarea" rows="5" placeholder="请详细描述问题现象、复现步骤、已尝试的解决方案..."></textarea>
          </div>
          <div class="form-group">
            <label class="form-label">附件（可选）</label>
            <div class="upload-zone" style="padding:24px;" onclick="App.toast('附件上传（原型演示）', 'info')">
              <div style="font-size:32px;margin-bottom:8px;">📎</div>
              <div class="upload-zone-title" style="font-size:14px;">拖拽文件或点击上传</div>
              <div class="upload-zone-hint" style="font-size:12px;">支持截图、日志、文档等，单文件最大 20MB</div>
            </div>
          </div>
          <div style="padding:12px;background:var(--primary-bg);border-radius:8px;font-size:13px;color:var(--primary-dark);">
            ${Utils.icon('sparkles', 14)} <strong>AI 助手提示：</strong>提交后，AI 将自动分析您的问题并推荐解决方案，预计响应时间 2 分钟内。
          </div>
        `,
        footer: true,
        confirmText: '提交工单',
        onConfirm: (overlay) => {
          App.toast('工单已提交，工单号 IT-2026-0782', 'success');
          overlay.remove();
        }
      });
    };

    const btn1 = document.getElementById('newTicketBtn');
    const btn2 = document.getElementById('newTicketBtn2');
    if (btn1) btn1.addEventListener('click', openTicketModal);
    if (btn2) btn2.addEventListener('click', openTicketModal);
  }
});
