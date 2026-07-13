/* === 认证与入门页面 === */

// 登录注册页
App.registerPage('login', {
  fullscreen: true,
  title: '登录',
  render() {
    return `
      <div class="login-container">
        <div class="login-left">
          <div style="position: relative; z-index: 1; max-width: 480px;">
            <div class="flex items-center gap-3 mb-8">
              <div style="width:48px;height:48px;background:rgba(255,255,255,0.15);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:28px;">🧠</div>
              <div style="font-size:24px;font-weight:700;">企业知识库</div>
            </div>
            <h1 style="font-size:36px;font-weight:700;line-height:1.3;margin-bottom:16px;">让企业知识<br>真正流动起来</h1>
            <p style="font-size:16px;opacity:0.8;line-height:1.7;margin-bottom:32px;">
              基于 Agentic RAG 架构的智能知识管理平台，支持多模态文档处理、AI 对话问答、知识图谱可视化和 MCP 工具调用。
            </p>
            <div style="display:flex;gap:24px;">
              <div>
                <div style="font-size:32px;font-weight:700;">1,019</div>
                <div style="font-size:13px;opacity:0.6;">知识文档</div>
              </div>
              <div>
                <div style="font-size:32px;font-weight:700;">8,542</div>
                <div style="font-size:13px;opacity:0.6;">AI 问答</div>
              </div>
              <div>
                <div style="font-size:32px;font-weight:700;">92.5%</div>
                <div style="font-size:13px;opacity:0.6;">满意度</div>
              </div>
            </div>
          </div>
        </div>
        <div class="login-right">
          <div class="login-form">
            <div class="login-logo">
              <div style="width:40px;height:40px;background:linear-gradient(135deg,var(--primary),var(--primary-light));border-radius:10px;display:flex;align-items:center;justify-content:center;color:white;font-weight:700;font-size:20px;">🧠</div>
              <div style="font-size:18px;font-weight:600;">企业知识库</div>
            </div>
            <h2 style="font-size:24px;font-weight:700;margin-bottom:8px;">欢迎回来</h2>
            <p style="font-size:14px;color:var(--text-muted);margin-bottom:32px;">登录您的企业知识库账户</p>

            <div class="tabs-pill mb-5" style="width:100%;">
              <div class="tab-pill active" data-tab="password" style="flex:1;text-align:center;">账号密码</div>
              <div class="tab-pill" data-tab="sso" style="flex:1;text-align:center;">企业 SSO</div>
            </div>

            <div id="tab-password" class="tab-content active">
              <div class="form-group">
                <label class="form-label">邮箱</label>
                <input class="form-input" type="email" placeholder="name@company.com" value="zhangming@company.com">
              </div>
              <div class="form-group">
                <label class="form-label">密码</label>
                <input class="form-input" type="password" placeholder="请输入密码" value="12345678">
              </div>
              <div class="flex items-center justify-between mb-5">
                <label class="checkbox"><input type="checkbox" checked>记住我</label>
                <a href="#" style="font-size:13px;">忘记密码？</a>
              </div>
              <button class="btn btn-primary btn-lg btn-block" onclick="App.navigate('home')">登录</button>
            </div>

            <div id="tab-sso" class="tab-content" style="display:none;">
              <p style="font-size:14px;color:var(--text-muted);margin-bottom:20px;line-height:1.6;">选择您的企业登录方式，将通过 SSO 单点登录跳转验证。</p>
              <button class="btn btn-secondary btn-lg btn-block mb-3" onclick="App.navigate('sso-callback')">
                <span style="font-size:18px;">🏢</span> 企业 SSO 登录
              </button>
              <button class="btn btn-secondary btn-lg btn-block mb-3" onclick="App.navigate('sso-callback')">
                <span style="font-size:18px;">🔗</span> 飞书扫码登录
              </button>
              <button class="btn btn-secondary btn-lg btn-block" onclick="App.navigate('sso-callback')">
                <span style="font-size:18px;">💬</span> 企业微信登录
              </button>
            </div>

            <div style="text-align:center;margin-top:32px;font-size:13px;color:var(--text-muted);">
              还没有账户？<a href="#" onclick="App.navigate('onboarding');return false;">申请试用</a>
            </div>
          </div>
        </div>
      </div>
    `;
  },
  init() {
    document.querySelectorAll('.login-form .tab-pill').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.login-form .tab-pill').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.querySelectorAll('.login-form .tab-content').forEach(c => { c.style.display = 'none'; c.classList.remove('active'); });
        document.getElementById('tab-' + tab.dataset.tab).style.display = 'block';
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      });
    });
  }
});

