/* ============================================
   领养人端 H5 门户 - 应用逻辑（对接 Django API）
   ============================================ */

const PUBLIC_MODE = window.TNR_ADOPTER_PUBLIC;

// === 全局状态 ===
let currentPage = 'hall';
let overlayStack = [];
let selectedCheckinPet = null;
let uploadedPhotoFile = null;
let hallCache = [];          // 领养大厅列表缓存
let myAdoptionCache = [];    // 我的领养缓存
let checkinHistoryCache = [];// 打卡历史缓存（全量，供时间筛选重渲染）
let activeApplyPet = null;   // 当前申请领养的宠物

// === 工具函数 ===

function getCurrentUser() {
  return TNR_API.getCurrentUser();
}

// === 起止时间筛选（移动端） ===

/* 各列表的筛选状态。key 与页面一一对应。
 *
 * **值存在这里、不存在 DOM 里** —— 每个 `renderXxx()` 都会整体重建列表容器的
 * innerHTML，如果从 DOM 读当前区间，重建后输入框是空的、读回来的就是空值，
 * 用户看到的是「填了日期，一操作就没了」。 */
const dateFilters = {
  hall: { start: '', end: '' },
  adoption: { start: '', end: '' },
  checkin: { start: '', end: '' },
  messages: { start: '', end: '' },
};

function dateFilterOf(key) {
  return dateFilters[key] || (dateFilters[key] = { start: '', end: '' });
}

/* 一条起止时间筛选条的 HTML。 */
function dateBarHtml(key) {
  const f = dateFilterOf(key);
  return `
    <div class="mobile-date-bar" data-date-bar="${key}">
      <span class="date-bar-label">时间</span>
      <input type="date" class="form-input" data-date-start value="${f.start}">
      <span class="date-bar-sep">~</span>
      <input type="date" class="form-input" data-date-end value="${f.end}">
      <button type="button" class="date-bar-clear" data-date-clear>清空</button>
    </div>`;
}

/* 把筛选条挂到 `mountId` 并绑定事件（**幂等**：已挂过就只把状态回写进控件）。
 *
 * ⚠ `rerender` 只允许重建**列表容器**，绝不能重建包含筛选条的容器 ——
 * 那样输入框会被连同节点一起丢掉，表现为「选完日期焦点和值一起没了」。
 *
 * 时间判据统一走 `TNR_UI.inDateRange`（全站唯一）：与后台各端的列表、
 * 以及后端 `__date` 查询同口径。领养人端只是外观更紧凑，**口径不许另起一套**。 */
function ensureDateBar(key, mountId, rerender) {
  const mount = document.getElementById(mountId);
  if (!mount) return;
  const f = dateFilterOf(key);
  if (mount.querySelector(`[data-date-bar="${key}"]`)) {
    mount.querySelector('[data-date-start]').value = f.start;
    mount.querySelector('[data-date-end]').value = f.end;
    return;
  }
  mount.innerHTML = dateBarHtml(key);
  const bar = mount.querySelector(`[data-date-bar="${key}"]`);
  const sEl = bar.querySelector('[data-date-start]');
  const eEl = bar.querySelector('[data-date-end]');
  const sync = () => {
    f.start = (sEl.value || '').trim();
    f.end = (eEl.value || '').trim();
    // 起止填反了必须提示：否则用户看到空列表，会以为「这个区间真的没数据」，
    // 而不是「我把日期填反了」。
    if (f.start && f.end && f.start > f.end) {
      TNR_UI.toast('开始日期晚于结束日期，该区间内不会有记录', 'warning');
    }
    rerender();
  };
  sEl.addEventListener('change', sync);
  eEl.addEventListener('change', sync);
  bar.querySelector('[data-date-clear]').addEventListener('click', () => {
    sEl.value = ''; eEl.value = '';
    sync();
  });
}

/* 该列表是否设了时间区间。未设时列表行为与改动前**完全一致**。 */
function hasDateFilter(key) {
  const f = dateFilterOf(key);
  return !!(f.start || f.end);
}

/* 「（2026-09-01 ~ 2026-09-30）」这样的后缀，未设区间时返回空串。
 * 抽出来是为了让「列表上方的条数说明」与「实际生效的过滤条件」**同源** ——
 * 两处各拼一遍，迟早出现「文字写着全部、实际只筛了一个月」这种看不出来的错。 */
function dateRangeText(key) {
  const f = dateFilterOf(key);
  if (!f.start && !f.end) return '';
  return '（' + (f.start || '最早') + ' ~ ' + (f.end || '至今') + '）';
}

function getPetEmoji(species) {
  if (species === '猫') return '🐱';
  if (species === '狗') return '🐶';
  return '🐾';
}

function getPhotoClass(species) {
  if (species === '猫') return 'cat';
  if (species === '狗') return 'dog';
  return 'other';
}

function getMessageTypeIcon(type) {
  const map = { 'approval': '📋', 'checkin_reminder': '⏰', 'system': '📢', 'notice': '📜', 'policy': '📜' };
  return map[type] || '📨';
}

function getMessageTypeLabel(type) {
  const map = { 'approval': '审核通知', 'checkin_reminder': '打卡提醒', 'system': '系统公告', 'notice': '公告', 'policy': '政策通知' };
  return map[type] || '消息';
}

function getCheckinStatusBadge(status) {
  // 本文件以前自带一份映射，机构端同一状态叫「未通过」——
  // 领养人看到「已驳回」、机构台账写「未通过」，同一条款两种说法。
  // 文案/配色统一取 TNR_UI.CHECKIN_STATUS（后端 CheckIn.STATUS_CHOICES 的 label）。
  return TNR_UI.checkinStatusBadge(status);
}

function getApplyStatusText(status) {
  // 本文件曾自带两张申请状态表（文本一份、徽章一份），现在只剩调用点，
  // 文案/配色由 `TNR_UI.APPLY_STATUS` 给出（对齐 `AdoptionApplication.STATUS_CHOICES`）。
  return TNR_UI.applyStatusText(status);
}

function getApplyStatusBadge(status) {
  return TNR_UI.applyStatusBadge(status);
}

// === 导航 ===

const tabTitles = {
  'hall': '领养大厅',
  'my-adoption': '我的领养',
  'checkin': '回访打卡',
  'messages': '消息中心',
  'profile': '个人中心'
};

function switchTab(tabId) {
  currentPage = tabId;
  document.querySelectorAll('.page-view').forEach(p => p.classList.remove('active'));
  const page = document.getElementById('page-' + tabId);
  if (page) page.classList.add('active');
  document.querySelectorAll('.mobile-nav-item').forEach(n => n.classList.remove('active'));
  const navItem = document.querySelector('.mobile-nav-item[data-tab="' + tabId + '"]');
  if (navItem) navItem.classList.add('active');
  const titleEl = document.getElementById('headerTitle');
  if (titleEl) titleEl.textContent = tabTitles[tabId] || '';
  const backEl = document.getElementById('headerBack');
  if (backEl) backEl.classList.add('hidden');

  if (tabId === 'hall') renderHall();
  else if (tabId === 'my-adoption') renderMyAdoption();
  else if (tabId === 'checkin') renderCheckin();
  else if (tabId === 'messages') renderMessages();
  else if (tabId === 'profile') renderProfile();

  updateMessageBadge();
}