// SSO 回调页
App.registerPage('sso-callback', {
  fullscreen: true,
  title: 'SSO 验证中',
  render() {
    return `
      <div class="fullscreen-page">
        <div style="text-align:center;">
          <div style="width:64px;height:64px;margin:0 auto 24px;position:relative;">
            <svg width="64" height="64" viewBox="0 0 64 64">
              <circle cx="32" cy="32" r="28" fill="none" stroke="var(--primary-bg)" stroke-width="4"/>
              <circle cx="32" cy="32" r="28" fill="none" stroke="var(--primary)" stroke-width="4" stroke-linecap="round" stroke-dasharray="176" stroke-dashoffset="44" transform="rotate(-90 32 32)">
                <animate attributeName="stroke-dashoffset" values="176;0;176" dur="2s" repeatCount="indefinite"/>
              </circle>
            </svg>
            <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:24px;">🧠</div>
          </div>
          <h2 style="font-size:20px;font-weight:600;margin-bottom:8px;">正在验证身份...</h2>
          <p style="font-size:14px;color:var(--text-muted);">请稍候，正在通过 SSO 完成身份验证</p>
          <div style="margin-top:32px;display:flex;gap:8px;justify-content:center;">
            <div class="typing-indicator"><span></span><span></span><span></span></div>
          </div>
          <button class="btn btn-ghost mt-6" onclick="App.navigate('home')">跳过等待</button>
        </div>
      </div>
    `;
  },
  init() {
    setTimeout(() => { if (App.currentRoute === 'sso-callback') App.navigate('onboarding'); }, 3000);
  }
});