async function updateMessageBadge() {
  if (PUBLIC_MODE || !getCurrentUser()) return;
  try {
    const messages = await TNR_API.getAllRaw('messages');
    const count = messages.filter(m => !m.is_read).length;
    const navItem = document.querySelector('.mobile-nav-item[data-tab="messages"]');
    if (!navItem) return;
    let badge = navItem.querySelector('.mobile-nav-badge');
    if (count > 0) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'mobile-nav-badge';
        navItem.appendChild(badge);
      }
      badge.textContent = count > 99 ? '99+' : count;
    } else {
      if (badge) badge.remove();
    }
  } catch (e) { /* 静默 */ }
}

function goBack() {
  if (overlayStack.length > 0) {
    const overlayId = overlayStack.pop();
    closeOverlay(overlayId);
  }
}

function showOverlay(overlayId) {
  const el = document.getElementById(overlayId);
  if (el) el.classList.add('show');
  overlayStack.push(overlayId);
}

function closeOverlay(overlayId) {
  const el = document.getElementById(overlayId);
  if (el) el.classList.remove('show');
  overlayStack = overlayStack.filter(id => id !== overlayId);
}

// === 1. 领养大厅 ===

async function renderHall() {
  const container = document.getElementById('hallList');
  if (!container) return;
  const keyword = (document.getElementById('hallSearch')?.value || '').trim().toLowerCase();

  // 挂载起止时间筛选条（幂等）。它挂在 #hallList 之外，重建列表不会影响它。
  ensureDateBar('hall', 'hallDateBar', () => renderHall());
  const df = dateFilterOf('hall');

  // 显示加载态
  container.innerHTML = '<div class="empty-state"><div class="empty-state-icon" style="animation:spin 1s linear infinite;">⏳</div><div class="empty-state-text">加载中...</div></div>';

  try {
    const listings = await TNR_API.getAllRaw('pets');
    hallCache = listings;

    let filtered = listings;
    if (keyword) {
      filtered = listings.filter(item => {
        const p = item.pet || item;
        return (p.name && p.name.toLowerCase().includes(keyword)) ||
               (p.code && p.code.toLowerCase().includes(keyword)) ||
               (p.breed && p.breed.toLowerCase().includes(keyword)) ||
               (p.species && p.species.toLowerCase().includes(keyword));
      });
    }

    /* 起止时间：大厅按**上架时间**筛。
     *
     * `published_at` 是「上架日期」（DateField，服务端在创建/编辑上架信息时
     * 自动刷成当天），比 `created_at` 更贴近用户想问的「这只什么时候挂出来的」；
     * 但它允许为空（历史数据），所以**必须兜底到 `created_at`** ——
     * 否则这类记录一旦设了区间就**无条件消失**，而且看起来像「大厅里本来就没有」。
     */
    if (hasDateFilter('hall')) {
      filtered = filtered.filter(item => TNR_UI.inDateRange(
        item.published_at || item.publishedAt || item.created_at || item.createdAt,
        df.start, df.end));
    }

    if (filtered.length === 0) {
      // 设了区间时换一套文案：否则用户会把「这个区间里没有」误读成
      // 「大厅里本来就没有可领养的动物」。
      container.innerHTML = hasDateFilter('hall')
        ? `<div class="empty-state">
             <div class="empty-state-icon">🐾</div>
             <div class="empty-state-text">该时间区间内暂无可领养宠物</div>
             <div class="empty-state-hint">可清空时间条件后重试</div>
           </div>`
        : `<div class="empty-state">
             <div class="empty-state-icon">🐾</div>
             <div class="empty-state-text">暂无可领养的宠物</div>
             <div class="empty-state-hint">请稍后再来查看</div>
           </div>`;
      return;
    }

    let html = '<div style="font-size:12px;color:var(--ink-light);margin-bottom:8px;">共 ' + filtered.length + ' 只待领养宠物' + dateRangeText('hall') + '</div>';
    filtered.forEach(item => {
      const pet = item.pet || item;
      const hospitalName = item.hospital_name || '未知医院';
      const photoClass = getPhotoClass(pet.species);
      const emoji = getPetEmoji(pet.species);
      const genderIcon = pet.gender === '公' ? '♂' : '♀';
      const genderColor = pet.gender === '公' ? 'var(--azurite)' : 'var(--cinnabar)';
      const desc = item.intro || pet.description || '暂无描述';

      html += `
        <div class="pet-card" onclick="showPetDetail('${item.id}')">
          <div class="pet-card-body">
            <div class="pet-card-photo photo-placeholder ${photoClass}">${emoji}</div>
            <div class="pet-card-info">
              <div class="pet-card-name">
                ${TNR_UI.escape(pet.name || pet.code || '')}
                <span class="badge badge-cinnabar">待领养</span>
              </div>
              ${pet.name ? `<div class="pet-card-code">${TNR_UI.escape(pet.code || '')}</div>` : ''}
              <div class="pet-card-desc">${TNR_UI.escape(desc)}</div>
              <div class="pet-card-meta">
                <span class="pet-card-meta-item">${TNR_UI.joinInfo([pet.species, pet.breed])}</span>
                <span class="pet-card-meta-item" style="color:${genderColor};font-weight:600;">${genderIcon} ${TNR_UI.escape(pet.gender || '')}</span>
                <span class="pet-card-meta-item">${TNR_UI.escape(pet.age || '')}</span>
              </div>
            </div>
          </div>
          <div class="pet-card-footer">
            <span>🏥 ${TNR_UI.escape(hospitalName)}</span>
            <span style="margin-left:auto;color:var(--cinnabar);">查看详情 ›</span>
          </div>
        </div>`;
    });

    html += `
      <div class="contact-guide" style="margin-top:12px;">
        <div class="contact-guide-title">📌 线下联系指引</div>
        <div class="contact-guide-text">
          领养流程为线下办理，请自主联系对应医院/捕捉点咨询。<br>
          领养资格审核、承诺书签署及领养协议均在线下完成。
        </div>
      </div>`;

    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">加载失败</div><div class="empty-state-hint">${TNR_UI.escape(e.message)}</div></div>`;
  }
}

// === 2. 宠物详情页 ===