// 新手引导页
App.registerPage('onboarding', {
  fullscreen: true,
  title: '新手引导',
  render() {
    return `
      <div style="min-height:100vh;background:var(--bg);display:flex;align-items:center;justify-content:center;padding:24px;">
        <div style="max-width:640px;width:100%;">
          <div style="text-align:center;margin-bottom:40px;">
            <div style="font-size:48px;margin-bottom:16px;">🎉</div>
            <h1 style="font-size:28px;font-weight:700;margin-bottom:8px;">欢迎加入企业知识库！</h1>
            <p style="color:var(--text-muted);">让我们用几分钟完成初始设置</p>
          </div>

          <div class="card shadow-md" style="padding:32px;">
            <div class="flex items-center justify-between mb-6">
              <div style="display:flex;gap:8px;">
                ${[1,2,3,4].map((s, i) => `
                  <div style="width:32px;height:4px;border-radius:2px;background:${i <= 0 ? 'var(--primary)' : 'var(--border-light)'};"></div>
                `).join('')}
              </div>
              <span style="font-size:13px;color:var(--text-muted);">步骤 1/4</span>
            </div>

            <div id="onboarding-step-1" class="onboarding-step active">
              <h3 style="font-size:18px;font-weight:600;margin-bottom:8px;">完善个人信息</h3>
              <p style="font-size:14px;color:var(--text-muted);margin-bottom:24px;">让我们更好地了解您，为您提供个性化推荐</p>
              <div class="grid grid-2 gap-4">
                <div class="form-group">
                  <label class="form-label">姓名</label>
                  <input class="form-input" placeholder="请输入姓名" value="张明">
                </div>
                <div class="form-group">
                  <label class="form-label">部门</label>
                  <select class="form-select">
                    <option>产品中心</option><option>研发部</option><option>市场部</option><option>财务部</option><option>人力资源</option><option>IT部</option>
                  </select>
                </div>
                <div class="form-group">
                  <label class="form-label">职位</label>
                  <input class="form-input" placeholder="请输入职位" value="知识管理员">
                </div>
                <div class="form-group">
                  <label class="form-label">角色</label>
                  <select class="form-select">
                    <option>知识管理员</option><option>编辑者</option><option>查看者</option><option>系统管理员</option>
                  </select>
                </div>
              </div>
            </div>

            <div id="onboarding-step-2" class="onboarding-step" style="display:none;">
              <h3 style="font-size:18px;font-weight:600;margin-bottom:8px;">选择感兴趣的知识库</h3>
              <p style="font-size:14px;color:var(--text-muted);margin-bottom:24px;">我们将根据您的选择推荐相关内容</p>
              <div class="grid grid-2 gap-3">
                ${Mock.knowledgeBases.map(kb => `
                  <div class="kb-select-item" onclick="this.classList.toggle('selected')" style="border:2px solid var(--border-light);border-radius:12px;padding:16px;cursor:pointer;transition:all 0.15s ease;">
                    <div style="font-size:28px;margin-bottom:8px;">${kb.icon}</div>
                    <div style="font-weight:600;font-size:14px;margin-bottom:4px;">${kb.name}</div>
                    <div style="font-size:12px;color:var(--text-muted);">${kb.docs} 篇文档</div>
                  </div>
                `).join('')}
              </div>
            </div>

            <div id="onboarding-step-3" class="onboarding-step" style="display:none;">
              <h3 style="font-size:18px;font-weight:600;margin-bottom:8px;">上传首批文档</h3>
              <p style="font-size:14px;color:var(--text-muted);margin-bottom:24px;">拖拽文件或选择文件开始构建您的知识库</p>
              <div class="upload-zone" onclick="App.toast('文件选择器（原型演示）', 'info')">
                <div class="upload-zone-icon">📁</div>
                <div class="upload-zone-title">拖拽文件到此处</div>
                <div class="upload-zone-hint">支持 PDF、Word、Excel、Markdown、图片等格式</div>
              </div>
              <p style="text-align:center;margin-top:16px;font-size:13px;color:var(--text-muted);">也可以稍后上传</p>
            </div>

            <div id="onboarding-step-4" class="onboarding-step" style="display:none;">
              <div style="text-align:center;padding:24px 0;">
                <div style="font-size:56px;margin-bottom:16px;">✅</div>
                <h3 style="font-size:20px;font-weight:600;margin-bottom:8px;">设置完成！</h3>
                <p style="font-size:14px;color:var(--text-muted);margin-bottom:32px;">您的企业知识库已准备就绪</p>
                <div class="grid grid-3 gap-4" style="text-align:left;">
                  <div class="card" style="padding:16px;text-align:center;">
                    <div style="font-size:28px;margin-bottom:8px;">💬</div>
                    <div style="font-weight:600;font-size:14px;">开始 AI 对话</div>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">向 AI 提问</div>
                  </div>
                  <div class="card" style="padding:16px;text-align:center;">
                    <div style="font-size:28px;margin-bottom:8px;">📚</div>
                    <div style="font-weight:600;font-size:14px;">浏览知识库</div>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">探索已有知识</div>
                  </div>
                  <div class="card" style="padding:16px;text-align:center;">
                    <div style="font-size:28px;margin-bottom:8px;">📤</div>
                    <div style="font-weight:600;font-size:14px;">上传文档</div>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">贡献新知识</div>
                  </div>
                </div>
              </div>
            </div>

            <div class="flex items-center justify-between mt-6">
              <button class="btn btn-ghost" id="onboard-prev" onclick="onboardStep(-1)" style="visibility:hidden;">上一步</button>
              <button class="btn btn-primary" id="onboard-next" onclick="onboardStep(1)">下一步</button>
            </div>
          </div>

          <div style="text-align:center;margin-top:24px;">
            <a href="#" onclick="App.navigate('home');return false;" style="font-size:13px;color:var(--text-muted);">跳过引导</a>
          </div>
        </div>
      </div>
    `;
  },
  init() {
    window.onboardStep = function(dir) {
      const steps = document.querySelectorAll('.onboarding-step');
      const bars = document.querySelectorAll('[style*="border-radius:2px"]');
      let current = 0;
      steps.forEach((s, i) => { if (s.style.display !== 'none') current = i; });
      let next = Math.max(0, Math.min(steps.length - 1, current + dir));
      if (next === current) return;
      steps[current].style.display = 'none';
      steps[next].style.display = 'block';
      bars.forEach((b, i) => { b.style.background = i <= next ? 'var(--primary)' : 'var(--border-light)'; });
      document.getElementById('onboard-prev').style.visibility = next > 0 ? 'visible' : 'hidden';
      const nextBtn = document.getElementById('onboard-next');
      if (next === steps.length - 1) { nextBtn.textContent = '进入知识库'; nextBtn.onclick = () => App.navigate('home'); }
      else { nextBtn.textContent = '下一步'; nextBtn.onclick = () => onboardStep(1); }
      const stepLabel = document.querySelector('[style*="步骤"]');
      if (stepLabel) stepLabel.textContent = `步骤 ${next + 1}/4`;
    };
  }
});