async function showPetDetail(listingId) {
  const content = document.getElementById('petDetailContent');
  content.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⏳</div><div class="empty-state-text">加载中...</div></div>';
  showOverlay('petDetailOverlay');

  // 优先从缓存取，否则请求详情
  let item = hallCache.find(l => String(l.id) === String(listingId));
  if (!item) {
    try {
      const json = await TNR_API.get(TNR_API.BASE + 'adoptions/hall/' + listingId + '/');
      item = json.data;
    } catch (e) {
      content.innerHTML = `<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">${TNR_UI.escape(e.message)}</div></div>`;
      return;
    }
  }
  if (!item) {
    TNR_UI.toast('宠物信息不存在', 'danger');
    return;
  }

  const pet = item.pet || item;
  const hospitalName = item.hospital_name || '';
  const photoClass = getPhotoClass(pet.species);
  const emoji = getPetEmoji(pet.species);
  const genderIcon = pet.gender === '公' ? '♂' : '♀';

  let html = '';

  // 照片区
  html += '<div class="detail-section">';
  html += '<div class="detail-section-title">宠物照片</div>';
  html += '<div class="detail-photo-grid">';
  if (pet.photo_before) {
    html += `<div class="detail-photo-item"><img src="${pet.photo_before}" style="width:100%;height:100%;object-fit:cover;" alt="捕捉前"><div class="detail-photo-label">捕捉前</div></div>`;
  } else {
    html += `<div class="detail-photo-item"><div class="photo-placeholder ${photoClass}">${emoji}</div><div class="detail-photo-label">捕捉前</div></div>`;
  }
  if (pet.photo_after) {
    html += `<div class="detail-photo-item"><img src="${pet.photo_after}" style="width:100%;height:100%;object-fit:cover;" alt="术后"><div class="detail-photo-label">术后</div></div>`;
  } else {
    html += `<div class="detail-photo-item"><div class="photo-placeholder ${photoClass}">${emoji}</div><div class="detail-photo-label">术后</div></div>`;
  }
  if (pet.photo_treatment) {
    html += `<div class="detail-photo-item"><img src="${pet.photo_treatment}" style="width:100%;height:100%;object-fit:cover;" alt="诊疗后"><div class="detail-photo-label">诊疗后</div></div>`;
  } else {
    html += `<div class="detail-photo-item"><div class="photo-placeholder ${photoClass}">${emoji}</div><div class="detail-photo-label">诊疗后</div></div>`;
  }
  html += '</div>';
  html += '</div>';

  // 基本信息
  html += '<div class="detail-section">';
  html += '<div class="detail-section-title">基本信息</div>';
  html += `<div class="info-row"><div class="info-label">编号</div><div class="info-value" style="font-family:var(--font-mono);">${TNR_UI.escape(pet.code || '')}</div></div>`;
  html += `<div class="info-row"><div class="info-label">名称</div><div class="info-value">${TNR_UI.escape(pet.name || '—')}</div></div>`;
  html += `<div class="info-row"><div class="info-label">物种</div><div class="info-value">${TNR_UI.escape(pet.species || '—')}</div></div>`;
  html += `<div class="info-row"><div class="info-label">品种</div><div class="info-value">${TNR_UI.escape(pet.breed || '—')}</div></div>`;
  html += `<div class="info-row"><div class="info-label">性别</div><div class="info-value">${genderIcon} ${TNR_UI.escape(pet.gender || '—')}</div></div>`;
  html += `<div class="info-row"><div class="info-label">年龄</div><div class="info-value">${TNR_UI.escape(pet.age || '—')}</div></div>`;
  html += `<div class="info-row"><div class="info-label">毛色</div><div class="info-value">${TNR_UI.escape(pet.color || '—')}</div></div>`;
  html += `<div class="info-row"><div class="info-label">体重</div><div class="info-value">${TNR_UI.escape(pet.weight || '—')}</div></div>`;
  html += `<div class="info-row"><div class="info-label">芯片号</div><div class="info-value" style="font-family:var(--font-mono);">${TNR_UI.escape(pet.chip_no || '未植入')}</div></div>`;
  html += '</div>';

  // 上架信息
  if (item.intro || item.personality || item.body_condition) {
    html += '<div class="detail-section">';
    html += '<div class="detail-section-title">领养信息</div>';
    if (item.intro) html += `<div class="info-row"><div class="info-label">简介</div><div class="info-value">${TNR_UI.escape(item.intro)}</div></div>`;
    if (item.personality) html += `<div class="info-row"><div class="info-label">性格</div><div class="info-value">${TNR_UI.escape(item.personality)}</div></div>`;
    if (item.body_condition) html += `<div class="info-row"><div class="info-label">身体状况</div><div class="info-value">${TNR_UI.escape(item.body_condition)}</div></div>`;
    html += '</div>';
  }

  // 所属医院
  html += '<div class="detail-section">';
  html += '<div class="detail-section-title">所属医院</div>';
  if (hospitalName) {
    html += `<div class="info-row"><div class="info-label">医院名称</div><div class="info-value">${TNR_UI.escape(hospitalName)}</div></div>`;
  } else {
    html += '<div class="info-row"><div class="info-label">所属医院</div><div class="info-value text-muted">暂无</div></div>';
  }
  html += '</div>';

  // 领养要求
  html += '<div class="detail-section">';
  html += '<div class="detail-section-title">领养要求</div>';
  html += '<div style="font-size:13px;color:var(--ink-text);line-height:1.8;">';
  html += '<div>· 有固定住所，居住环境适合养宠</div>';
  html += '<div>· 有稳定收入，能承担宠物日常开支</div>';
  html += '<div>· 有养宠经验或愿意学习养宠知识</div>';
  html += '<div>· 承诺定期疫苗、驱虫，不弃养、不虐待</div>';
  html += '<div>· 配合每月回访打卡，接受定期回访</div>';
  html += '</div>';
  html += '</div>';

  // 线下联系指引
  html += '<div class="detail-section">';
  html += '<div class="contact-guide">';
  html += '<div class="contact-guide-title">📌 线下联系指引</div>';
  html += '<div class="contact-guide-text">';
  html += '1. 请自行联系上方所属医院咨询领养事宜<br>';
  html += '2. 前往捕捉点进行线下资质审核<br>';
  html += '3. 签署纸质承诺书及领养协议<br>';
  html += '4. 审核通过后开通领养人专属端口<br>';
  html += '5. 到指定医院领取宠物';
  html += '</div>';
  html += '</div>';
  html += '</div>';

  // 领养流程须知
  html += '<div class="detail-section">';
  html += '<div class="detail-section-title">领养流程须知</div>';
  const flowDoc = item.flow_doc || '';
  if (flowDoc) {
    html += `<div style="font-size:13px;color:var(--ink-text);line-height:1.8;white-space:pre-line;">${TNR_UI.escape(flowDoc)}</div>`;
  } else {
    html += '<div class="adoption-process">';
    html += '<div class="adoption-process-title">📋 领养流程</div>';
    html += '<div class="adoption-process-list">';
    html += '<div>领养人线下自主联系对应医院/捕捉点</div>';
    html += '<div>捕捉点线下审核领养人资质（房产、居住、个人情况）</div>';
    html += '<div>签署纸质承诺书</div>';
    html += '<div>领养协议全部线下签署、纸质存档</div>';
    html += '<div>审核通过后，系统开通领养人专属端口</div>';
    html += '<div>到指定医院领取宠物，完成领养</div>';
    html += '<div>每月登录小程序完成回访打卡</div>';
    html += '</div>';
    html += '</div>';
  }
  html += '</div>';

  content.innerHTML = html;

  // 在线申请领养按钮（登录后可申请）
  if (!PUBLIC_MODE && getCurrentUser()) {
    const applyBtn = document.createElement('div');
    applyBtn.style.cssText = 'position:sticky;bottom:0;padding:12px;background:var(--bg, #fff);border-top:1px solid var(--line, rgba(0,0,0,0.06));';
    applyBtn.innerHTML = '<button class="btn btn-primary w-full" onclick="openApplyForm(\'' + listingId + '\')">🐾 申请领养</button>';
    content.appendChild(applyBtn);
  }
}

// === 2.1 在线领养申请 ===

function openApplyForm(listingId) {
  const item = hallCache.find(l => String(l.id) === String(listingId));
  const pet = (item && item.pet) ? item.pet : null;
  /* `adoption_hall_list` 返回的 `pet` 是**嵌套的宠物对象**，列表项里根本没有 `pet_id`
     字段。原实现写的是 `item.pet_id || item.pet || listingId`：`pet_id` 缺失时
     直接退到**整个对象**，于是请求体里的 `pet_id` 是个 JSON 对象 ——
     后端 `Pet.objects.filter(id={...})` 抛 TypeError 500，前端只拿到 HTML 错误页，
     `TNR_API._handle` 解析 JSON 失败 → 界面提示「服务器返回格式错误」。
     也就是说**在线领养申请从来就没成功过**，而且报错信息完全指不到原因。 */
  const petId = (pet && pet.id) || (item && (item.pet_id || item.petId)) || null;
  activeApplyPet = { listingId, petId, petName: pet ? pet.name : '', petCode: pet ? pet.code : '' };

  const user = getCurrentUser();
  const content = document.getElementById('applyFormContent');
  content.innerHTML = `
    <div class="detail-section">
      <div class="detail-section-title">申请领养</div>
      <div style="font-size:13px;color:var(--ink-muted);margin-bottom:12px;">
        宠物：${TNR_UI.escape(activeApplyPet.petCode || '')}
      </div>
      <div class="form-group">
        <label class="form-label">姓名 *</label>
        <input type="text" class="form-input" id="applyName" value="${TNR_UI.escape(user.name || '')}" placeholder="请输入真实姓名">
      </div>
      <div class="form-group">
        <label class="form-label">联系电话 *</label>
        <input type="tel" class="form-input" id="applyPhone" value="${TNR_UI.escape(user.phone || '')}" placeholder="请输入联系电话">
      </div>
      <div class="form-group">
        <label class="form-label">身份证号</label>
        <input type="text" class="form-input" id="applyIdCard" placeholder="请输入身份证号">
      </div>
      <div class="form-group">
        <label class="form-label">居住地址</label>
        <input type="text" class="form-input" id="applyAddress" placeholder="请输入居住地址">
      </div>
      <div class="form-group">
        <label class="form-label">领养资质说明</label>
        <textarea class="form-textarea" id="applyQualification" rows="2" placeholder="如：有稳定住所、稳定收入、有养宠经验等"></textarea>
      </div>
      <div class="form-group">
        <label class="form-label">领养理由</label>
        <textarea class="form-textarea" id="applyReason" rows="3" placeholder="请简要说明领养理由（必填）"></textarea>
      </div>
      <button class="btn btn-primary w-full" onclick="submitApply()">提交申请</button>
    </div>`;
  showOverlay('applyOverlay');
}

async function submitApply() {
  const name = (document.getElementById('applyName')?.value || '').trim();
  const phone = (document.getElementById('applyPhone')?.value || '').trim();
  const reason = (document.getElementById('applyReason')?.value || '').trim();
  if (!name) { TNR_UI.toast('请填写姓名', 'warning'); return; }
  if (!phone) { TNR_UI.toast('请填写联系电话', 'warning'); return; }
  if (!reason) { TNR_UI.toast('请填写领养理由', 'warning'); return; }

  try {
    const json = await TNR_API.post('adoptions/apply/', {
      pet_id: activeApplyPet.petId,
      applicant_name: name,
      applicant_phone: phone,
      applicant_id_card: (document.getElementById('applyIdCard')?.value || '').trim(),
      applicant_address: (document.getElementById('applyAddress')?.value || '').trim(),
      qualification: (document.getElementById('applyQualification')?.value || '').trim(),
      reason: reason,
    });
    TNR_UI.toast(json.message || '领养申请提交成功', 'success');
    closeOverlay('applyOverlay');
    closeOverlay('petDetailOverlay');
  } catch (e) {
    TNR_UI.toast(e.message || '提交失败', 'danger');
  }
}

/* 撤回自己的领养申请（`pending` → `cancelled`）。
 *
 * 走 `TNR_API.cancelAdoptionApplication()`（`_post` 系：**返回完整信封、
 * 非 2xx 也不抛错**），所以必须用 `TNR_UI.assertOk()` 把 `success:false`
 * 转成异常，catch 里的失败提示才可能出现。**不能**用 `_get` 系的静默读 ——
 * 那连 HTTP 状态都忽略，撤回失败会被渲染成「没反应」，用户以为撤回了、
 * 实际还在等审核（「没报错」的地方最危险）。
 *
 * 撤回成功后必须 `renderMyAdoption()` 重渲染：不重渲染的话卡片上还是
 * 「待审核」+ 撤回按钮，用户会反复点，第二次开始 400（已处理）。
 */
async function cancelApply(id) {
  const ok = await TNR_UI.confirm(
    '确定撤回这条领养申请吗？<br>撤回后可以重新提交申请。', '撤回申请');
  if (!ok) return;
  try {
    // ⚠ `cancelAdoptionApplication` 走 `_post`：返回的是**完整信封**，非 2xx
    // 也**不抛错**。必须用 `assertOk` 把 `success:false` 变成异常，否则
    // 会把「撤回失败」当成成功弹绿条（这正是死 catch 判据报出来的问题）。
    const json = TNR_UI.assertOk(
      await TNR_API.cancelAdoptionApplication(id), '撤回失败');
    TNR_UI.toast(json.message || '申请已撤回', 'success');
    renderMyAdoption();
  } catch (e) {
    TNR_UI.toast(e.message || '撤回失败', 'danger');
  }
}

async function renderMyAdoption() {
  if (PUBLIC_MODE || !getCurrentUser()) {
    document.getElementById('myAdoptionList').innerHTML = loginRequiredHtml('前往登录后查看您的领养记录');
    // 未登录时不挂筛选条：一个筛不出任何东西的控件比没有更糟。
    const mount = document.getElementById('myAdoptionDateBar');
    if (mount) mount.innerHTML = '';
    return;
  }
  const container = document.getElementById('myAdoptionList');
  ensureDateBar('adoption', 'myAdoptionDateBar', () => renderMyAdoption());
  const df = dateFilterOf('adoption');
  container.innerHTML = loadingHtml();

  try {
    const [adoptions, appJson] = await Promise.all([
      TNR_API.getAllRaw('adoptions'),
      TNR_API.get(TNR_API.BASE + 'adoptions/my-applications/'),
    ]);
    // 缓存始终存**全量** —— 详情页/打卡页都从它取数据，
    // 存过滤后的会让「按 id 找记录」找不到（表现为「记录不存在」）。
    myAdoptionCache = adoptions;

    /* 本页两段列表的时间字段**不同**：申请看 `applied_at`（提交申请时间），
       记录看 `adopted_at`（领养日期）。同一个区间分别作用于各自的字段，
       与政府端台账中心「按标签页选时间字段」是同一套做法。 */
    const applications = (appJson.data || []).filter(a =>
      TNR_UI.inDateRange(a.applied_at || a.appliedAt, df.start, df.end));
    const adoptionRows = adoptions.filter(a =>
      TNR_UI.inDateRange(TNR_UI.firstDate(
        a.adopted_at, a.adoptedAt, a.created_at, a.createdAt),
        df.start, df.end));
    const ranged = hasDateFilter('adoption');

    let html = '';

    // 我的领养申请（在线申请状态）
    html += '<div class="section-header-mobile"><div class="section-header-title">我的领养申请</div></div>';
    if (applications.length === 0) {
      html += ranged
        ? `<div class="empty-state" style="padding:16px;">
             <div class="empty-state-icon" style="font-size:28px;">📝</div>
             <div class="empty-state-text" style="font-size:13px;">该时间区间内暂无领养申请</div>
           </div>`
        : `<div class="empty-state" style="padding:16px;">
             <div class="empty-state-icon" style="font-size:28px;">📝</div>
             <div class="empty-state-text" style="font-size:13px;">暂无领养申请</div>
             <div class="empty-state-hint">前往领养大厅选择宠物，在线提交领养申请</div>
           </div>`;
    } else {
      applications.forEach(app => {
        const pet = app.pet;
        // 标题位以前填的是 `pet.code`，与后面的编号 chip 同一个值 ——
        // 一张卡片把编号打印两遍，而宠物的名字压根没出现过。
        const petName = (pet && (pet.name || pet.code)) || '—';
        const petCode = (pet && pet.code) || app.pet_code || '';
        const statusBadge = getApplyStatusBadge(app.status);
        // `applied_at` 是 DateTimeField（UTC 串），裸截断会让日期**差一天**。
        const appliedAt = app.applied_at ? TNR_UI.localDateStr(app.applied_at) : '—';
        html += `
          <div class="adoption-card" style="margin-bottom:10px;">
            <div class="adoption-card-header">
              <div class="pet-card-info" style="padding-left:0;">
                <div class="pet-card-name">${TNR_UI.escape(petName)} ${pet.name ? `<span class="pet-card-code" style="font-family:var(--font-mono);">${TNR_UI.escape(petCode)}</span>` : ''}</div>
                <div class="pet-card-meta">
                  <span class="pet-card-meta-item">申请时间：${TNR_UI.escape(appliedAt)}</span>
                  <span class="pet-card-meta-item">受理：${TNR_UI.escape(app.hospital_name || '—')}</span>
                </div>
                ${app.review_note ? `<div class="pet-card-desc" style="margin-top:6px;color:var(--ink-muted);">审核意见：${TNR_UI.escape(app.review_note)}</div>` : ''}
              </div>
            </div>
            <div class="adoption-card-footer">
              <span>状态：${statusBadge}</span>
              ${app.status === 'pending'
                ? `<button class="btn btn-secondary btn-sm" style="margin-left:auto;" onclick="event.stopPropagation();cancelApply('${app.id}')">撤回申请</button>`
                : `<span style="margin-left:auto;color:var(--ink-light);font-size:12px;">${TNR_UI.escape(getApplyStatusText(app.status))}</span>`}
            </div>
          </div>`;
      });
    }

    // 领养记录
    html += '<div class="section-header-mobile" style="margin-top:8px;"><div class="section-header-title">领养记录</div></div>';
    if (adoptionRows.length === 0) {
      html += ranged
        ? `<div class="empty-state">
             <div class="empty-state-icon">🐾</div>
             <div class="empty-state-text">该时间区间内暂无领养记录</div>
           </div>`
        : `<div class="empty-state">
             <div class="empty-state-icon">🐾</div>
             <div class="empty-state-text">暂无领养记录</div>
             <div class="empty-state-hint">申请审核通过后，可前往医院办理领养</div>
           </div>`;
      container.innerHTML = html;
      return;
    }

    html += '<div style="font-size:12px;color:var(--ink-light);margin-bottom:8px;">共 ' + adoptionRows.length + ' 条领养记录' + dateRangeText('adoption') + '</div>';
    adoptionRows.forEach(adoption => {
      const pet = adoption.pet;
      if (!pet) return;
      const photoClass = getPhotoClass(pet.species);
      const emoji = getPetEmoji(pet.species);
      // `adopted_at` 是 DateField（纯日期），localDateStr 原样返回 —— 统一口径。
      const adoptedAt = adoption.adopted_at ? TNR_UI.localDateStr(adoption.adopted_at) : '—';
      // 卡片标题以前写死 `badge-success 已领养`，**根本不看 adoption.status**：
      // `pending_claim`（医院还没确认领出）与 `cancelled`（已取消）的单子
      // 也显示成绿色「已领养」，领养人据此以为手续办完、白跑一趟医院。
      const petTitle = pet.name || pet.code || '—';

      html += `
        <div class="adoption-card" onclick="showLifecycle('${adoption.id}')">
          <div class="adoption-card-header">
            <div class="pet-card-photo photo-placeholder ${photoClass}">${emoji}</div>
            <div class="pet-card-info">
              <div class="pet-card-name">${TNR_UI.escape(petTitle)} ${TNR_UI.adoptionStatusBadge(adoption.status)}</div>
              ${pet.name ? `<div class="pet-card-code">${TNR_UI.escape(pet.code || '')}</div>` : ''}
              <div class="pet-card-meta">
                <span class="pet-card-meta-item">${TNR_UI.joinInfo([pet.species, pet.breed])}</span>
                <span class="pet-card-meta-item">领养日期：${TNR_UI.escape(adoptedAt)}</span>
              </div>
            </div>
          </div>
          <div class="adoption-card-footer">
            <span>📋 ${TNR_UI.escape(adoption.hospital_name || '—')}</span>
            <span style="margin-left:auto;color:var(--cinnabar);">查看生命周期 ›</span>
          </div>
        </div>`;
    });

    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = errorHtml(e.message);
  }
}

// === 生命周期溯源 ===

async function showLifecycle(adoptionId) {
  const content = document.getElementById('lifecycleContent');
  content.innerHTML = loadingHtml();
  showOverlay('lifecycleOverlay');

  const adoption = myAdoptionCache.find(a => String(a.id) === String(adoptionId));
  if (!adoption || !adoption.pet) {
    content.innerHTML = errorHtml('领养记录不存在');
    return;
  }

  const pet = adoption.pet;
  const photoClass = getPhotoClass(pet.species);
  const emoji = getPetEmoji(pet.species);
  const petId = pet.id || adoption.pet;

  // 拉取全生命周期数据
  let lifecycleData = null;
  try {
    lifecycleData = await TNR_API.getPetLifecycle(petId);
  } catch (e) {
    // 接口失败时降级为仅展示领养记录
  }

  let html = '';

  // 宠物概要
  html += '<div class="detail-section" style="text-align:center;">';
  html += `<div class="pet-card-photo photo-placeholder ${photoClass}" style="width:80px;height:80px;margin:0 auto 12px;font-size:40px;">${emoji}</div>`;
  html += `<div style="font-family:var(--font-display);font-size:18px;font-weight:600;color:var(--ink);">${TNR_UI.escape(pet.code || '')}</div>`;
  html += `<div style="font-size:12px;color:var(--ink-light);font-family:var(--font-mono);margin-top:4px;">${TNR_UI.escape(pet.code || '')}</div>`;
  html += `<div style="margin-top:8px;">${TNR_UI.petStatusBadge(pet.status)}</div>`;
  html += '</div>';

  // 时间线
  html += '<div class="detail-section">';
  html += '<div class="detail-section-title">全生命周期溯源</div>';
  html += '<div class="timeline">';

  const events = (lifecycleData && lifecycleData.events) || [];
  if (events.length === 0) {
    // 降级：至少展示领养记录
    const adoptedAt = adoption.adopted_at ? TNR_UI.localDateStr(adoption.adopted_at) : '—';
    html += `<div class="timeline-item done">
      <div class="timeline-time">${TNR_UI.escape(adoptedAt)}</div>
      <div class="timeline-title">领养完成</div>
      <div class="timeline-desc">领养人：${TNR_UI.escape(adoption.adopter_name || '—')}<br>医院：${TNR_UI.escape(adoption.hospital_name || '—')}<br>协议：${TNR_UI.escape(adoption.adoption_agreement || '线下已签')}</div>
    </div>`;
  } else {
    events.forEach(ev => {
      // 后端 `ev.date` 统一给**本地日期**串；这里仍走 localDateStr 兜底 ——
      // 历史上 `owner_return` 事件返回的是 UTC 时间串（`return_time.isoformat()`），
      // 裸 `substring(0, 10)` 会让「回收登记」的日期**差一天**。
      // localDateStr 对纯日期串原样返回，对 UTC 串转本地。
      const dateStr = ev.date ? TNR_UI.localDateStr(ev.date) : '—';
      let desc = '';
      if (ev.type === 'capture') {
        desc = `捕捉点：${TNR_UI.escape(ev.shelter_name || '—')}<br>小区：${TNR_UI.escape(ev.community_name || '—')}<br>地址：${TNR_UI.escape(ev.address || '—')}`;
      } else if (ev.type === 'transfer') {
        // 这里以前自带一张转运状态表且**漏了 `partial`**，兜底还是 `|| '待签收'` ——
        // 医院已经签收一部分的单子，在领养人的生命周期里显示成「待签收」，
        // 看不出「一部分动物已经签收了」。取值走 `TNR_UI.TRANSFER_STATUS`。
        desc = `发出：${TNR_UI.escape(ev.from_shelter_name || '—')}<br>接收：${TNR_UI.escape(ev.to_hospital_name || '—')}<br>状态：${TNR_UI.escape(TNR_UI.transferStatusText(ev.status))}`;
      } else if (ev.type === 'treatment') {
        desc = `医院：${TNR_UI.escape(ev.hospital_name || '—')}<br>项目：${TNR_UI.escape((ev.items || []).join('、') || '—')}<br>状态：${TNR_UI.escape(TNR_UI.treatmentStatusText(ev.status))}`;
        if (ev.chip_no) desc += `<br>芯片号：${TNR_UI.escape(ev.chip_no)}`;
      } else if (ev.type === 'release') {
        desc = `小区：${TNR_UI.escape(ev.community_name || '—')}<br>接收人：${TNR_UI.escape(ev.receiver_name || '—')}<br>状态：${TNR_UI.escape(TNR_UI.releaseStatusText(ev.status))}`;
      } else if (ev.type === 'adoption') {
        desc = `领养人：${TNR_UI.escape(ev.adopter_name || '—')}<br>医院：${TNR_UI.escape(ev.hospital_name || '—')}`;
      } else if (ev.type === 'euthanasia') {
        desc = `医院：${TNR_UI.escape(ev.hospital_name || '—')}<br>原因：${TNR_UI.escape(ev.reason || '—')}`;
      } else if (ev.type === 'owner_return') {
        desc = `领回单号：${TNR_UI.escape(ev.ledger_no || '—')}<br>领回人：${TNR_UI.escape(ev.owner_name || '—')}`
          + (ev.owner_phone ? `<br>联系电话：${TNR_UI.escape(ev.owner_phone)}` : '')
          + (ev.owner_address ? `<br>住址：${TNR_UI.escape(ev.owner_address)}` : '')
          + `<br>原因：${TNR_UI.escape(ev.reason || '—')}<br>操作人：${TNR_UI.escape(ev.operator_name || '—')}`;
        if (ev.signature && ev.signature.startsWith('data:image')) {
          desc += `<div style="margin-top:8px;"><div style="font-size:12px;color:var(--ink-muted);margin-bottom:4px;">领回人电子签名</div>`
            + `<img src="${ev.signature}" style="max-width:240px;max-height:100px;background:#fff;border:1px solid var(--border-light);border-radius:6px;padding:4px;"></div>`;
        }
      }
      html += `<div class="timeline-item done">
        <div class="timeline-time">${TNR_UI.escape(dateStr)}</div>
        <div class="timeline-title">${TNR_UI.escape(ev.type_display || ev.type)}</div>
        <div class="timeline-desc">${desc}</div>
      </div>`;
    });
  }

  html += '</div>';
  html += '</div>';

  content.innerHTML = html;
}

// === 4. 回访打卡 ===

async function renderCheckin() {
  const historySection = document.getElementById('checkinHistorySection');
  if (PUBLIC_MODE || !getCurrentUser()) {
    document.getElementById('checkinContent').innerHTML = loginRequiredHtml('前往登录后进行回访打卡');
    if (historySection) historySection.style.display = 'none';
    return;
  }
  const container = document.getElementById('checkinContent');

  try {
    const [adoptions, checkIns] = await Promise.all([
      TNR_API.getAllRaw('adoptions'),
      TNR_API.getAllRaw('checkIns'),
    ]);
    myAdoptionCache = adoptions;

    if (adoptions.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">📷</div>
          <div class="empty-state-text">暂无领养宠物</div>
          <div class="empty-state-hint">领养宠物后可进行月度回访打卡</div>
        </div>`;
      if (historySection) historySection.style.display = 'none';
      return;
    }

    const now = new Date();
    const currentMonth = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0');
    const pendingPets = adoptions.filter(adoption => {
      const petCheckIns = checkIns.filter(c => String(c.pet) === String(adoption.pet) || String(c.pet_id) === String(adoption.pet));
      return !petCheckIns.some(c => c.month === currentMonth);
    });

    let html = '';

    // 待打卡提醒
    if (pendingPets.length > 0) {
      html += '<div class="section-header-mobile">';
      html += '<div class="section-header-title">待打卡月份</div>';
      html += '</div>';
      html += '<div style="margin-bottom:16px;">';
      pendingPets.forEach(adoption => {
        const pet = adoption.pet;
        if (!pet) return;
        html += `<span class="pending-month-tag">⏰ ${TNR_UI.escape(pet.code || '')} - ${currentMonth}</span>`;
      });
      html += '</div>';
    }

    // 打卡表单
    html += '<div class="section-header-mobile">';
    html += '<div class="section-header-title">提交月度打卡</div>';
    html += '</div>';

    html += '<div class="checkin-form">';
    html += '<div class="form-label">选择宠物</div>';
    html += '<div class="checkin-pet-select">';
    adoptions.forEach((adoption, idx) => {
      const pet = adoption.pet;
      if (!pet) return;
      const active = (idx === 0) ? ' active' : '';
      const petId = pet.id || adoption.pet;
      html += `<div class="checkin-pet-item${active}" data-pet-id="${petId}" onclick="selectCheckinPet('${petId}', this)">${TNR_UI.escape(pet.code || '')}</div>`;
    });
    html += '</div>';

    html += `<div class="form-label">打卡月份</div>`;
    html += `<div style="margin-bottom:12px;"><span class="badge badge-cinnabar" style="font-size:13px;">${currentMonth}</span></div>`;

    html += '<div class="form-label">宠物近期照片</div>';
    html += `<div class="photo-upload-area" id="photoUploadArea" onclick="document.getElementById('checkinPhotoInput').click()">
      <div class="photo-upload-placeholder">📷</div>
      <div style="font-size:13px;color:var(--ink-muted);">点击上传照片</div>
      <div style="font-size:11px;color:var(--ink-light);margin-top:4px;">支持 JPG/PNG 格式</div>
    </div>`;
    html += `<input type="file" id="checkinPhotoInput" accept="image/*" style="display:none;" onchange="onPhotoSelected(event)">`;

    html += '<div class="form-group" style="margin-top:16px;">';
    html += '<label class="form-label">近况说明</label>';
    html += '<textarea class="form-textarea" id="checkinNote" placeholder="请简要描述宠物近期状况（食欲、精神、健康等）" rows="3"></textarea>';
    html += '</div>';

    html += `<button class="btn btn-primary w-full" onclick="submitCheckin('${currentMonth}')" id="checkinSubmitBtn">提交打卡</button>`;
    html += '</div>';

    /* 打卡历史**不再拼进 `html`**，而是交给 `renderCheckinHistory()` 渲染到
       `#checkinHistory`（静态容器，在表单之外）。原因：设了时间区间后要重渲染
       历史列表，如果历史跟表单在同一个容器里，重渲染会把用户**已经填好的
       「近况说明」和选好的照片一起清空** —— 而且不报任何错。 */
    checkinHistoryCache = checkIns;
    if (historySection) historySection.style.display = '';
    ensureDateBar('checkin', 'checkinDateBar', () => renderCheckinHistory());
    renderCheckinHistory();

    container.innerHTML = html;

    // 设置默认选中的宠物
    if (adoptions.length > 0) {
      const firstPet = adoptions[0].pet;
      if (firstPet) selectedCheckinPet = firstPet.id || adoptions[0].pet;
    }
    uploadedPhotoFile = null;
  } catch (e) {
    container.innerHTML = errorHtml(e.message);
  }
}

function selectCheckinPet(petId, el) {
  selectedCheckinPet = petId;
  document.querySelectorAll('.checkin-pet-item').forEach(item => item.classList.remove('active'));
  el.classList.add('active');
}

/* 渲染「打卡历史」列表。独立于表单，可被时间筛选安全地反复重建。
 *
 * 时间口径取 `created_at`（提交打卡的时间），不是 `month`（打卡月份）——
 * 两者可能不一致（比如 9 月补交 8 月的打卡），而用户圈日期区间时问的是
 * 「我什么时候提交的」。 */
function renderCheckinHistory() {
  const box = document.getElementById('checkinHistory');
  if (!box) return;
  const df = dateFilterOf('checkin');
  const rows = (checkinHistoryCache || []).filter(c =>
    TNR_UI.inDateRange(c.created_at || c.createdAt, df.start, df.end));

  if (rows.length === 0) {
    box.innerHTML = hasDateFilter('checkin')
      ? `<div class="empty-state" style="padding:24px;">
           <div class="empty-state-icon" style="font-size:36px;">📋</div>
           <div class="empty-state-text" style="font-size:13px;">该时间区间内暂无打卡记录</div>
         </div>`
      : `<div class="empty-state" style="padding:24px;">
           <div class="empty-state-icon" style="font-size:36px;">📋</div>
           <div class="empty-state-text" style="font-size:13px;">暂无打卡记录</div>
         </div>`;
    return;
  }

  const sorted = [...rows].sort((a, b) => (b.month || '').localeCompare(a.month || ''));
  let html = '<div style="font-size:12px;color:var(--ink-light);margin-bottom:8px;">共 '
    + rows.length + ' 条打卡记录' + dateRangeText('checkin') + '</div>';
  sorted.forEach(checkin => {
    const petName = checkin.pet_name || checkin.adopter_name || '';
    html += `
      <div class="checkin-history-item">
        <div class="checkin-history-header">
          <span class="checkin-history-month">${TNR_UI.escape(checkin.month || '—')}</span>
          ${getCheckinStatusBadge(checkin.status)}
        </div>
        <div style="font-size:12px;color:var(--ink-light);">${TNR_UI.escape(petName)} · ${TNR_UI.escape(checkin.pet_code || '')}</div>
        ${checkin.note ? `<div class="checkin-history-note">${TNR_UI.escape(checkin.note)}</div>` : ''}
        <div class="checkin-history-date">提交时间：${TNR_UI.escape(TNR_UI.formatDateTime(checkin.created_at))}</div>
      </div>`;
  });
  box.innerHTML = html;
}

function onPhotoSelected(event) {
  const file = event.target.files[0];
  if (!file) return;
  uploadedPhotoFile = file;
  const reader = new FileReader();
  reader.onload = function(e) {
    const area = document.getElementById('photoUploadArea');
    area.classList.add('has-photo');
    area.innerHTML = `<img src="${e.target.result}" class="photo-upload-preview" alt="预览"><div style="position:absolute;top:8px;right:8px;background:rgba(157,41,51,0.9);color:#fff;border:none;padding:4px 8px;border-radius:4px;font-size:12px;cursor:pointer;" onclick="event.stopPropagation();resetPhotoUpload()">重新上传</div>`;
  };
  reader.readAsDataURL(file);
}

function resetPhotoUpload() {
  uploadedPhotoFile = null;
  const input = document.getElementById('checkinPhotoInput');
  if (input) input.value = '';
  const area = document.getElementById('photoUploadArea');
  area.classList.remove('has-photo');
  area.innerHTML = `
    <div class="photo-upload-placeholder">📷</div>
    <div style="font-size:13px;color:var(--ink-muted);">点击上传照片</div>
    <div style="font-size:11px;color:var(--ink-light);margin-top:4px;">支持 JPG/PNG 格式</div>`;
}

async function submitCheckin(month) {
  if (!selectedCheckinPet) {
    TNR_UI.toast('请选择打卡宠物', 'warning');
    return;
  }
  const noteEl = document.getElementById('checkinNote');
  const note = noteEl ? noteEl.value.trim() : '';
  if (!note) {
    TNR_UI.toast('请填写近况说明', 'warning');
    return;
  }

  const btn = document.getElementById('checkinSubmitBtn');
  if (btn) { btn.disabled = true; btn.textContent = '提交中...'; }

  // 使用 FormData 提交（含文件）
  const formData = new FormData();
  formData.append('pet_id', selectedCheckinPet);
  formData.append('month', month);
  formData.append('note', note);
  if (uploadedPhotoFile) formData.append('photo', uploadedPhotoFile);

  try {
    const json = await TNR_API.post(TNR_API.BASE + 'checkins/create/', formData);
    TNR_UI.toast(json.message || '打卡提交成功，等待审核', 'success');
    renderCheckin();
  } catch (e) {
    TNR_UI.toast(e.message || '提交失败', 'danger');
    if (btn) { btn.disabled = false; btn.textContent = '提交打卡'; }
  }
}

// === 5. 消息中心 ===

async function renderMessages() {
  const barMount = document.getElementById('messagesDateBar');
  if (PUBLIC_MODE || !getCurrentUser()) {
    document.getElementById('messagesList').innerHTML = loginRequiredHtml('前往登录后查看消息');
    if (barMount) barMount.innerHTML = '';
    return;
  }
  const container = document.getElementById('messagesList');
  ensureDateBar('messages', 'messagesDateBar', () => renderMessages());
  const df = dateFilterOf('messages');
  container.innerHTML = loadingHtml();

  try {
    const all = await TNR_API.getAllRaw('messages');
    // 时间口径 = `created_at`（消息产生时间），与「未读角标」用的全量口径分开：
    // 角标要显示的是**总共**多少条未读，不能被当前筛选区间影响。
    const messages = all.filter(m =>
      TNR_UI.inDateRange(m.created_at || m.createdAt, df.start, df.end));
    const ranged = hasDateFilter('messages');

    if (messages.length === 0) {
      container.innerHTML = ranged
        ? `<div class="empty-state">
             <div class="empty-state-icon">🔔</div>
             <div class="empty-state-text">该时间区间内暂无消息</div>
           </div>`
        : `<div class="empty-state">
             <div class="empty-state-icon">🔔</div>
             <div class="empty-state-text">暂无消息</div>
           </div>`;
      return;
    }

    const sorted = [...messages].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    const unreadCount = messages.filter(m => !m.is_read).length;

    let html = '<div style="font-size:12px;color:var(--ink-light);margin-bottom:8px;">共 ' + messages.length + ' 条消息，' + unreadCount + ' 条未读' + dateRangeText('messages') + '</div>';

    sorted.forEach(msg => {
      const icon = getMessageTypeIcon(msg.type);
      const typeLabel = getMessageTypeLabel(msg.type);
      const iconBg = msg.is_read ? 'var(--rice-warm)' : 'var(--cinnabar-light)';

      html += `
        <div class="message-item ${msg.is_read ? '' : 'unread'}" onclick="showMessageDetail('${msg.id}')">
          <div class="message-icon" style="background:${iconBg};">${icon}</div>
          <div class="message-content">
            <div class="message-title">
              ${TNR_UI.escape(msg.title)}
              ${!msg.is_read ? '<span class="message-unread-dot"></span>' : ''}
            </div>
            <div class="message-preview">${TNR_UI.escape(msg.content || '')}</div>
            <div class="message-time">${TNR_UI.escape(TNR_UI.formatDateTime(msg.created_at))} · ${typeLabel}</div>
          </div>
        </div>`;
    });

    container.innerHTML = html;
    updateMessageBadge();
  } catch (e) {
    container.innerHTML = errorHtml(e.message);
  }
}

async function showMessageDetail(messageId) {
  const content = document.getElementById('messageDetailContent');
  showOverlay('messageDetailOverlay');
  content.innerHTML = loadingHtml();

  // 从缓存查找（避免再请求）
  let msg = null;
  try {
    const messages = await TNR_API.getAllRaw('messages');
    msg = messages.find(m => String(m.id) === String(messageId));
  } catch (e) {
    content.innerHTML = errorHtml(e.message);
    return;
  }
  if (!msg) {
    content.innerHTML = errorHtml('消息不存在');
    return;
  }

  // 标记已读
  if (!msg.is_read) {
    try {
      await TNR_API.post(TNR_API.BASE + 'portal/messages/' + msg.id + '/read/', {});
    } catch (e) { /* 静默 */ }
  }

  const icon = getMessageTypeIcon(msg.type);
  const typeLabel = getMessageTypeLabel(msg.type);

  let html = '<div class="message-detail-content">';
  html += `<div class="message-detail-title">${TNR_UI.escape(msg.title)}</div>`;
  html += `<div class="message-detail-meta">`;
  html += `<span>${icon} ${typeLabel}</span>`;
  html += `<span>${TNR_UI.escape(TNR_UI.formatDateTime(msg.created_at))}</span>`;
  html += `</div>`;
  html += `<div class="message-detail-body">${TNR_UI.escape(msg.content || '暂无内容')}</div>`;
  html += `</div>`;

  content.innerHTML = html;

  setTimeout(() => {
    renderMessages();
    updateMessageBadge();
  }, 300);
}

// === 6. 个人中心 ===

function renderProfile() {
  const user = getCurrentUser();
  if (!user) {
    document.getElementById('profileContent').innerHTML = loginRequiredHtml('请登录后查看个人中心');
    return;
  }

  const avatarText = user.name ? user.name.charAt(0) : '👤';
  let html = '';

  // 头部
  html += '<div class="profile-header">';
  html += `<div class="profile-avatar">${TNR_UI.escape(avatarText)}</div>`;
  html += `<div class="profile-name">${TNR_UI.escape(user.name || '—')}</div>`;
  html += `<div class="profile-phone">${TNR_UI.escape(user.phone || '—')}</div>`;
  html += '</div>';

  // 身份绑定
  html += '<div class="profile-section">';
  html += '<div class="profile-section-header">🔐 账号信息</div>';
  html += `<div class="profile-row"><div class="profile-row-label">👤 账号</div><div class="profile-row-value">${TNR_UI.escape(user.username || '—')}</div></div>`;
  html += `<div class="profile-row"><div class="profile-row-label">🎭 角色</div><div class="profile-row-value">${TNR_UI.escape(user.role_display || '领养人')}</div></div>`;
  html += `<div class="profile-row"><div class="profile-row-label">📱 手机号码</div><div class="profile-row-value">${TNR_UI.escape(user.phone || '—')}</div></div>`;
  if (user.district_name) {
    html += `<div class="profile-row"><div class="profile-row-label">📍 所属区县</div><div class="profile-row-value">${TNR_UI.escape(user.district_name)}</div></div>`;
  }
  html += '</div>';

  // 其他
  html += '<div class="profile-section">';
  html += '<div class="profile-section-header">⚙️ 其他</div>';
  html += '<div class="profile-row" onclick="aboutApp()">';
  html += '<div class="profile-row-label">ℹ️ 关于系统</div>';
  html += '<div class="profile-row-value"><span class="profile-row-arrow">›</span></div>';
  html += '</div>';
  html += `<div class="profile-row" onclick="window.location.href='/accounts/logout/'">`;
  html += '<div class="profile-row-label" style="color:var(--cinnabar);">🚪 退出登录</div>';
  html += '<div class="profile-row-value"><span class="profile-row-arrow">›</span></div>';
  html += '</div>';
  html += '</div>';

  // 版本信息
  html += '<div style="text-align:center;padding:16px;color:var(--ink-light);font-size:11px;">';
  html += 'TNR流浪动物管理系统 · 领养人端<br>v1.0.0';
  html += '</div>';

  document.getElementById('profileContent').innerHTML = html;
}

function aboutApp() {
  TNR_UI.modal({
    title: '关于系统',
    body: `
      <div style="text-align:center;padding:16px 0;">
        <div style="font-size:48px;margin-bottom:12px;">🐾</div>
        <div style="font-family:var(--font-display);font-size:18px;font-weight:600;color:var(--ink);margin-bottom:4px;">TNR流浪动物管理系统</div>
        <div style="font-size:13px;color:var(--ink-light);margin-bottom:16px;">领养人端 v1.0.0</div>
        <div style="font-size:13px;color:var(--ink-muted);line-height:1.8;text-align:left;padding:0 16px;">
          TNR（捕捉-绝育-放归）是一种人道化的流浪动物治理模式。本系统致力于规范流浪动物管理全流程，实现可溯源、可监管、可闭环治理。
        </div>
        <div style="margin-top:16px;font-size:12px;color:var(--ink-light);">
          © 2025 TNR管理系统
        </div>
      </div>
    `,
    actions: '<button class="btn btn-primary" onclick="document.querySelector(\'.modal-overlay\').remove()">确定</button>'
  });
}

// === 辅助 HTML ===

function loadingHtml() {
  return '<div class="empty-state"><div class="empty-state-icon">⏳</div><div class="empty-state-text">加载中...</div></div>';
}

function errorHtml(msg) {
  return `<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">加载失败</div><div class="empty-state-hint">${TNR_UI.escape(msg || '未知错误')}</div></div>`;
}

function loginRequiredHtml(hint) {
  const loginUrl = window.TNR_LOGIN_URL;
  return `
    <div class="empty-state">
      <div class="empty-state-icon">🔒</div>
      <div class="empty-state-text">请先登录</div>
      <div class="empty-state-hint">${TNR_UI.escape(hint || '')}</div>
      <div style="margin-top:16px;"><a href="${loginUrl}" class="btn btn-primary">前往登录</a></div>
    </div>`;
}

// === 初始化 ===

(function init() {
  // 公开模式：仅展示领养大厅
  if (PUBLIC_MODE) {
    document.getElementById('headerTitle').textContent = '领养大厅';
    renderHall();
  } else {
    switchTab('hall');
  }
})();
