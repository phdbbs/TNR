/* ====================================================
   宠物医院端 SPA · 全部业务逻辑（TNR_API 异步调用）
   ==================================================== */

/* 去向（诊疗页的「建议去向」）图标与说明（第四十六轮 v5.1）。
   ⚠ 只放**展示**信息：去向的**合法取值**由后端
     `Treatment.DISPOSAL_CHOICES` 通过 `options.disposals` 下发，
     前端不另写一份清单 —— 两边各写一份必然漂移（后端加了去向、
     前端下拉里没有，界面上就是「这个去向选不了」且不报错）。 */
const TX_DISPOSAL_ICON = {
  pending_adopt: '🎗️', released: '🌳', owner_returned: '🏠',
  euthanized: '🕯️', dead: '⚰️'
};
const TX_DISPOSAL_DESC = {
  pending_adopt: '需登记领养并办出库', released: '待转运可选',
  owner_returned: '联系主人领回', euthanized: '需另开安乐死单',
  dead: '需另开无害化单'
};

const Hospital = {
  titles: {
    'dashboard': '首页看板',
    'receive': '动物交接管理',
    'treatment': '诊疗业务管理',
    'discharge': '出院管理',
    'material': '物料库存管理',
    'ledger': '单据台账',
    'profile': '个人中心'
  },

  state: {
    currentPage: 'dashboard',
    receiveTab: 'pending',
    // 诊疗页三个子页签：flow（v5.1 三步流程）/ list（诊疗记录）/ quick（旧口径快速登记）
    treatmentTab: 'flow',
    materialTab: 'pending',
    ledgerTab: 'transfer',
    // 诊疗看板筛选（第四十八轮五态）：取值、顺序、文案都在 `TNR_UI.BOARD_STAGE`，
    // 这里只记初始页签
    treatmentFilter: 'checking',
    // 出院二级菜单：adopt 领养 / dead 死亡出库 / owner 领回 / release 放归
    dischargeTab: 'adopt',
    // 领养二级菜单内的三态：ready 待领养 / reserved 已预约 / outbound 已出库
    adoptionTab: 'ready',
    // 诊疗看板数据（`/api/business/treatments/board/` 的响应体）
    treatmentBoard: null,
    // 当前诊疗流程会话：{ detail, draft, step, maxStep, open }
    // 非空 = 正在看某只动物的流程；null = 停在看板
    treatmentFlow: null,
    treatmentPetId: null,
    // 首页看板「数据概览」的统计区间（任意起止）。空串 = 不限。
    dashStart: '',
    dashEnd: ''
  },

  /* ========== 初始化 ========== */
  init() {
    this.bindSidebarNav();
    TNR_UI.initPortalShell();
    this.navigate('dashboard');
  },

  bindSidebarNav() {
    const navItems = document.querySelectorAll('.portal-sidebar-nav .nav-item[data-nav]');
    navItems.forEach(item => {
      item.addEventListener('click', (e) => {
        e.preventDefault();
        const page = item.dataset.nav;
        if (page) {
          navItems.forEach(x => x.classList.remove('active'));
          item.classList.add('active');
          this.navigate(page);
        }
      });
    });
  },

  navigate(pageId) {
    this.state.currentPage = pageId;
    document.querySelectorAll('.page-view').forEach(p => p.classList.remove('active'));
    const page = document.getElementById('page-' + pageId);
    if (page) {
      page.classList.add('active');
      page.innerHTML = '<div class="loading-state">加载中...</div>';
    }
    const titleEl = document.querySelector('.portal-topbar-title');
    if (titleEl) titleEl.textContent = this.titles[pageId] || '';
    const renderer = this['render_' + pageId.replace(/-/g, '_')];
    if (renderer) renderer.call(this);
  },

  navigateTo(pageId) {
    const el = document.querySelector('.nav-item[data-nav="' + pageId + '"]');
    if (el) el.click();
  },

  /* ========== 辅助方法 ========== */
  currentUser() { return TNR_API.getCurrentUser(); },
  getHospitalId() { const u = this.currentUser(); return u ? u.institution_id : null; },
  getHospitalName() { const u = this.currentUser(); return u ? (u.institution_name || u.name) : ''; },

  /* ⚠ 一律委托公共实现，不要在这里再写第二份。
     原来这两处是**裸截断** `substr(0, 10)` / `substr(0, 16)` —— 而后端
     `serialize_instance` 对 `DateTimeField` 用 `isoformat()`，`USE_TZ=True` 下是
     **UTC 带偏移**（`2026-09-22T06:05:50+00:00`）。裸截断 = 直接显示 UTC：
     转运台账 / 诊疗详情的时间**差 8 小时**；UTC ≥ 16:00（本地已跨日）时**日期差一天**。
     公共实现走 `new Date()` 按浏览器本地时区换算，是对的。
     实测（TZ=Asia/Shanghai，输入 `2026-09-22T20:30:00+00:00`）：
       旧 `formatDateTime` → `2026-09-22 20:30`；公共 → `2026-09-23 04:30`。 */
  formatDate(iso) { return TNR_UI.formatDate(iso); },

  formatDateTime(iso) { return TNR_UI.formatDateTime(iso); },

  parsePetCodes(codes) {
    if (!codes) return [];
    if (Array.isArray(codes)) return codes;
    return String(codes).split(',').map(c => c.trim()).filter(Boolean);
  },

  escape(s) { return TNR_UI.escape(s); },

  /* 转运单/诊疗单状态徽章一律取真源（`TNR_UI.TRANSFER_STATUS` / `TREATMENT_STATUS`），
     本文件以前各写一张表，`pending` 与监管端的「待接收」对不上账。 */
  transferStatusBadge(s) {
    return TNR_UI.transferStatusBadge(s);
  },

  treatmentStatusBadge(s) {
    return TNR_UI.treatmentStatusBadge(s);
  },

  /* ========== 1. 首页看板 ========== */
  async render_dashboard() {
    try {
      const [transfers, pets, materials, txns, treatments, euthanasia] = await Promise.all([
        TNR_API.getTransfersStrict(),
        TNR_API.getHospitalPetsStrict(),
        TNR_API.getMaterialsStrict(),
        TNR_API.getMaterialTransactionsStrict(),
        TNR_API.getTreatmentsStrict(),
        TNR_API.getEuthanasiaStrict()
      ]);

      // 待签收 = pending + partial（部分签收的单还有剩余动物没到）
      const pendRecv = transfers.filter(t => TNR_UI.isOpenTransferStatus(t.status)).length;
      /* 「在院」不等于「待诊疗」（第五十二轮电脑端巡检发现）。
         第五十六轮起，「诊疗完成」只落 `done_at`，动物进入**待出院**；
         只有出院管理里的终态动作才写 `disposed_at`。所以按每只动物最新
         一张诊疗单分流：`done_at` 有、`disposed_at` 空的进「待出院」，
         没完成诊疗的才是「待诊疗」—— 两者都不该在首页上消失。 */
      const latestTxByPet = new Map();
      treatments.forEach(t => {
        const prev = latestTxByPet.get(t.pet_id);
        if (!prev || (t.id || 0) > (prev.id || 0)) latestTxByPet.set(t.pet_id, t);
      });
      const inHouse = pets.filter(p => p.status === 'in_treatment');
      const pendTreatPets = inHouse.filter(p => {
        const t = latestTxByPet.get(p.id);
        return !t || !t.done_at;
      });
      const pendDischargePets = inHouse.filter(p => {
        const t = latestTxByPet.get(p.id);
        return !!t && !!t.done_at && !t.is_disposed;
      });
      const pendTreat = pendTreatPets.length;
      const pendDischarge = pendDischargePets.length;
      const stockWarn = materials.filter(m => (m.hospital_stock || 0) <= (m.safety_stock || 0)).length;
      // 待领养资料维护 = 历史「待领养」状态 ∪ 诊疗完成且医生建议领养的待出院动物。
      // 第五十六轮起诊疗完成不再改 `Pet.status`，只按状态筛会漏掉全部新数据。
      const legacyAdoptPets = pets.filter(p => p.status === 'pending_adopt');
      const advisedAdoptPets = pendDischargePets.filter(p => {
        const t = latestTxByPet.get(p.id);
        return t && t.disposal_advised === 'pending_adopt';
      });
      const pendAdopt = legacyAdoptPets.length + advisedAdoptPets.length;
      const pendingDisp = txns.filter(t => t.type === 'dispatch' && !(t.note || '').includes('[已签收]'));

      const todoItems = [];
      transfers.filter(t => TNR_UI.isOpenTransferStatus(t.status)).forEach(t => {
        const done = this.parsePetCodes(t.received_codes).length;
        const total = t.pet_count || 0;
        todoItems.push({icon: '📥',
          // 与后端 label 同一个词（待签收 / 部分签收），不再「待接收」另起炉灶
          title: TNR_UI.transferStatusText(t.status) + '转运单 ' + (t.ledger_no || ''),
          desc: '来源：' + (t.from_shelter_name || '') + ' · '
            + (t.status === 'partial' ? '已签收 ' + done + '/' + total : total + '只动物'),
          nav: 'receive'});
      });
      pendTreatPets.forEach(p => {
        todoItems.push({icon: '🔬', title: '待诊疗：' + (p.code || ''), desc: TNR_UI.joinInfo([p.name, p.species, p.breed]), nav: 'treatment'});
      });
      pendDischargePets.forEach(p => {
        todoItems.push({icon: '🚪', title: '待出院：' + (p.code || ''),
          desc: TNR_UI.joinInfo([p.name, '诊疗已完成，出院单据还没办完']), nav: 'discharge'});
      });
      pendingDisp.forEach(t => {
        todoItems.push({icon: '📦', title: '待接收物料：' + (t.material_name || ''), desc: '数量：' + (t.quantity || 0) + (t.unit || ''), nav: 'material'});
      });
      legacyAdoptPets.concat(advisedAdoptPets).forEach(p => {
        todoItems.push({icon: '🐾', title: '待领养资料维护：' + (p.code || ''),
          desc: TNR_UI.joinInfo([p.name, '请完善照片和简介']),
          nav: 'discharge', tab: 'adopt', sub: 'ready'});
      });

      /* —— 数据概览：带「统计区间」（任意起止）。
       *
       * 改动前这一页**完全没有时间维度**：4 张卡片全是「当前状态」计数，
       * 只增不减的任务数看不出「这周干了多少」。现在补一层按区间加总的业务量，
       * 原 4 张当前态卡片保留、并明确标注「不受统计区间影响」——
       * 两者混在一起而不标注，用户会以为「待接收」也是区间内的数字。
       *
       * ⚠ 卡片标题上的区间文字与下面的实际过滤**共用同一个 `win`**：
       * 两处各算一遍必然漂移，而漂移在界面上看不出来（标题写「最近一周」、
       * 数字却是全部时间）。 */
      const win = { start: this.state.dashStart || '', end: this.state.dashEnd || '' };
      const inR = (v) => TNR_UI.inDateRange(v, win.start, win.end);
      const rangeText = (win.start || win.end)
        ? ((win.start || '最早') + ' ~ ' + (win.end || '至今'))
        : '全部时间';
      // 交接单：有签收时间就按签收时间（「这段时间接收了多少」），否则退到创建时间。
      const recvInRange = transfers.filter(t => inR(t.received_at || t.created_at)).length;
      const treatInRange = treatments.filter(t => inR(t.created_at)).length;
      const consumeInRange = txns
        .filter(t => t.type === 'consume' && inR(t.date || t.created_at))
        .reduce((s, t) => s + (t.quantity || 0), 0);
      const euthInRange = euthanasia.filter(e => inR(e.euthanized_at || e.created_at)).length;

      let html = '';
      html += '<div class="card mb-4"><div class="card-header" style="flex-wrap:wrap;gap:var(--sp-2);">'
        + '<div class="card-title"><span class="card-title-bar"></span>数据概览</div>'
        + '<div class="flex gap-2" style="flex-wrap:wrap;align-items:center;">'
        + '<span class="fs-sm text-muted">开始日期</span>'
        + '<input type="date" class="form-input" id="hdashStart" style="width:auto;" value="' + win.start + '">'
        + '<span class="fs-sm text-muted">结束日期</span>'
        + '<input type="date" class="form-input" id="hdashEnd" style="width:auto;" value="' + win.end + '">'
        + '<button type="button" class="btn btn-secondary btn-sm" data-hdash-range="week">最近一周</button>'
        + '<button type="button" class="btn btn-secondary btn-sm" data-hdash-range="month">最近一月</button>'
        + '<button type="button" class="btn btn-secondary btn-sm" data-hdash-range="all">全部</button>'
        + '</div></div><div class="card-body">';
      html += '<div class="fs-sm text-muted mb-3">业务量统计区间：' + rangeText
        + '（下方「当前状态」为实时值，不受统计区间影响）</div>';
      html += '<div class="stat-grid">';
      html += '<div class="stat-card blue"><div class="stat-card-icon">📥</div><div><div class="stat-card-value">' + recvInRange + '</div><div class="stat-card-label">接收交接单（' + rangeText + '）</div></div></div>';
      html += '<div class="stat-card"><div class="stat-card-icon">🔬</div><div><div class="stat-card-value">' + treatInRange + '</div><div class="stat-card-label">诊疗记录（' + rangeText + '）</div></div></div>';
      html += '<div class="stat-card gold"><div class="stat-card-icon">📦</div><div><div class="stat-card-value">' + consumeInRange + '</div><div class="stat-card-label">物料消耗（' + rangeText + '）</div></div></div>';
      html += '<div class="stat-card rouge"><div class="stat-card-icon">⚖️</div><div><div class="stat-card-value">' + euthInRange + '</div><div class="stat-card-label">安乐死处置（' + rangeText + '）</div></div></div>';
      html += '</div>';
      html += '</div></div>';

      html += '<div class="fs-sm text-muted mb-3">当前状态（实时值，不受统计区间影响）</div>';
      html += '<div class="stat-grid">';
      html += '<div class="stat-card"><div class="stat-card-icon">📥</div><div><div class="stat-card-value">' + pendRecv + '</div><div class="stat-card-label">待签收交接单</div></div></div>';
      html += '<div class="stat-card blue"><div class="stat-card-icon">🔬</div><div><div class="stat-card-value">' + pendTreat + '</div><div class="stat-card-label">待诊疗任务</div></div></div>';
      html += '<div class="stat-card rouge"><div class="stat-card-icon">🚪</div><div><div class="stat-card-value">' + pendDischarge + '</div><div class="stat-card-label">待出院动物</div></div></div>';
      html += '<div class="stat-card gold"><div class="stat-card-icon">📦</div><div><div class="stat-card-value">' + stockWarn + '</div><div class="stat-card-label">库存预警</div></div></div>';
      html += '<div class="stat-card green"><div class="stat-card-icon">🐾</div><div><div class="stat-card-value">' + pendAdopt + '</div><div class="stat-card-label">待领养资料待维护</div></div></div>';
      html += '</div>';

      html += '<div class="dashboard-grid">';
      html += '<div class="card"><div class="card-header"><div class="card-title"><span class="card-title-bar"></span>待办任务</div></div><div class="card-body-flush">';
      if (todoItems.length === 0) {
        html += '<div class="empty-state" style="padding:var(--sp-8);"><div class="empty-state-icon">✓</div><div class="empty-state-text">暂无待办任务</div></div>';
      } else {
        todoItems.forEach(t => {
          html += '<div class="todo-item" data-todo="' + t.nav + '"'
            + (t.tab ? ' data-tab="' + t.tab + '"' : '')
            + (t.sub ? ' data-sub="' + t.sub + '"' : '') + '>';
          html += '<div class="todo-item-icon">' + t.icon + '</div>';
          html += '<div class="todo-item-content"><div class="todo-item-title">' + this.escape(t.title) + '</div><div class="todo-item-desc">' + this.escape(t.desc) + '</div></div>';
          html += '<span style="color:var(--ink-light);font-size:14px;">›</span></div>';
        });
      }
      html += '</div></div>';

      html += '<div class="card"><div class="card-header"><div class="card-title"><span class="card-title-bar"></span>快捷操作</div></div><div class="card-body"><div class="quick-grid">';
      const actions = [
        {icon: '📥', label: '动物交接', page: 'receive'},
        {icon: '🔬', label: '诊疗操作', page: 'treatment'},
        {icon: '🚪', label: '出院办理', page: 'discharge'},
        {icon: '📦', label: '物料管理', page: 'material'},
        {icon: '📊', label: '单据台账', page: 'ledger'}
      ];
      actions.forEach(a => {
        html += '<div class="quick-action" data-quick="' + a.page + '"'
          + (a.tab ? ' data-tab="' + a.tab + '"' : '') + '>'
          + '<div class="quick-action-icon">' + a.icon + '</div>'
          + '<div class="quick-action-label">' + a.label + '</div></div>';
      });
      html += '</div></div></div>';
      html += '</div>';

      html += '<div class="card mt-5"><div class="card-header"><div class="card-title"><span class="card-title-bar"></span>库存概览</div></div><div class="card-body">';
      html += this.renderStockOverview(materials);
      html += '</div></div>';

      document.getElementById('page-dashboard').innerHTML = html;

      document.querySelectorAll('[data-todo]').forEach(el => {
        el.addEventListener('click', () => {
          if (el.dataset.tab) this.state.dischargeTab = el.dataset.tab;
          if (el.dataset.sub) this.state.adoptionTab = el.dataset.sub;
          this.navigateTo(el.dataset.todo);
        });
      });
      document.querySelectorAll('[data-quick]').forEach(el => {
        el.addEventListener('click', () => {
          if (el.dataset.tab) this.state.dischargeTab = el.dataset.tab;
          this.navigateTo(el.dataset.quick);
        });
      });

      // ---- 数据概览：统计区间交互 ----
      const hFmt = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
        + '-' + String(d.getDate()).padStart(2, '0');
      document.querySelectorAll('#page-dashboard [data-hdash-range]').forEach(btn => {
        btn.addEventListener('click', () => {
          const today = new Date();
          const kind = btn.dataset.hdashRange;
          if (kind === 'all') {
            this.state.dashStart = '';
            this.state.dashEnd = '';
          } else {
            const days = kind === 'week' ? 6 : 29;
            this.state.dashStart = hFmt(new Date(today.getTime() - days * 86400000));
            this.state.dashEnd = hFmt(today);
          }
          this.render_dashboard();
        });
      });
      const hDashStartEl = document.getElementById('hdashStart');
      const hDashEndEl = document.getElementById('hdashEnd');
      const applyHDash = () => {
        this.state.dashStart = (hDashStartEl.value || '').trim();
        this.state.dashEnd = (hDashEndEl.value || '').trim();
        // 起止填反不静默变成 0 —— 明确提示，区间文字本身也会照原样显示。
        if (this.state.dashStart && this.state.dashEnd
            && this.state.dashStart > this.state.dashEnd) {
          TNR_UI.toast('开始日期晚于结束日期，该区间内不会有记录', 'warning');
        }
        this.render_dashboard();
      };
      [hDashStartEl, hDashEndEl].forEach(inp => {
        if (!inp) return;
        inp.addEventListener('change', applyHDash);
        inp.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter') { ev.preventDefault(); applyHDash(); }
        });
      });
    } catch (e) {
      document.getElementById('page-dashboard').innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">加载失败</div><div class="empty-state-desc">' + this.escape(e.message) + '</div></div>';
    }
  },

  renderStockOverview(materials) {
    return materials.map(m => {
      const stock = m.hospital_stock || 0;
      const safety = m.safety_stock || 1;
      const pct = Math.min(100, (stock / (safety * 2)) * 100);
      const isLow = stock < safety;
      const color = isLow ? 'var(--rouge)' : (stock < safety * 1.5 ? 'var(--autumn-gold)' : 'var(--bamboo-green)');
      return '<div style="display:flex;align-items:center;gap:var(--sp-4);padding:var(--sp-3) 0;border-bottom:1px solid var(--border-faint);">' +
        '<div style="flex:1;"><div style="display:flex;align-items:center;gap:var(--sp-2);margin-bottom:4px;">' +
        '<span style="font-weight:500;">' + this.escape(m.name) + '</span>' +
        '<span class="badge ' + (isLow ? 'badge-danger' : 'badge-default') + '">' + TNR_API.getMaterialCategoryText(m.category) + '</span>' +
        (isLow ? '<span class="badge badge-danger">库存不足</span>' : '') + '</div>' +
        '<div class="stock-bar"><div class="stock-bar-fill" style="width:' + pct + '%;background:' + color + ';"></div></div></div>' +
        '<div style="text-align:right;min-width:100px;"><div style="font-family:var(--font-display);font-size:var(--fs-xl);font-weight:700;color:' + color + ';">' + stock +
        '<span style="font-size:var(--fs-sm);color:var(--ink-muted);font-weight:400;"> / ' + safety + (m.unit || '') + '</span></div>' +
        '<div style="font-size:var(--fs-xs);color:var(--ink-light);">当前 / 安全库存</div></div></div>';
    }).join('');
  },

  /* ========== 2. 动物交接管理 ========== */
  async render_receive() {
    try {
      const transfers = await TNR_API.getTransfersStrict();
      // 「待签收」= pending + partial：部分签收的单还有剩余动物没到，
      // 仍然要出现在待办里，否则医院会以为这张单已经处理完了。
      // 口径只有一处定义：`TNR_UI.OPEN_TRANSFER_STATUS`。
      const pend = transfers.filter(t => TNR_UI.isOpenTransferStatus(t.status)).length;
      const recv = transfers.filter(t => t.status === 'received').length;
      const rej = transfers.filter(t => t.status === 'rejected').length;
      const vd = transfers.filter(t => t.status === 'void').length;

      // 标签页名也走真源：以前这四个词手写在界面里，后端改 label 时这里最容易被忘。
    let html = '<div class="tabs">' +
        '<div class="tab-item ' + (this.state.receiveTab === 'pending' ? 'active' : '') + '" data-rtab="pending">' + TNR_UI.transferStatusText('pending') + ' (' + pend + ')</div>' +
        '<div class="tab-item ' + (this.state.receiveTab === 'received' ? 'active' : '') + '" data-rtab="received">' + TNR_UI.transferStatusText('received') + ' (' + recv + ')</div>' +
        '<div class="tab-item ' + (this.state.receiveTab === 'rejected' ? 'active' : '') + '" data-rtab="rejected">' + TNR_UI.transferStatusText('rejected') + ' (' + rej + ')</div>' +
        '<div class="tab-item ' + (this.state.receiveTab === 'void' ? 'active' : '') + '" data-rtab="void">' + TNR_UI.transferStatusText('void') + ' (' + vd + ')</div>' +
        '</div>' +
        // 四个状态标签页共用一条筛选条，位置紧跟 .tabs（CSS 里 .tabs 钉 top:0、.filter-bar 钉 top:46px）。
        TNR_UI.renderFilterBar([
          { key: 'keyword', label: '搜索', type: 'search', placeholder: '交接单号 / 宠物编号 / 来源捕捉点' },
          { key: 'start_date', label: '开始日期', type: 'date' },
          { key: 'end_date', label: '结束日期', type: 'date' }
        ], '', { id: 'receiveFilterBar' }) +
        '<div id="receiveTabContent"></div>';

      document.getElementById('page-receive').innerHTML = html;

      document.querySelectorAll('#page-receive [data-rtab]').forEach(el => {
        el.addEventListener('click', () => {
          this.state.receiveTab = el.dataset.rtab;
          document.querySelectorAll('#page-receive .tab-item').forEach(t => {
            t.classList.toggle('active', t.dataset.rtab === this.state.receiveTab);
          });
          this.renderReceiveTab(transfers);
        });
      });

      this._receiveCache = transfers;
      this.renderReceiveTab(transfers);
      TNR_UI.bindFilter(() => this.renderReceiveTab(this._receiveCache || []),
        document.getElementById('page-receive'));
    } catch (e) {
      document.getElementById('page-receive').innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">加载失败</div></div>';
    }
  },

  async renderReceiveTab(allTransfers) {
    const tab = this.state.receiveTab;
    // 「待签收」包含部分签收的单：还剩动物没到，仍是待办。
    const transfers = allTransfers.filter(t => tab === 'pending'
      ? TNR_UI.isOpenTransferStatus(t.status)
      : t.status === tab);
    const c = document.getElementById('receiveTabContent');
    if (!c) return;

    if (transfers.length === 0) {
      c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📭</div><div class="empty-state-text">暂无数据</div><div class="empty-state-desc">暂无' + TNR_UI.transferStatusText(tab) + '的交接单</div></div>';
      return;
    }

    let pets = [];
    let petsError = null;
    try { pets = await TNR_API.getHospitalPetsStrict(); } catch (e) { petsError = e; }

    const rows = transfers.map(t => {
      const codes = this.parsePetCodes(t.pet_codes);
      const petInfo = codes.map(code => {
        const p = pets.find(x => x.code === code);
        if (!p) return '<div style="display:flex;align-items:center;gap:var(--sp-2);padding:4px 0;"><div class="photo-placeholder">🐾</div><div><div style="font-weight:500;">' + this.escape(code) + '</div><div style="font-size:var(--fs-sm);color:var(--ink-muted);">' + (petsError ? '档案加载失败' : '未知') + '</div></div></div>';
        return '<div style="display:flex;align-items:center;gap:var(--sp-2);padding:4px 0;"><div class="photo-placeholder">🐾</div><div><div style="font-weight:500;">' + this.escape(p.code) + '</div><div style="font-size:var(--fs-sm);color:var(--ink-muted);">' + TNR_UI.joinInfo([p.name, p.species, p.breed, p.gender]) + '</div></div></div>';
      }).join('');

      // 所有状态统一「查看详情」——签收 / 驳回 / 部分签收都在详情里做。
      // 原先行内直接给「确认签收」按钮，医院看不到这单到底送来了几只、
      // 分别是谁，只能凭单号盲签；部分签收更是不可能（只有整单一个动作）。
      const actions = '<button class="btn btn-secondary btn-sm" data-detail="' + t.id + '">查看详情</button>'
        + (TNR_UI.isOpenTransferStatus(t.status)
          ? '<button class="btn btn-success btn-sm" data-detail-go="' + t.id + '">去签收</button>' : '');

      const doneCount = this.parsePetCodes(t.received_codes).length;
      const totalCount = this.parsePetCodes(t.pet_codes).length || t.pet_count || 0;
      const statusCell = this.transferStatusBadge(t.status)
        + (t.status === 'partial'
          ? '<div class="fs-sm text-muted">已签收 ' + doneCount + '/' + totalCount + '</div>' : '');

      return {
        _ledger: '<div style="font-weight:500;">' + this.escape(t.ledger_no || '') + '</div><div style="font-size:var(--fs-xs);color:var(--ink-light);">' + this.formatDateTime(t.created_at) + '</div>',
        ledger_no: t.ledger_no || '',
        created_at: t.created_at,
        petInfo: petInfo || '<span class="text-muted">—</span>',
        from_shelter_name: t.from_shelter_name || '',
        pet_count: t.pet_count || 0,
        pet_count_num: t.pet_count || 0,
        status: statusCell,
        status_raw: t.status,
        _actions: '<div class="flex gap-2">' + actions + '</div>',
      };
    });

    const bar = document.getElementById('receiveFilterBar');
    const v = TNR_UI.getFilterValues(bar);
    const shown = rows.filter(r => {
      // ⚠ `created_at` 是 DateTimeField，后端序列化成 **UTC 带偏移**的串；
      // 统一交给 `TNR_UI.inDateRange` 按**本地日期**比较（裸 `.slice(0,10)` 会把
      // 北京时间 00:00–08:00 的记录算到前一天）。
      if (!TNR_UI.inDateRange(r.created_at, v.start_date, v.end_date)) return false;
      if (!v.keyword) return true;
      // 行是映射后的展示结构（含 HTML 片段），逐字段拼 haystack，避免漏列。
      return Object.keys(r).map(k => r[k]).join(' ').toLowerCase().includes(v.keyword);
    });

    TNR_UI.mountTable(c, { id: 'hospital.receive', columns: [
        { title: '交接单号', key: '_ledger', sortable: false },
        { title: '宠物信息', key: 'petInfo', sortable: false },
        { title: '来源捕捉点', key: 'from_shelter_name' },
        { title: '数量', key: 'pet_count_num', align: 'center' },
        { title: '状态', key: 'status', sortable: false },
        { title: '操作', key: '_actions', sortable: false },
      ], data: shown, emptyText: '暂无待交接动物' });

    if (petsError) {
      c.insertAdjacentHTML('afterbegin',
        '<div class="form-hint" style="color:var(--rouge);margin-bottom:var(--sp-3);display:flex;align-items:center;gap:var(--sp-2);flex-wrap:wrap;">'
        + '<span>⚠ 动物档案加载失败：' + this.escape(petsError.message || '请稍后重试') + '，列表仅显示编号。</span>'
        + '<button type="button" class="btn btn-secondary btn-sm" data-reload-pets>重新加载</button></div>');
      const reloadBtn = c.querySelector('[data-reload-pets]');
      if (reloadBtn) reloadBtn.onclick = () => this.renderReceiveTab(allTransfers);
    }

    TNR_UI.delegateClick(c, {
      'data-detail': (id) => this.viewTransferDetail(id),
      'data-detail-go': (id) => this.viewTransferDetail(id),
    });
  },

  /* 交接单详情：先看清「送来了什么」，再决定签收多少。
     列表页的行内只有单号与数量，医院没法据此判断该不该签 —— 实际到货
     可能少几只、也可能多了只没登记的病猫。详情里逐只列出（带档案信息与
     签收状态），勾选本次实际收到的即可**部分签收**。 */
  async viewTransferDetail(transferId) {
    const all = this._receiveCache || [];
    const t = all.find(x => String(x.id) === String(transferId));
    if (!t) { TNR_UI.toast('交接单不存在或列表已刷新，请重试', 'warning'); return; }
    let pets = [];
    let petsError = null;
    try { pets = await TNR_API.getHospitalPetsStrict(); } catch (e) { petsError = e; }

    const codes = this.parsePetCodes(t.pet_codes);
    const doneCodes = this.parsePetCodes(t.received_codes);
    const canSign = TNR_UI.isOpenTransferStatus(t.status);
    const pendingCount = codes.length - doneCodes.length;

    const rows = codes.map(code => {
      const p = pets.find(x => x.code === code) || {};
      const isDone = doneCodes.indexOf(code) >= 0;
      const meta = [p.name, p.species, p.breed, p.gender]
        .filter(Boolean).map(x => this.escape(x)).join(' · ')
        || (petsError ? '档案加载失败' : '档案信息待补全');
      return '<label class="xfer-pet' + (isDone ? ' on' : '') + '">'
        + '<input type="checkbox" data-code="' + this.escape(code) + '"'
        + (isDone || !canSign ? ' disabled' : '') + (isDone ? ' checked' : '') + '>'
        + '<div class="xfer-ph">🐾</div>'
        + '<div class="xfer-info"><div class="xfer-code">' + this.escape(code) + '</div>'
        + '<div class="xfer-meta">' + meta + '</div></div>'
        + (isDone ? '<span class="badge badge-success">已签收</span>'
                  : '<span class="badge badge-warning">待签收</span>')
        + '</label>';
    }).join('');

    const info = (petsError
        ? '<div class="form-hint" style="color:var(--rouge);margin-bottom:var(--sp-3);">'
          + '⚠ 动物档案加载失败：' + this.escape(petsError.message || '请稍后重试')
          + '，以下仅显示编号，请关闭弹窗后重试。</div>'
        : '')
      + '<div class="tx-kv" style="margin-bottom:12px;">'
      + '<div><div class="k">交接单号</div><div class="v">' + this.escape(t.ledger_no || '—') + '</div></div>'
      + '<div><div class="k">来源捕捉点</div><div class="v">' + this.escape(t.from_shelter_name || '—') + '</div></div>'
      + '<div><div class="k">接收医院</div><div class="v">' + this.escape(t.to_hospital_name || '—') + '</div></div>'
      + '<div><div class="k">发出时间</div><div class="v">' + this.formatDateTime(t.created_at) + '</div></div>'
      + '<div><div class="k">状态</div><div class="v">' + this.transferStatusBadge(t.status) + '</div></div>'
      + '<div><div class="k">动物数量</div><div class="v">' + codes.length + ' 只（已签收 ' + doneCodes.length + '）</div></div>'
      + '</div>'
      + (t.note ? '<div class="tx-tip inf"><span>📝</span><div>备注：' + this.escape(t.note) + '</div></div>' : '')
      + (t.status === 'rejected' ? '<div class="tx-tip wrn"><span>⚠</span><div>驳回理由：' + this.escape(t.reject_reason || '—') + '</div></div>' : '')
      + (t.status === 'void' ? '<div class="tx-tip wrn"><span>⚠</span><div>捕捉点已撤回该交接单。</div></div>' : '');

    TNR_UI.modal({
      title: '交接单详情 · ' + this.escape(t.ledger_no || ''),
      size: 'lg',
      body: info
        + '<div class="xfer-head"><span>动物清单</span>'
        + (canSign ? '<span class="text-muted fs-sm">勾选本次实际收到的动物，可以只签收一部分</span>' : '')
        + '</div>'
        + '<div class="xfer-list">' + (rows || '<div class="tx-empty">本单没有动物</div>') + '</div>',
      actions: canSign
        ? '<button class="btn btn-secondary" data-action="close">取消</button>'
          + (pendingCount > 0
            ? '<button class="btn btn-danger" id="xferRejectBtn">驳回剩余 ' + pendingCount + ' 只</button>' : '')
          + '<button class="btn btn-success" id="xferSignBtn" disabled>请勾选要签收的动物</button>'
        : '<button class="btn btn-primary" data-action="close">关闭</button>',
      onShow: (overlay) => {
        // ⚠ `TNR_UI.modal` 的 `onShow(overlay)` **不传** closeFn，
        // 这里自己拿「关闭」按钮来收口（直接 `overlay.remove()` 会留下
        // 未解绑的 Esc 监听，多开几次详情就叠一堆）。
        const closeModal = () => {
          const btn = overlay.querySelector('[data-action="close"]');
          if (btn) btn.click(); else overlay.remove();
        };
        const signBtn = overlay.querySelector('#xferSignBtn');
        const rejectBtn = overlay.querySelector('#xferRejectBtn');
        const boxes = Array.prototype.slice.call(overlay.querySelectorAll('input[data-code]'));
        const sync = () => {
          if (!signBtn) return;
          const n = boxes.filter(b => b.checked && !b.disabled).length;
          signBtn.disabled = n === 0;
          signBtn.textContent = n ? '确认签收已选 ' + n + ' 只' : '请勾选要签收的动物';
        };
        boxes.forEach(b => b.addEventListener('change', sync));
        sync();
        if (signBtn) signBtn.onclick = () => {
          const picked = boxes.filter(b => b.checked && !b.disabled)
            .map(b => b.dataset.code);
          if (!picked.length) { TNR_UI.toast('请至少勾选一只动物', 'warning'); return; }
          closeModal();
          this.confirmReceive(t.id, picked);
        };
        if (rejectBtn) rejectBtn.onclick = () => {
          closeModal();
          this.rejectTransfer(t.id);
        };
      }
    });
  },

  async confirmReceive(transferId, codes) {
    const n = (codes || []).length;
    TNR_UI.confirm('确认签收选中的 ' + n + ' 只动物？签收后它们将变更为「待诊疗」，'
      + '本单未勾选的动物仍留在待签收。', '确认签收').then(async ok => {
      if (!ok) return;
      try {
        const res = await TNR_API.receiveTransfer(transferId, codes);
        if (res.success) {
          TNR_UI.toast(res.message || '签收成功，宠物已进入待诊疗状态', 'success');
          this.render_receive();
        } else {
          TNR_UI.toast(res.message || '签收失败', 'danger');
        }
      } catch (e) {
        TNR_UI.toast(e.message || '操作失败', 'danger');
      }
    });
  },

  rejectTransfer(transferId) {
    TNR_UI.modal({
      title: '驳回转运单',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="form-group"><label class="form-label">驳回理由<span class="req">*</span></label><textarea class="form-textarea" id="rejectReasonInput" placeholder="请填写驳回理由" required></textarea></div>' +
          '<div style="display:flex;gap:var(--sp-3);justify-content:flex-end;margin-top:var(--sp-4);"><button class="btn btn-secondary" data-close>取消</button><button class="btn btn-danger" id="confirmRejectBtn">确认驳回</button></div>';
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelector('#confirmRejectBtn').onclick = async () => {
          const r = bodyEl.querySelector('#rejectReasonInput').value.trim();
          if (!r) { TNR_UI.toast('请填写驳回理由', 'warning'); return; }
          try {
            const res = await TNR_API.rejectTransfer(transferId, r);
            if (res.success) {
              TNR_UI.toast('已驳回该转运单', 'success');
              closeFn();
              this.render_receive();
            } else {
              TNR_UI.toast(res.message || '驳回失败', 'danger');
            }
          } catch (e) {
            TNR_UI.toast(e.message || '操作失败', 'danger');
          }
        };
      }
    });
  },

  viewRejectReason(transferId, transfers) {
    const t = transfers.find(x => String(x.id) === String(transferId));
    TNR_UI.modal({
      title: '驳回理由',
      body: '<div style="padding:var(--sp-3);background:var(--rouge-light);border-radius:var(--r-md);font-size:var(--fs-md);color:var(--ink-text);line-height:1.8;">' + this.escape(t ? (t.reject_reason || '无') : '无') + '</div>',
      actions: '<button class="btn btn-primary" data-modal-close>关闭</button>'
    });
  },

  /* ========== 3. 诊疗业务管理（第四十七轮 v5.2 三步流程） ==========
   *
   * 两个子页签：
   *   flow —— v5.2 口径：看板（五态，见 `TNR_UI.BOARD_STAGE`）→ 点动物进三步流程
   *   list —— 诊疗记录（原有列表，保留）
   *
   * ⚠ 第四十七轮删掉了旧口径「快速登记」页签（一次性提交整单并当场扣库存，
   *   字段口径与 v5.1 三步流程不一致）。删之前先把它的**唯一独有价值** ——
   *   绝育手术病历（手术日期 / 主刀 / 麻醉 / 诊断 / 手术过程 / 恢复）——
   *   并进了三步流程的「绝育」项目卡片；`treatment_create` 接口一并移除，
   *   否则会变成「接口存在、界面没接」的半截功能。病历的读写现在统一走
   *   `services_treatment.save_sterilization()`。
   *
   * 事件一律走 `TNR_UI.delegateClick`（绑在 `#page-treatment` 这个**稳定容器**上）：
   * 视图每次都是整体重渲染 `innerHTML`，直接绑在子节点上的监听器会随旧节点一起
   * 被丢弃，表现为「点按钮没反应，且控制台干干净净」。
   */
  async render_treatment() {
    const page = document.getElementById('page-treatment');
    const TABS = [['flow', '诊疗流程'], ['list', '诊疗记录']];
    page.innerHTML = '<div class="sub-tabs">' + TABS.map(([k, t]) =>
      '<div class="sub-tab ' + (this.state.treatmentTab === k ? 'active' : '') +
      '" data-act="ttab" data-v="' + k + '">' + t + '</div>').join('') +
      '</div><div id="treatmentContent"></div>';

    TNR_UI.delegateClick(page, { 'data-act': (v, el) => this.onTreatmentAct(v, el) }, 'treatment');
    this.bindTreatmentInputs(page);
    this.renderTreatmentContent();
  },

  renderTreatmentContent() {
    if (this.state.treatmentTab === 'list') this.renderTreatmentList();
    else this.renderTreatmentFlow();
  },

  /* 输入事件同样要委托。
     处置项目卡里的日期 / 芯片号 / 备注是**边输边存**的，而卡片会被
     `refreshItemCard()` 局部重建 —— 每次重建都重绑一遍必然漏掉某条路径，
     漏掉的表现是「改了值，保存时还是旧的」，且不报错。 */
  bindTreatmentInputs(page) {
    if (this._txInputBound) return;
    this._txInputBound = true;
    const handler = (e) => this.onTreatmentInput(e.target);
    page.addEventListener('input', handler);
    page.addEventListener('change', handler);
  },

  /* ==================== 诊疗看板 ==================== */
  async renderTreatmentFlow() {
    const c = document.getElementById('treatmentContent');
    if (!c) return;
    if (this.state.treatmentFlow) { this.renderFlowView(); return; }
    c.innerHTML = '<div class="loading-state">加载中...</div>';
    try {
      // 两条都是**严格读**（失败抛错）。用静默读的话，403/500 会被渲染成
      // 「该分类下暂无动物」—— 把「加载失败」伪装成「没有数据」，
      // 操作员会以为真的没病人。
      const [board, materials] = await Promise.all([
        TNR_API.getTreatmentBoard(),
        TNR_API.getMaterialsStrict()
      ]);
      this.state.treatmentBoard = board;
      this._txMaterials = materials || [];
      // 看板数据在途时用户可能已经进了某只动物的流程（出院页横幅「去补录」就是这样）。
      // 流程视图已经渲染，这里再 renderBoard 会把它整块覆盖回看板。
      if (this.state.treatmentFlow) { this.renderFlowView(); return; }
      this.renderBoard();
    } catch (e) {
      c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚠️</div>'
        + '<div class="empty-state-text">诊疗看板加载失败</div>'
        + '<div class="empty-state-desc">' + this.escape(e.message || '请稍后重试') + '</div></div>';
    }
  },

  txBoardRows(kind) {
    const board = this.state.treatmentBoard || {};
    return board[kind] || [];
  },

  txStageBadge(row) {
    // 五态判据只认后端 `board_stage`（`_board_stage()` 是唯一真源），
    // 文案只认 `TNR_UI.BOARD_STAGE`。这里以前自带一张 `[文案, badge]` 表，
    // 与页签表各写一遍，改一个页签名要翻两个方法；详情抽屉的「当前阶段」还有第三张表，
    // 于是同一个 code `checking` 在看板叫「待检」、在抽屉叫「待接诊」，
    // 同一只动物、同一时刻两种说法。
    const stage = row.board_stage || '';
    // 已出院显示最终去向；待出院显示医生在「诊疗完成」时给的**建议**，
    // 让下一班的人一眼知道该开哪张出院单。
    const sub = stage === 'discharged' ? row.disposal_label
      : (stage === 'pending_discharge' ? row.disposal_advised_label : '');
    return TNR_UI.boardStageBadge(stage)
      + (sub ? '<div class="fs-sm text-muted">'
        + (stage === 'pending_discharge' ? '建议：' : '')
        + this.escape(sub) + '</div>' : '');
  },

  renderBoard() {
    const c = document.getElementById('treatmentContent');
    if (!c) return;
    // 五态（与后端 `_board_stage()` 一一对应）：取值与文案取真源，
    // 这里只留**每一步该干什么**的悬停提示（医院端操作语，别处不需要）。
    const TIPS = {
      checking: '已交接、尚未提交检查单',
      pending_diagnose: '检查已提交、尚未下诊断',
      treating: '已下诊断、尚未治疗完成',
      pending_discharge: '诊疗完成、等待办结出院',
      discharged: '已办结出院'
    };
    const FILTERS = TNR_UI.BOARD_STAGE_ORDER.map(k => ({
      k: k, t: TNR_UI.boardStageText(k), tip: TIPS[k] || ''
    }));
    let html = '<div class="tx-filter-tabs">';
    FILTERS.forEach(f => {
      html += '<button class="tx-ft ' + (this.state.treatmentFilter === f.k ? 'active' : '') + '"'
        + ' data-act="tx-filter" data-v="' + f.k + '" title="' + this.escape(f.tip) + '">'
        + f.t + ' <span class="cnt">' + this.txBoardRows(f.k).length + '</span></button>';
    });
    html += '<button class="btn btn-sm btn-secondary" data-act="tx-refresh"'
      + ' style="margin-left:auto;flex:none;">刷新</button></div>';

    // 统一布局（第四十八轮）：**左侧状态模块 + 右侧搜索条件 + 列表**。
    // 搜索条与表格容器的顺序不能反 —— 先写进 DOM 再 mountTable，
    // 否则选择器查不到元素，表格会静默不渲染。
    html += TNR_UI.renderFilterBar([
      { key: 'keyword', label: '搜索', type: 'search', placeholder: '宠物编号 / 名称 / 品种 / 转运单' },
      { key: 'start_date', label: '入院开始', type: 'date' },
      { key: 'end_date', label: '入院结束', type: 'date' }
    ], '', { id: 'txBoardFilterBar' });
    html += '<div id="txBoardTable"></div>';
    c.innerHTML = html;

    this.renderBoardTable();
    TNR_UI.bindFilter(() => this.renderBoardTable(), c);
  },

  renderBoardTable() {
    const c = document.getElementById('treatmentContent');
    if (!c) return;
    const rows = this.txBoardRows(this.state.treatmentFilter);
    const bar = document.getElementById('txBoardFilterBar');
    const v = TNR_UI.getFilterValues(bar);
    const shown = rows.filter(r => {
      // `receive_date` 是纯日期串（`_intake_info` 已 isoformat），
      // 与筛选条的 YYYY-MM-DD 同口径，不需要再走时区换算。
      if (!TNR_UI.inDateRange(r.receive_date, v.start_date, v.end_date)) return false;
      if (!v.keyword) return true;
      return [r.pet_code, r.pet_name, r.species, r.breed, r.transfer_no, r.shelter]
        .join(' ').toLowerCase().indexOf(v.keyword) !== -1;
    });

    // 每个状态对应**唯一一个**操作 —— 用户不用猜「这一步该点哪个按钮」。
    const ACTION = {
      checking: ['检查', 'check'],
      pending_diagnose: ['诊断', 'diagnose'],
      treating: ['治疗', 'treat'],
      pending_discharge: ['出院', 'discharge'],
      discharged: ['查看', 'view']
    };
    const data = shown.map(r => {
      const act = ACTION[r.board_stage] || ['查看', 'view'];
      const attr = [r.species, r.gender, r.breed, r.color]
        .filter(Boolean).map(x => this.escape(x)).join(' · ') || '—';
      return {
        _pet: '<div style="font-weight:500;">' + this.escape(r.pet_code) + ' · '
          + this.escape(r.pet_name || '未命名') + '</div>'
          + '<div class="fs-sm text-muted">' + attr + '</div>',
        _intake: '<div>' + this.escape(r.transfer_no || '—') + '</div>'
          + '<div class="fs-sm text-muted">' + this.escape(r.shelter || '—') + ' · '
          + (r.receive_date ? this.formatDate(r.receive_date) : '—') + '</div>',
        _status: this.txStageBadge(r),
        _actions: '<button class="btn btn-sm '
          + (r.board_stage === 'discharged' ? 'btn-secondary' : 'btn-primary') + '"'
          + ' data-txact="' + act[1] + '" data-v="' + r.pet_id + '"'
          + ' data-tid="' + (r.treatment_id || '') + '"'
          + ' data-dsp="' + this.escape(r.disposal_advised || '') + '">' + act[0] + '</button>',
        pet_code: r.pet_code,
        pet_name: r.pet_name || '',
        receive_date: r.receive_date || '',
      };
    });

    TNR_UI.mountTable('#txBoardTable', {
      id: 'hospital.treatment.board',
      columns: [
        { title: '动物', key: '_pet', sortable: false },
        { title: '来源 / 入院', key: '_intake', sortable: false },
        { title: '状态', key: '_status', sortable: false },
        { title: '操作', key: '_actions', sortable: false, align: 'right' },
      ],
      data: data,
      emptyText: '该状态下暂无动物'
    });

    TNR_UI.delegateClick(c, {
      'data-txact': (kind, el) => this.onTxRowAction(kind, el)
    });
  },

  onTxRowAction(kind, el) {
    const petId = el.dataset.v;
    const tid = el.dataset.tid || '';
    if (kind === 'discharge') {
      this.openDischargeChoice(petId, el.dataset.dsp || '', el.dataset.tid || '');
      return;
    }
    const step = { check: 1, diagnose: 2, treat: 3 }[kind] || undefined;
    this.openTreatmentFlow(petId, tid, step);
  },

  /* ==================== 出院（第四十八轮） ====================
   * 四类去向：领养 / 死亡（含安乐死）/ 领回 / 放归。
   *
   * 诊疗单的「已出院」只由这里的**终态动作**决定（第五十六轮）：
   * 领养出库、死亡出库单提交、运回单对方签收、主人领回完成时，
   * 后端才写 `Treatment.disposed_at`。诊疗页选的只是「建议去向」，
   * 点「诊疗完成」后动物仍在「待出院」。
   *
   * 前端只负责按去向开单，办结由后端随单据终态一起完成：
   *   - 领养：登记领养单 → 领养列表里办「领养出库」；
   *   - 死亡：登记死亡 / 安乐死 → 办「死亡出库单」；
   *   - 领回：领回单当场完成交接（终态）；
   *   - 放归：开运回单 → 捕捉点签收（终态）。
   */
  openDischargeChoice(petId, advised, tid) {
    const OPTS = [
      { k: 'adopt', ic: '🏠', n: '领养', d: '开具领养单，动物由领养人领走' },
      { k: 'dead', ic: '🕯️', n: '死亡（含安乐死）', d: '安乐死，或死亡后的无害化处理' },
      { k: 'owner', ic: '🔁', n: '领回', d: '主人到场认领并领回' },
      { k: 'release', ic: '🌿', n: '放归', d: '先转运回原小区，再放归回捕捉地' }
    ];
    // `Treatment.disposal_advised`（后端 key）→ 本弹窗的单据类型。
    // 「死亡 / 无害化」和「安乐死」共用同一张登记表单，两个 key 都落到 dead。
    const ADVISED_KIND = {
      pending_adopt: 'adopt', dead: 'dead', euthanized: 'dead',
      owner_returned: 'owner', released: 'release'
    };
    const suggested = ADVISED_KIND[advised] || '';
    if (!suggested) {
      // 没记建议 = 后端四个去向**全部** 400。开一个让人「人工选择」的弹窗
      // 只会让操作员点一次吃一个报错，而且选完仍然办不了（建议没动）。
      // 直接把他送到第三步的去向建议上补录 —— 改完回来列表里就有这只动物。
      TNR_UI.confirm(
        '这张诊疗单在「诊疗完成」时没有记录建议去向，出院管理的四个去向都选不到它。<br>'
        + '先到诊疗流程补录建议去向，再回来办理出院。',
        '需要先补录建议去向').then(ok => {
        if (ok) this.openTreatmentFlow(petId, tid, 3);
      });
      return;
    }
    const label = (OPTS.find(o => o.k === suggested) || {}).n || suggested;
    TNR_UI.modal({
      title: '办理出院 · 选择去向',
      size: 'lg',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="dsp-choice">' + OPTS.map(o =>
          '<button type="button" class="dsp-opt'
          + (o.k === suggested ? ' suggested' : ' disabled') + '"'
          + (o.k === suggested ? ' data-dsp="' + o.k + '"' : ' disabled') + '>'
          + '<span class="ic">' + o.ic + '</span>'
          + '<span class="tx"><b>' + o.n
          + (o.k === suggested ? '<span class="dsp-tag">医生已建议</span>' : '')
          + '</b><i>' + (o.k === suggested ? o.d : '与建议去向不符，不可办理') + '</i></span>'
          + '<span class="go">›</span></button>').join('')
          + '<div class="dsp-advice">出院去向必须与「诊疗完成」时的建议去向一致（现在：'
          + this.escape(label) + '）。确实要更换，请先到诊疗流程修改建议去向，'
          + '再回来开单。'
          + '<button type="button" class="btn btn-link btn-sm" data-dspedit="1">'
          + '现在就去修改</button></div>';
        bodyEl.querySelectorAll('[data-dsp]').forEach(btn => {
          btn.onclick = () => {
            closeFn();
            this.openDischargeForm(btn.dataset.dsp, petId);
          };
        });
        const edit = bodyEl.querySelector('[data-dspedit]');
        if (edit) edit.onclick = () => {
          closeFn();
          this.openTreatmentFlow(petId, tid, 3);
        };
      }
    });
  },

  openDischargeForm(kind, petId) {
    const shelterOptions = ((this._dsp && this._dsp.returnShelters) || []).map(s => ({
      value: s.id,
      label: s.name + (s.is_city ? '（市级）' : '（' + (s.district_name || '') + '）')
    }));
    const pet = this.dspPet(petId) || {};
    const DEF = {
      adopt: {
        title: '登记领养预约',
        hint: '保存后动物进入「已预约」，还要在「出院管理 · 领养 · 已预约」中'
          + '填写领养出库单（交接人、出库日期、领养人签字、可选照片）才完成交接。',
        fields: [
          { k: 'adopter_name', l: '领养人姓名', req: true },
          { k: 'adopter_phone', l: '联系电话', req: true },
          { k: 'adopter_id_card', l: '身份证号' },
          { k: 'adopter_address', l: '住址', full: true },
          { k: 'adopted_at', l: '领养日期', type: 'date' }
        ]
      },
      dead: {
        title: '死亡 / 安乐死登记',
        hint: '安乐死与「死亡 / 无害化」是两种处置方式，台账分开记。',
        chips: { k: 'kind', l: '处置方式', opts: [['euthanasia', '安乐死'], ['dead', '死亡 / 无害化']] },
        fields: [
          { k: 'reason', l: '原因', req: true, full: true },
          { k: 'condition', l: '动物状况', full: true },
          { k: 'euthanized_at', l: '处置日期', type: 'date' }
        ]
      },
      owner: {
        title: '主人领回',
        hint: '核对主人身份与联系方式后办理；领回后动物状态变为「已领回」。',
        fields: [
          { k: 'owner_name', l: '主人姓名', req: true },
          { k: 'owner_phone', l: '联系电话' },
          { k: 'owner_id_card', l: '身份证号' },
          { k: 'owner_address', l: '住址', full: true },
          { k: 'reason', l: '领回原因', full: true },
          { k: 'return_time', l: '领回时间', type: 'date' },
          { k: 'signature', l: '主人签字', type: 'sig', req: true }
        ]
      },
      release: {
        title: '开运回单（放归反向物流）',
        hint: '放归先由医院把动物运回捕捉点：本单提交后进入「待确认」，'
          + '捕捉点填写接收单并签字后，动物进入捕捉点的「待放归」列表。'
          + '接收捕捉点可选市级，或本院所在区县的捕捉点。',
        fields: [
          { k: 'shelter_id', l: '接收捕捉点', req: true, type: 'select',
            options: shelterOptions, selected: pet.shelter_id },
          { k: 'note', l: '备注（交接说明）', full: true }
        ]
      }
    }[kind];
    if (!DEF) return;

    const chipsHTML = DEF.chips
      ? '<div class="dsp-fld full"><label>' + DEF.chips.l + '<span class="req">*</span></label>'
        + '<div class="dsp-chips" data-f="' + DEF.chips.k + '">'
        + DEF.chips.opts.map(([v, t], i) =>
          '<span class="' + (i === 0 ? 'on' : '') + '" data-v="' + v + '">' + t + '</span>').join('')
        + '</div></div>'
      : '';
    const fieldsHTML = DEF.fields.map(f => {
      const label = '<label>' + f.l + (f.req ? '<span class="req">*</span>' : '') + '</label>';
      let control;
      if (f.type === 'sig') {
        control = '<div class="sig-field" data-sig="' + f.k + '"></div>';
      } else if (f.type === 'select') {
        control = '<select class="form-select" data-f="' + f.k + '">'
          + (f.options || []).map(o => '<option value="' + o.value + '"'
            + (String(o.value) === String(f.selected) ? ' selected' : '') + '>'
            + TNR_UI.escape(o.label) + '</option>').join('')
          + '</select>';
      } else if (f.type === 'textarea') {
        control = '<textarea class="form-textarea" data-f="' + f.k + '" rows="2"></textarea>';
      } else {
        control = '<input class="form-input" type="' + (f.type === 'date' ? 'date' : 'text')
          + '" data-f="' + f.k + '">';
      }
      return '<div class="dsp-fld' + (f.full || f.type === 'sig' || f.type === 'textarea' ? ' full' : '')
        + '">' + label + control + '</div>';
    }).join('');

    TNR_UI.modal({
      title: DEF.title,
      size: 'lg',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="tx-tip inf"><span>ℹ</span><div>' + DEF.hint + '</div></div>'
          + '<div class="dsp-form">' + chipsHTML + fieldsHTML + '</div>'
          + '<div class="dsp-actions">'
          + '<button type="button" class="btn btn-secondary" data-close>取消</button>'
          + '<button type="button" class="btn btn-primary" id="dspSubmit">确认办理</button>'
          + '</div>';
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelectorAll('.dsp-chips span').forEach(sp => {
          sp.onclick = () => {
            sp.parentElement.querySelectorAll('span').forEach(x => x.classList.remove('on'));
            sp.classList.add('on');
          };
        });
        const sigPads = {};
        bodyEl.querySelectorAll('[data-sig]').forEach(el => {
          sigPads[el.dataset.sig] = TNR_UI.initSignatureField(el, {title: '手写签名'});
        });
        bodyEl.querySelector('#dspSubmit').onclick = () => {
          const payload = {};
          bodyEl.querySelectorAll('[data-f]').forEach(el => {
            payload[el.dataset.f] = (el.value || '').trim();
          });
          Object.keys(sigPads).forEach(k => { payload[k] = sigPads[k].getDataURL() || ''; });
          const chip = bodyEl.querySelector('.dsp-chips span.on');
          if (chip) payload[chip.parentElement.dataset.f] = chip.dataset.v;
          for (const f of (DEF.fields || [])) {
            if (f.req && !payload[f.k]) {
              TNR_UI.toast('请填写「' + f.l + '」', 'warning');
              return;
            }
          }
          closeFn();
          this.submitDischarge(kind, petId, payload);
        };
      }
    });
  },

  async submitDischarge(kind, petId, payload) {
    const body = Object.assign({ pet_id: Number(petId) }, payload);
    try {
      let res;
      if (kind === 'adopt') {
        res = await TNR_API.registerAdoption(body);
        TNR_UI.assertOk(res, '登记领养预约失败');
        // 不再当场「确认领出」：领养完成必须走带交接人、签字、照片的
        // 领养出库单，否则领养台账里没有任何交接凭证。
      } else if (kind === 'dead') {
        res = await TNR_API.createEuthanasia(body);
        TNR_UI.assertOk(res, '登记失败');
      } else if (kind === 'owner') {
        res = await TNR_API.createOwnerReturn(body);
        TNR_UI.assertOk(res, '办理领回失败');
      } else {
        res = await TNR_API.createReturnOrder({
          pet_ids: [Number(petId)],
          note: payload.note || '',
          shelter_id: Number(payload.shelter_id) || undefined
        });
        TNR_UI.assertOk(res, '开运回单失败');
      }

      // 这里只开单，不办结诊疗单：领养 / 死亡 / 放归还差最后一步
      // （领养出库 / 死亡出库 / 捕捉点签收），要等那一步完成才算出院。
      TNR_UI.toast('单据已创建，请继续办理出库 / 签收', 'success');
      /* 刷新**当前所在的页面**：同一个表单既能从诊疗看板（待出院行）打开，
         也能从出院管理页打开。写死 `render_treatment()` 会让出院页上的
         操作完成后跳到诊疗页 —— 用户点了「办理领养」，结果被弹回诊疗看板。 */
      if (this.state.currentPage === 'discharge') {
        this.render_discharge();
      } else if (this.state.treatmentFlow) {
        this.state.treatmentFlow = null;
        this.renderTreatmentFlow();
      } else {
        this.render_treatment();
      }
    } catch (e) {
      TNR_UI.toast(e.message || '办理失败', 'danger');
    }
  },

  /* ==================== 三步流程 ==================== */
  /* 点看板动物进流程。
     `treatmentId` 有值时直接开**那张**单（看板行自带 tid，已办结的单也能查看）；
     没有（孤儿宠物，尚未开单）才走 `openTreatment()` 现场新建。 */
  async openTreatmentFlow(petId, treatmentId, step) {
    const c = document.getElementById('treatmentContent');
    if (c) c.innerHTML = '<div class="loading-state">正在进入诊疗流程...</div>';
    try {
      let detail;
      if (treatmentId) {
        // ⚠ `getData` 已经解包（返回 `data.data`），失败会 throw —— 不能再查
        // `res.success`，那永远是 undefined，会把正常响应误判成失败。
        detail = await TNR_API.getData('/api/business/treatments/' + treatmentId + '/');
      } else {
        const res = await TNR_API.openTreatment(petId);
        // `_post` 失败**不抛异常**（业务错误是 200 + success:false），
        // 不过一遍 assertOk 的话 catch 是死代码，界面会谎报成功。
        TNR_UI.assertOk(res, '进入诊疗流程失败');
        detail = res.data;
      }
      // `step` = 从列表行「检查 / 诊断 / 治疗」直接落到对应步骤；
      // 不传时按 stage 派生（已出院 → 1，只读查看）。
      this.startTreatmentFlow(detail, step);
    } catch (e) {
      TNR_UI.toast(e.message || '进入诊疗流程失败', 'danger');
      this.state.treatmentFlow = null;
      this.renderTreatmentFlow();
    }
  },

  /* 用服务端返回的诊疗单详情重建本地草稿。
     `stepOverride` 用于「保存后停在原步骤」/「提交后前进到下一步」；
     不传时按 `stage` 派生（checking→1 / treating→2 / done→3）。 */
  startTreatmentFlow(detail, stepOverride) {
    if (!detail) return;
    const pet = detail.pet || {};
    const draft = {
      species: pet.species || '', gender: pet.gender || '',
      breed: pet.breed || '', color: pet.color || '', weight: pet.weight || '',
      temperature: detail.temperature === null || detail.temperature === undefined
        ? '' : String(detail.temperature),
      temperatureFlag: detail.temperature_flag || '',
      commonFindings: (detail.common_findings_list || []).slice(),
      commonOther: detail.common_other || '',
      commonOtherOn: !!detail.common_other,
      infectious: Object.assign({}, detail.infectious || {}),
      skinMucosa: detail.skin_mucosa || '',
      skinOther: detail.skin_other || '',
      checkNote: detail.check_note || '',
      advice: detail.advice || '',
      // 「诊疗完成」只落 `disposal_advised`（建议），最终去向 `disposal` 要等出院单据。
      // 重开这张单时若只读 `disposal`，已点过完成的那一步会显示成「什么都没选」，
      // 医生得重新选一遍 —— 所以回落到建议值。已办结时 `disposal` 必然非空，优先它。
      disposal: detail.disposal || detail.disposal_advised || '',
      // 绝育手术病历（第四十七轮并入）。六个旧口径列由 `detail` 原样带回，
      // 前端只用一套键名（与服务端 `STERILIZATION_FIELDS` 对齐）。
      sterilization: {
        surgery_date: detail.sterilization_surgery_date || '',
        surgeon: detail.sterilization_surgeon || '',
        anesthesia: detail.sterilization_anesthesia || '',
        diagnosis: detail.sterilization_diagnosis || '',
        procedure: detail.sterilization_procedure || '',
        recovery: detail.sterilization_recovery || ''
      },
      items: {}
    };
    const byKey = {};
    (detail.items || []).forEach(it => { byKey[it.item_key] = it; });
    ((detail.options || {}).items || []).forEach(meta => {
      const it = byKey[meta.key];
      draft.items[meta.key] = {
        id: it ? it.id : null,
        plannedDate: it ? (it.planned_date || '') : '',
        chipNo: it ? (it.chip_no || '') : '',
        notes: it ? (it.notes || '') : '',
        rxOther: it ? (it.rx_other || '') : '',
        vetName: it ? (it.vet_name || '') : (detail.vet_name || ''),
        vetLicense: it ? (it.vet_license || '') : (detail.vet_license || ''),
        isDone: it ? !!it.is_done : false,
        doneAt: it ? (it.done_at || '') : '',
        isMust: it ? !!it.is_must : false,
        rx: it ? (it.rx || []).map(r => r.material_id) : []
      };
    });
    // 折叠态：默认全折叠；已填过内容的自动展开，便于回看
    const open = {};
    Object.keys(draft.items).forEach(k => {
      const p = draft.items[k];
      if (p.rx.length || String(p.rxOther).trim() || String(p.chipNo).trim()) open[k] = true;
    });

    const derived = detail.stage === 'done' ? 3 : (detail.stage === 'treating' ? 2 : 1);
    const step = stepOverride || derived;
    this.state.treatmentFlow = {
      detail: detail, draft: draft, step: step,
      maxStep: Math.max(derived, step), open: open
    };
    this.renderTreatmentContent();
  },

  renderFlowView() {
    const c = document.getElementById('treatmentContent');
    if (!c) return;
    const f = this.state.treatmentFlow;
    if (!f) { this.renderTreatmentFlow(); return; }
    const detail = f.detail || {};
    const pet = detail.pet || {};
    const STEPS = ['诊疗前检查', '医嘱与处方', '执行与办结'];
    const attr = [pet.species, pet.gender, pet.breed, pet.color]
      .filter(Boolean).map(x => this.escape(x)).join(' · ');

    let html = '<div class="tx-flow-head">'
      + '<button class="btn btn-sm btn-secondary" data-act="tx-back">‹ 返回看板</button>'
      + '<span class="who">' + this.escape(detail.pet_code || '') + ' · '
      + this.escape(pet.name || '未命名') + '</span>'
      + '<span class="meta">' + attr + '</span>'
      + TNR_UI.printDocBtn('treatment', detail.id, '打印诊疗单')
      + '</div>';

    // 已办结 = 只读查看：办结时间与去向是这张单的历史事实，改不得。
    // 提示条放在头部下面、步骤条上面，避免和步骤条视觉打架。
    if (detail.is_disposed) {
      const dspList = (detail.options || {}).disposals || [];
      const dspSel = dspList.find(x => x.key === detail.disposal) || {};
      html += '<div class="tx-tip suc"><span>✅</span><div>'
        + '<b>该诊疗单已办结，仅可查看、不可编辑。</b>'
        + (detail.disposed_at ? '办结时间 ' + this.escape(TNR_UI.formatDate(detail.disposed_at)) + '，' : '')
        + '去向：' + this.escape(dspSel.label || detail.disposal || '—') + '。</div></div>';
    }

    html += '<div class="tx-steps">';
    STEPS.forEach((s, i) => {
      const n = i + 1;
      let cls = n === f.step ? 'cur' : (n < f.step ? 'done' : '');
      if (n > f.maxStep) cls += ' lock';
      html += '<div class="tx-step ' + cls + '" data-act="tx-step" data-v="' + n + '">'
        + '<div class="dot">' + (n < f.step ? '✓' : n) + '</div>'
        + '<div class="lb">' + s + '</div></div>';
    });
    html += '</div>';

    html += '<div>' + (f.step === 1 ? this.step1HTML()
      : (f.step === 2 ? this.step2HTML() : this.step3HTML())) + '</div>';
    html += '<div class="tx-bottom-bar">' + this.bottomBarHTML() + '</div>';
    c.innerHTML = html;

    const scrollBox = document.getElementById('adminContent');
    if (scrollBox) scrollBox.scrollTop = 0;
    window.scrollTo(0, 0);
  },

  /* ---------- 步骤 1：诊疗前检查 ---------- */
  step1HTML() {
    const f = this.state.treatmentFlow;
    const d = f.draft;
    const detail = f.detail || {};
    const pet = detail.pet || {};
    const opts = detail.options || {};
    const tests = opts.infectious_tests || [];
    const results = opts.test_results || [];
    const intake = detail.intake || {};
    // 只读的两种情况（第四十八轮）：
    //   ① 已办结 —— 办结时间与去向是历史事实，改不得；
    //   ② 检查单**已提交** —— 返回第 1 步只能看，不能改。
    //      检查结果是医嘱与诊断的依据，事后静默改写会让「医嘱基于什么
    //      体检结论下的」失去凭据。服务端 `apply_check()` 同样拒绝二次修改。
    const locked = !!detail.is_disposed || !!detail.checked_at;
    const dis = locked ? ' disabled' : '';

    let h = '<div class="card mb-5"><div class="card-header">'
      + '<div class="card-title"><span class="card-title-bar"></span>基础信息</div>'
      + '<div class="text-muted fs-sm">转运档案带出 · 可修改</div></div><div class="card-body">';

    h += '<div class="tx-row3">'
      + '<div class="tx-fld"><label>种类<span class="req">*</span></label>'
      + '<select class="tx-sel" data-inp="species"' + dis + '>'
      + ['猫', '狗'].map(s => '<option' + (d.species === s ? ' selected' : '') + '>' + s + '</option>').join('')
      + '</select></div>'
      + '<div class="tx-fld"><label>公母</label><select class="tx-sel" data-inp="gender"' + dis + '>'
      + ['公', '母'].map(s => '<option' + (d.gender === s ? ' selected' : '') + '>' + s + '</option>').join('')
      + '</select></div>'
      + '<div class="tx-fld"><label>品种</label><input class="tx-inp" data-inp="breed" value="'
      + this.escape(d.breed) + '"' + dis + '></div>'
      + '</div>';

    h += '<div class="tx-row2">'
      + '<div class="tx-fld"><label>体重（kg）</label><div class="tx-unit">'
      + '<input class="tx-inp" data-inp="weight" value="' + this.escape(d.weight) + '"' + dis + '>'
      + '<span class="u">kg</span></div></div>'
      + '<div class="tx-fld"><label>毛色</label><input class="tx-inp" data-inp="color" value="'
      + this.escape(d.color) + '"' + dis + '></div>'
      + '</div>';

    h += '<div class="tx-kv" style="margin-top:6px;">'
      + '<div><div class="k">档案编号</div><div class="v">' + this.escape(detail.pet_code || '—') + '</div></div>'
      + '<div><div class="k">来源捕捉点</div><div class="v">' + this.escape(intake.shelter || '—') + '</div></div>'
      + '<div><div class="k">转运单号</div><div class="v">' + this.escape(intake.transfer_no || '—') + '</div></div>'
      + '<div><div class="k">接收日期</div><div class="v">' + this.formatDate(intake.receive_date) + '</div></div>'
      + '</div>';

    if (!locked) {
      h += '<div class="tx-tip wrn"><span>✎</span><div>以上为动物基础属性，'
        + '修改后写回档案并留痕（记录操作人与时间）。</div></div>';
    } else if (detail.checked_at && !detail.is_disposed) {
      h += '<div class="tx-tip suc"><span>✓</span><div><b>检查单已提交</b>，'
        + '本步内容仅可查看、不可修改（提交时间 '
        + this.formatDateTime(detail.checked_at) + '）。</div></div>';
    }
    h += '</div></div>';

    /* 体检项目 */
    h += '<div class="card mb-5"><div class="card-header">'
      + '<div class="card-title"><span class="card-title-bar"></span>体检项目</div>'
      + '<div class="text-muted fs-sm">带 * 为提交必填</div></div><div class="card-body">';

    h += '<div class="tx-fld"><label>1. 体温检测<span class="req">*</span></label>'
      + '<div class="tx-chips">'
      + '<span class="tx-chip ' + (d.temperatureFlag === 'normal' ? 'active ok' : '')
      + '" data-act="tx-temp" data-v="normal" data-d="38.5">正常 38.5℃</span>'
      + '<span class="tx-chip ' + (d.temperatureFlag === 'fever' ? 'active warn' : '')
      + '" data-act="tx-temp" data-v="fever" data-d="40.5">发烧 40.5℃</span>'
      + '</div>'
      + '<div class="tx-unit" style="margin-top:9px;max-width:200px;">'
      + '<input class="tx-inp" data-inp="temperature" value="' + this.escape(d.temperature)
      + '" placeholder="点上方快速填入，或直接输入数值"' + dis + '><span class="u">℃</span></div>'
      // 手改数值会自动刷新判定（`syncTempFlag`），把规则提前亮出来，
      // 医生才不会困惑「chip 怎么自己亮了」。阈值来自 `check_options()`。
      + '<div class="text-muted fs-sm" style="margin-top:5px;">手改数值后，判定按 ≥'
      + this.escape(opts.fever_threshold) + '℃ 自动刷新</div></div>';

    const common = opts.common_findings || [];
    h += '<div class="tx-fld"><label>2. 常见病检查</label><div class="tx-chips">'
      + common.map(v => '<span class="tx-chip ' + (d.commonFindings.indexOf(v) >= 0 ? 'active' : '')
        + '" data-act="tx-common" data-v="' + this.escape(v) + '">' + this.escape(v) + '</span>').join('')
      + '<span class="tx-chip ' + (d.commonOtherOn ? 'active' : '')
      + '" data-act="tx-common-other">其他</span>'
      + '</div>'
      + '<input class="tx-inp" id="txCommonOther" data-inp="commonOther" style="margin-top:9px;'
      + (d.commonOtherOn ? '' : 'display:none;') + '"'
      + ' placeholder="其他异常，请填写" value="' + this.escape(d.commonOther) + '"' + dis + '></div>';

    h += '<div class="tx-fld"><label>3. 传染病检查 '
      + '<span class="text-light" style="font-weight:400;">（按种类：' + this.escape(d.species || '未指定') + '）</span></label>';
    if (!tests.length) {
      h += '<div class="tx-rx-empty">该种类没有预置传染病快检项</div>';
    } else {
      h += '<div style="max-width:560px;">';
      tests.forEach(t => {
        const v = d.infectious[t.key] || 'none';
        h += '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:7px 0;border-bottom:1px solid var(--border-faint);">'
          + '<span style="flex:1;min-width:110px;font-size:var(--fs-base);">' + this.escape(t.label) + '</span>'
          + '<span class="tx-seg">'
          + results.map(r => '<button class="' + (v === r.key ? 'on-' + r.key : '')
            + '" data-act="tx-test" data-v="' + t.key + '" data-d="' + r.key + '">'
            + this.escape(r.label) + '</button>').join('')
          + '</span></div>';
      });
      h += '</div>';
    }
    h += '</div>';

    const skin = opts.skin_mucosa || [];
    h += '<div class="tx-fld"><label>4. 皮肤黏膜检查</label><div class="tx-chips">'
      + skin.map(v => '<span class="tx-chip ' + (d.skinMucosa === v ? 'active' : '')
        + '" data-act="tx-skin" data-v="' + this.escape(v) + '">' + this.escape(v) + '</span>').join('')
      + '<span class="tx-chip ' + (d.skinMucosa === '其他' ? 'active' : '')
      + '" data-act="tx-skin" data-v="其他">其他</span>'
      + '</div>'
      + '<input class="tx-inp" id="txSkinOther" data-inp="skinOther" style="margin-top:9px;'
      + (d.skinMucosa === '其他' ? '' : 'display:none;') + '"'
      + ' placeholder="其他情况，请填写" value="' + this.escape(d.skinOther) + '"' + dis + '></div>';

    h += '<div class="tx-fld"><label>5. 其它</label>'
      + '<textarea class="tx-tar" data-inp="checkNote" placeholder="其它需要记录的情况"' + dis + '>'
      + this.escape(d.checkNote) + '</textarea></div>';

    h += '</div></div>';

    /* 检查单状态（服务端时间戳，只读回显） */
    h += '<div class="card mb-5"><div class="card-header">'
      + '<div class="card-title"><span class="card-title-bar"></span>检查单记录</div></div><div class="card-body">'
      + '<div class="tx-kv">'
      + '<div><div class="k">创建时间</div><div class="v">' + this.formatDateTime(detail.created_at) + '</div></div>'
      + '<div><div class="k">单据号</div><div class="v">' + this.escape(detail.ledger_no || '—') + '</div></div>'
      + '<div><div class="k">检查提交</div><div class="v">' + this.formatDateTime(detail.checked_at) + '</div></div>'
      + '<div><div class="k">诊断完成</div><div class="v">' + this.formatDateTime(detail.diagnosed_at) + '</div></div>'
      + '<div><div class="k">当前阶段</div><div class="v">' + this.txStageText(detail.stage) + '</div></div>'
      + '<div><div class="k">状态</div><div class="v">'
      + (detail.checked_at ? '<span class="badge badge-success">已提交</span>'
                           : '<span class="badge badge-warning">草稿</span>') + '</div></div>'
      + '</div></div></div>';
    return h;
  },

  txStageText(stage) {
    // 三态（`Treatment.stage`，后端 `STAGE_CHOICES` 的 label）。看板页签用五态，
    // 详情抽屉的「当前阶段」用三态 —— 两张表都在 `TNR_UI`，这里不再自写一份。
    return TNR_UI.treatmentStageText(stage);
  },

  /* ---------- 步骤 2：医嘱与处方 ---------- */
  step2HTML() {
    const f = this.state.treatmentFlow;
    const detail = f.detail || {};
    const opts = detail.options || {};
    const items = opts.items || [];
    const tpl = opts.advice_templates || [];
    const diagnosed = !!detail.diagnosed_at;
    let h = '';

    if (diagnosed) {
      h += '<div class="tx-tip suc"><span>✓</span><div>诊断已完成，本单已进入执行阶段。'
        + '如需修改医嘱 / 处方，请先点底部「撤回诊断」。</div></div>';
    }

    h += '<div class="card mb-5"><div class="card-header">'
      + '<div class="card-title"><span class="card-title-bar"></span>医嘱</div>'
      + '<div class="text-muted fs-sm">文字记录 · 整单</div></div><div class="card-body">'
      + '<textarea class="tx-tar" data-inp="advice" style="min-height:104px;"'
      + (diagnosed ? ' disabled' : '')
      + ' placeholder="填写医嘱，如：术前禁食禁水 12 小时；术后观察精神食欲……">'
      + this.escape(f.draft.advice) + '</textarea>';
    if (!diagnosed && tpl.length) {
      h += '<div class="tx-tpl-row">' + tpl.map((t, i) =>
        '<span class="t" data-act="tx-advice-tpl" data-v="' + i + '">＋ '
        + this.escape(t) + '</span>').join('') + '</div>';
    }
    // 医嘱尾部的操作人：整单医嘱由开单/执行医生负责，只显示一行文字
    // （姓名 + 执业证号），不再放一枚重复的「电子章」。
    const adviceVetName = detail.vet_name || '';
    const adviceVetLicense = detail.vet_license || '';
    h += '<div class="tx-fld" style="margin-top:10px;"><label>操作人</label>'
      + '<input class="tx-inp" value="' + this.escape(adviceVetName)
      + (adviceVetLicense ? '（' + this.escape(adviceVetLicense) + '）' : '')
      + '" readonly></div>';
    h += '</div></div>';

    h += '<div class="tx-sec-head"><span class="bar"></span>处置项目'
      + '<span class="hint">已填写 <b style="color:var(--bamboo-green);" id="txItemCount">'
      + this.txMustItems().length + '</b> / ' + items.length + ' 项 · 点击标题展开</span></div>';
    items.forEach(meta => { h += this.itemCardHTML(meta, diagnosed); });

    h += '<div class="tx-tip wrn"><span>⚠</span><div><b>填写规则：</b>'
      + '展开要做的项目、选好<b>处方</b>即视为<b>必须执行</b>'
      + '（执行阶段不完成则无法「诊疗完成」）；不选处方 = 该项目跳过。'
      + '预计执行时间默认当天，可用「3天后 / 5天后」快捷改。<br>'
      + '<b>绝育手术病历不用在这一步填</b>：这一步只是排手术，主刀、麻醉、手术过程、'
      + '术后恢复要到<b>手术做完之后</b>才存在。到「执行与办结」点「治疗完成」前补录即可'
      + '（不补录就执行不了，术后档案不会漏）。要现在就填也可以，但必须一次填全。</div></div>';
    return h;
  },

  txItemMeta(key) {
    const opts = ((this.state.treatmentFlow || {}).detail || {}).options || {};
    return (opts.items || []).find(m => m.key === key) || null;
  },

  /* 项目三态：blank 未填写 / filled 已填写 / half 待补全（有处方但缺日期）。
     ⚠ `needs_chip` 不能写成 `meta.needs_chip` 直接参与布尔运算 —— 缺该属性的
     项目会得到 `undefined`，使 `hasRx` 变成 `undefined`、`half` 恒为真，
     把「诊断完成」永久拦死。 */
  txItemState(key) {
    const f = this.state.treatmentFlow;
    const item = (f && f.draft.items[key]) || { rx: [] };
    const meta = this.txItemMeta(key) || {};
    const hasDate = !!item.plannedDate;
    const hasRx = item.rx.length > 0 || !!String(item.rxOther).trim()
      || (!!meta.needs_chip && !!String(item.chipNo).trim());
    /* 病历类项目（绝育）没有处方，「这台手术要做」有两种表达（第五十一轮）：
         ① 排了预计执行时间 —— 计划阶段的标准用法，病历要等术后才写得出来；
         ② 病历草稿填全了 —— 旧口径，留给 admin / 历史导入的半成品。
       服务端 `_replace_items()` 的 `has_record` 就是这同一个或运算，
       两边不一致的后果是：界面显示「0 项必执行」，点「诊疗完成」却被
       服务端拦住（或反过来，单子办结了却没有任何手术记录）。 */
    const rec = (f && f.draft.sterilization) || {};
    const total = this.STER_REC_FIELDS.length;
    const recFilled = this.STER_REC_FIELDS.filter(k =>
      !!String(rec[k] || '').trim()).length;
    const isRec = !!meta.needs_record;
    /* 「填了没有」必须用服务端 `sterilization_draft_filled()` 的同一份判据
       （麻醉方式由下拉默认带出，医生没动过它也算非空）：一边算必执行、
       另一边不算的话，界面写「0 项必执行」、点「诊疗完成」却被服务端拦下。 */
    const recAny = this.STER_DRAFT_FIELDS.some(k => !!String(rec[k] || '').trim());
    const must = isRec ? (hasDate || recAny) : hasRx;
    /* 病历此刻还没到该填的时候，所以「没日期」对它是合法态而非待补全；
       真正的「必须填全」闸门在执行绝育那一刻（`sterRecordMissing()`）。 */
    const state = must ? ((hasDate || isRec) ? 'filled' : 'half') : 'blank';
    return { hasDate: hasDate, hasRx: hasRx, must: must, isRec: isRec,
             half: state === 'half', state: state,
             recFilled: recFilled, recTotal: total };
  },

  /* 病历六栏的键名（与服务端 `STERILIZATION_FIELDS` 逐字对齐）。
     只有这一份：卡片摘要、弹窗、执行前闸门都读它。
     `STER_DRAFT_FIELDS` 单独列出「算不算填过」的判据，理由见 `txItemState()`。 */
  STER_REC_FIELDS: ['surgery_date', 'surgeon', 'anesthesia',
                    'diagnosis', 'procedure', 'recovery'],
  STER_DRAFT_FIELDS: ['surgery_date', 'surgeon', 'diagnosis',
                      'procedure', 'recovery'],
  STER_REC_LABELS: { surgery_date: '手术日期', surgeon: '主刀医生',
                     anesthesia: '麻醉方式', diagnosis: '诊断',
                     procedure: '手术过程', recovery: '恢复情况' },

  /* **库里**的病历还缺哪几栏（不是草稿 —— 服务端 `execute_item()` 查的是库）。
     `detail.sterilization_*` 六列由 `_treatment_detail_data()` 原样带回，
     每次执行/撤销/补录后都重新 `startTreatmentFlow()`，所以它与库同步。 */
  sterRecordMissing() {
    const detail = (this.state.treatmentFlow || {}).detail || {};
    const cols = { surgery_date: 'sterilization_surgery_date',
                   surgeon: 'sterilization_surgeon',
                   anesthesia: 'sterilization_anesthesia',
                   diagnosis: 'sterilization_diagnosis',
                   procedure: 'sterilization_procedure',
                   recovery: 'sterilization_recovery' };
    return this.STER_REC_FIELDS
      .filter(k => !String(detail[cols[k]] || '').trim())
      .map(k => this.STER_REC_LABELS[k]);
  },

  txMustItems() {
    const f = this.state.treatmentFlow;
    if (!f) return [];
    return Object.keys(f.draft.items).filter(k => this.txItemState(k).must);
  },

  itemCardHTML(meta, locked) {
    const f = this.state.treatmentFlow;
    const item = f.draft.items[meta.key] || { rx: [] };
    const st = this.txItemState(meta.key);
    const open = !!f.open[meta.key];
    const badge = st.state === 'filled' ? '<span class="badge badge-success">已填写</span>'
      : (st.state === 'half' ? '<span class="badge badge-danger">待补全</span>'
        : '<span class="badge badge-default">未填写</span>');
    const dis = locked ? ' disabled' : '';

    let h = '<div class="tx-item-card ' + st.state + ' ' + (open ? 'open' : 'collapsed')
      + '" data-item="' + meta.key + '">'
      + '<div class="tx-item-h" data-act="tx-item-toggle" data-v="' + meta.key + '">'
      + '<span class="ic">' + this.escape(meta.icon || '') + '</span>'
      + '<div class="nm">' + this.escape(meta.name) + '<small>'
      + this.escape(meta.hint || '') + '</small></div>'
      + badge + '<span class="arr">›</span></div>'
      + '<div class="tx-item-b">';

    h += '<div class="tx-fld"><label>预计执行时间</label><div class="tx-date-row">'
      + '<input type="date" class="tx-inp" data-plan="' + meta.key + '" data-f="plannedDate" value="'
      + this.escape(item.plannedDate) + '"' + dis + '>';
    if (!locked) {
      h += [['今天', 0], ['3天后', 3], ['5天后', 5], ['7天后', 7]].map(q =>
        '<span class="tx-chip qk" data-act="tx-date-quick" data-v="' + meta.key
        + '" data-d="' + q[1] + '">' + q[0] + '</span>').join('');
    }
    h += '</div></div>';

    /* 绝育手术病历。第五十一轮把「必须填全」的时机从这一步挪到执行那一刻：
       计划阶段可以整体留空（手术还没做），填了才要求六栏齐全；已诊断后这张
       卡片就是「术后补录」入口（服务端 `execute_item()` 会硬拦）。
       空病历时按钮写着「填写」，填过显示日期+医生摘要。`locked` 只禁输入框，
       病历按钮不受它限制 —— 否则「手术做完了也没法把病历记进系统」。 */
    if (meta.needs_record) {
      const rec = f.draft.sterilization || {};
      const total = this.STER_REC_FIELDS.length;
      const filled = this.STER_REC_FIELDS.filter(k =>
        !!String(rec[k] || '').trim()).length;
      const full = filled === total;
      const sum = full || filled > 0
        ? ((rec.surgery_date ? this.escape(rec.surgery_date) + ' · ' : '（无日期）· ')
            + this.escape(rec.surgeon || '主刀未填')
            + (rec.anesthesia ? ' · ' + this.escape(rec.anesthesia) : ''))
        : (locked ? '尚未补录手术病历' : '尚未填写手术病历');
      h += '<div class="tx-fld"><label>手术病历'
        + (filled > 0 && !full
            ? ' <span class="badge badge-danger">' + filled + '/' + total + '</span>' : '')
        + '</label>'
        + '<div class="tx-rec-row">'
        + '<span class="tx-rec-sum' + (full ? ' on' : '') + '">' + sum + '</span>'
        + '<button class="btn btn-sm ' + (full ? 'btn-secondary' : 'btn-primary')
        + '" data-act="tx-record-open" data-v="' + meta.key + '">'
        + (full ? '查看 / 修改病历'
            : (locked ? '补录手术病历' : '填写手术病历')) + '</button>'
        + '</div>'
        + (locked && !full
            ? '<div class="text-muted fs-sm" style="margin-top:4px;">'
              + '手术已做完的话，请在这里把病历记进系统 —— '
              + '「治疗完成」会先校验病历，缺栏会被拒绝。</div>'
            : (!locked
                ? '<div class="text-muted fs-sm" style="margin-top:4px;">'
                  + '可先空着：这一步只排手术，主刀 / 麻醉 / 过程 / 恢复术后补录。</div>'
                : ''))
        + '</div>';
    }

    if (meta.needs_chip) {
      h += '<div class="tx-fld"><label>芯片号</label>'
        + '<div style="display:flex;gap:var(--sp-2);">'
        + '<input class="tx-inp" style="flex:1;" data-plan="' + meta.key + '" data-f="chipNo" value="'
        + this.escape(item.chipNo) + '" placeholder="扫描或输入芯片号"' + dis + '>'
        + (locked ? '' : '<button class="btn btn-sm btn-secondary" data-act="tx-chip-open" data-v="'
          + meta.key + '">选择</button>')
        + '</div></div>';
    }

    h += '<div class="tx-fld"><label>处方 <span class="auto">库存用药 · 固定 1 头份</span></label>';
    if (!locked) {
      h += '<button class="btn btn-sm btn-secondary" data-act="tx-med-open" data-v="'
        + meta.key + '">＋ 从库存药品中选择</button>';
    }
    h += '<div class="tx-rx-list">' + this.rxListHTML(meta.key) + '</div>';
    if (!locked) {
      h += '<input class="tx-inp" style="margin-top:8px;" data-plan="' + meta.key
        + '" data-f="rxOther" placeholder="其他药品及数量（手写，仅记录、不统计库存）" value="'
        + this.escape(item.rxOther) + '">';
    } else if (item.rxOther) {
      h += '<div class="tx-rx-empty" style="text-align:left;">手写：'
        + this.escape(item.rxOther) + '</div>';
    }
    h += '</div>';

    const noteTpl = meta.note_tpl || [];
    h += '<div class="tx-fld"><label>注意事项</label>'
      + '<input class="tx-inp" data-plan="' + meta.key + '" data-f="notes" value="'
      + this.escape(item.notes) + '" placeholder="常用标签点选，或手写"' + dis + '>';
    if (!locked && noteTpl.length) {
      h += '<div class="tx-tpl-row">' + noteTpl.map((t, i) =>
        '<span class="t" data-act="tx-note-tpl" data-v="' + meta.key + '" data-d="' + i + '">＋ '
        + this.escape(t) + '</span>').join('') + '</div>';
    }
    h += '</div>';

    const detail = f.detail || {};
    const vetName = item.vetName || detail.vet_name || '';
    const vetLicense = item.vetLicense || detail.vet_license || '';
    // 操作人只保留这一行。原先后面还有一条 `tx-sign-strip`（圆形红章 +
    // 姓名/证号），与这行信息完全重复，视觉上像盖了两个章。
    h += '<div class="tx-fld"><label>操作人 <span class="auto">默认当前账号</span></label>'
      + '<input class="tx-inp" value="' + this.escape(vetName)
      + (vetLicense ? '（' + this.escape(vetLicense) + '）' : '') + '" readonly></div>';

    h += '</div></div>';
    return h;
  },

  rxListHTML(key) {
    const f = this.state.treatmentFlow;
    const item = (f && f.draft.items[key]) || { rx: [] };
    if (!item.rx.length) return '<div class="tx-rx-empty">尚未选择药品</div>';
    return item.rx.map((mid, i) => {
      const m = this.txMaterial(mid) || {};
      return '<div class="tx-rx-row">'
        + '<div class="nm">' + this.escape(m.name || '—') + '<small>'
        + this.escape(m.specification || '') + ' · '
        + this.escape(TNR_API.getMaterialCategoryText(m.category)) + '</small></div>'
        + '<span class="qty">1 头份</span>'
        + '<button class="del" data-act="tx-rx-del" data-v="' + key + '" data-d="' + i
        + '">✕</button></div>';
    }).join('');
  },

  txMaterial(id) {
    return (this._txMaterials || []).find(m => String(m.id) === String(id)) || null;
  },

  /* 局部刷新单张卡（不重建 DOM，避免打断正在输入的字段） */
  refreshItemCard(key) {
    const wrap = document.querySelector('#page-treatment .tx-item-card[data-item="' + key + '"]');
    const meta = this.txItemMeta(key);
    if (!wrap || !meta) return;
    const detail = (this.state.treatmentFlow || {}).detail || {};
    const tmp = document.createElement('div');
    tmp.innerHTML = this.itemCardHTML(meta, !!detail.diagnosed_at);
    const fresh = tmp.firstElementChild;
    if (!fresh) return;
    wrap.className = fresh.className;
    wrap.querySelector('.tx-item-h').innerHTML = fresh.querySelector('.tx-item-h').innerHTML;
    wrap.replaceChild(fresh.querySelector('.tx-item-b'), wrap.querySelector('.tx-item-b'));
    this.refreshItemCount();
  },

  /* 只改配色与徽章，连 header 都不重建 —— 用于「边输边存」的输入事件 */
  refreshItemState(key) {
    const wrap = document.querySelector('#page-treatment .tx-item-card[data-item="' + key + '"]');
    if (!wrap) return;
    const st = this.txItemState(key);
    wrap.classList.toggle('filled', st.state === 'filled');
    wrap.classList.toggle('half', st.state === 'half');
    wrap.classList.toggle('blank', st.state === 'blank');
    const badge = wrap.querySelector('.tx-item-h .badge');
    if (badge) {
      badge.className = 'badge ' + (st.state === 'filled' ? 'badge-success'
        : (st.state === 'half' ? 'badge-danger' : 'badge-default'));
      badge.textContent = st.state === 'filled' ? '已填写'
        : (st.state === 'half' ? '待补全' : '未填写');
    }
    this.refreshItemCount();
  },

  refreshItemCount() {
    const cnt = document.getElementById('txItemCount');
    if (cnt) cnt.textContent = this.txMustItems().length;
  },

  /* ---------- 步骤 3：执行与办结 ---------- */
  step3HTML() {
    const f = this.state.treatmentFlow;
    const detail = f.detail || {};
    const opts = detail.options || {};
    const must = this.txMustItems();
    const disposed = !!detail.is_disposed;
    const doneN = must.filter(k => f.draft.items[k].isDone).length;

    let h = '<div class="card mb-5"><div class="card-header">'
      + '<div class="card-title"><span class="card-title-bar"></span>处置执行清单</div>'
      + '<div class="text-muted fs-sm">' + doneN + ' / ' + must.length + ' 已完成</div>'
      + '</div><div class="card-body">';
    if (!must.length) {
      h += '<div class="tx-empty">本单未选择任何处置项目（未填写处方）。<br>'
        + (detail.done_at ? '诊疗已完成，等待出院办理。'
          : '可直接点击下方「诊疗完成」进入办结。')
        + '</div>';
    } else {
      must.forEach(k => {
        const item = f.draft.items[k];
        const meta = this.txItemMeta(k) || {};
        const names = item.rx.map(id => (this.txMaterial(id) || {}).name || '')
          .filter(Boolean).join('、');
        /* 病历类项目（绝育）：手术做完了才允许确认执行，服务端 `execute_item()`
           会先查库里六栏齐不齐。所以缺病历时不给「治疗完成」按钮 ——
           换成「补录病历」，点了就直接进弹窗，省一次必然失败的请求。
           ⚠ 判据取**库里**（`sterRecordMissing()`）而不是前端草稿：
           草稿里写着齐、库里没有的话，放行只会换来一句报错。 */
        const miss = meta.needs_record && !item.isDone && !disposed
          ? this.sterRecordMissing() : null;
        h += '<div class="tx-exec-row ' + (item.isDone ? 'done' : '') + '">'
          + '<span class="ic">' + this.escape(meta.icon || '') + '</span>'
          + '<div class="info"><div class="nm">' + this.escape(meta.name || k) + ' '
          + (item.isDone ? '<span class="badge badge-success">已完成</span>'
                         : '<span class="badge badge-warning">待执行</span>') + '</div>'
          + '<div class="sub">处方：<b>' + this.escape(names || item.rxOther || '—') + '</b>'
          + ((item.rxOther && names) ? '，手写：' + this.escape(item.rxOther) : '')
          + (item.chipNo ? '，芯片：' + this.escape(item.chipNo) : '') + '</div>'
          + '<div class="sub">预计 ' + this.escape(item.plannedDate || '—')
          + (item.isDone ? ' · 实际 ' + this.formatDateTime(item.doneAt) : '') + '</div>'
          + (meta.needs_record
            ? '<div class="sub">' + (miss && miss.length
                ? '<span class="badge badge-danger">病历缺 ' + miss.length + ' 栏</span> '
                  + this.escape(miss.join('、'))
                : '<span class="badge badge-success">手术病历已录入</span>') + '</div>'
            : '')
          + '</div><div class="act">' + (item.isDone
            ? '<button class="btn btn-sm btn-secondary" data-act="tx-undo" data-v="' + item.id
              + '"' + (disposed ? ' disabled' : '') + '>撤销</button>'
            : (miss && miss.length
              ? '<button class="btn btn-sm btn-primary" data-act="tx-record-open">补录病历</button>'
              : '<button class="btn btn-sm btn-success" data-act="tx-exec" data-v="' + item.id
                + '"' + (disposed ? ' disabled' : '') + '>治疗完成</button>'))
          + (meta.needs_record && !(miss && miss.length)
            ? '<button class="btn btn-sm btn-ghost" data-act="tx-record-open"'
              + ' style="margin-left:6px;">查看病历</button>' : '')
          + '</div></div>';
      });
    }
    h += '</div></div>';
    h += '<div class="tx-tip inf"><span>💊</span><div>点「治疗完成」时<b>扣减该项处方药品的'
      + '本院库存</b>并生成消耗流水；手写药品仅记录、不扣库存。芯片植入同时扣芯片物料'
      + '库存，并把该芯片绑定到本动物档案（芯片永久不可复用）。<br>'
      + '<b>绝育项目要先补录手术病历才能点「治疗完成」</b>：手术记录（主刀 / 麻醉 / '
      + '过程 / 恢复）是病历归档与政府端统计的依据，漏了它就是「做了手术却没有记录」。'
      + '已执行的项目可以「撤销」，病历不会被撤销删除。</div></div>';

    // 诊疗后照片（B2）：办结环节当场留证，落 Pet.photo_treatment —— 领养
    // 大厅与领养资料页的「诊疗后」读的就是同一字段，一张照片两处展示。
    // 走诊疗单独立接口（tx-photo → uploadTreatmentPhoto），**不能**复用
    // 领养 edit-info —— 那会顺带把「放归 / 无害化」动物挂进领养大厅。
    // 已办结只读展示（与检查单的锁定口径一致）。
    const petPhoto = ((detail.pet || {}).photo_treatment) || '';
    h += '<div class="card mb-5" style="margin-top:14px;"><div class="card-header">'
      + '<div class="card-title"><span class="card-title-bar"></span>诊疗后照片</div>'
      + '<div class="text-muted fs-sm">'
      + (disposed ? '已办结 · 仅展示' : '选填 · 点击上传 / 更换') + '</div></div>'
      + '<div class="card-body"><div style="display:flex;gap:var(--sp-4);align-items:flex-start;">'
      + '<div' + (disposed ? '' : ' data-act="tx-photo" title="点击上传 / 更换" style="cursor:pointer;flex:none;width:160px;"')
      + '><div class="photo-placeholder-lg">' + (petPhoto
        ? '<img src="' + this.escape(petPhoto) + '" alt="诊疗后照片" style="width:100%;height:100%;object-fit:cover;" />'
        : '📷') + '</div></div>'
      + '<div class="text-muted fs-sm" style="flex:1;padding-top:4px;">'
      + '诊疗结束后的现场留证（如术后伤口、精神状态），与领养资料「诊疗后」为同一张照片，'
      + '无需重复上传。</div></div></div></div>';

    // 「建议去向」的可改窗口 = 诊疗完成之后、出院办结之前（第五十七轮）。
    // 出院的四个去向完全按它分流，所以「换去向」必须先在这里改，改完再去
    // 出院管理开单。动物一旦被出院单据占用，后端 `set_advice()` 会 400 并
    // 指名要先处理哪张单 —— 占用判据只写在服务端一处，这里只按是否已办结
    // 决定控件形态（原先 `!!detail.done_at` 就把整段锁死，等于没有改口）。
    const adviceLocked = !!detail.is_disposed;
    const adviceLive = !!detail.done_at && !adviceLocked;
    h += '<div class="card mb-5" style="margin-top:14px;"><div class="card-header">'
      + '<div class="card-title"><span class="card-title-bar"></span>建议去向</div>'
      + '<div class="text-muted fs-sm">'
      + (adviceLocked ? '已办结 · 出院单据已确定去向'
        : adviceLive ? '待出院 · 点击即可修改（立即保存）' : '单选 · 随「诊疗完成」一起提交')
      + '</div></div><div class="card-body">';
    if (!detail.diagnosed_at) {
      h += '<div class="tx-empty">请先完成「医嘱与处方」并点击「诊断完成」。</div>';
    } else {
      h += '<div class="tx-disposal-grid">';
      (opts.disposals || []).forEach(dsp => {
        h += '<div class="tx-dsp ' + (f.draft.disposal === dsp.key ? 'active' : '') + '"'
          + (adviceLocked ? '' : ' data-act="tx-disposal"')
          + ' data-v="' + dsp.key + '">'
          + '<span class="ic">' + (TX_DISPOSAL_ICON[dsp.key] || '📌') + '</span>'
          + '<div class="tx"><div class="n">' + this.escape(dsp.label) + '</div>'
          + '<div class="d">' + this.escape(TX_DISPOSAL_DESC[dsp.key] || '') + '</div>'
          + '</div></div>';
      });
      h += '</div>';
      if (f.draft.disposal) {
        const sel = (opts.disposals || []).find(x => x.key === f.draft.disposal) || {};
        h += '<div class="tx-tip suc" style="margin-top:12px;"><span>✓</span><div>'
          + (adviceLocked ? '办结去向：<b>' : '建议去向：<b>')
          + this.escape(sel.label || f.draft.disposal) + '</b>。'
          + (adviceLive
            ? '动物已进入「待出院」，出院管理只会把这只动物列在<b>本建议</b>对应的去向里；'
              + '确实要更换去向，点上面另一个去向即可（立刻保存），再回去院开单。'
            : '动物仍在院，诊疗完成后列表显示「待出院」；只有在<b>出院管理</b>里办完单据'
              + '（领养出库 / 死亡出库 / 运回签收 / 主人领回）才算已出院。')
          + '</div></div>';
      } else if (adviceLive) {
        h += '<div class="tx-tip wrn" style="margin-top:12px;"><span>!</span><div>'
          + '这张单「诊疗完成」时没有记录建议去向，出院管理的<b>四个去向都选不到它</b>。'
          + '请在上面点选实际去向，再回去院办理。</div></div>';
      }
    }
    h += '</div></div>';
    return h;
  },

  bottomBarHTML() {
    const f = this.state.treatmentFlow;
    const detail = f.detail || {};
    if (detail.is_disposed) {
      return '<button class="btn btn-primary" data-act="tx-back">返回看板（本单已办结）</button>';
    }
    if (f.step === 1) {
      // 已提交 → 只剩「下一步」。不给「保存修改」，因为服务端也拒绝改
      // （`apply_check` 见 `checked_at` 即 400）—— 界面留着按钮只会让
      // 用户点一次、吃一个 400，还以为是系统坏了。
      if (detail.checked_at) {
        return '<button class="btn btn-primary" data-act="tx-step" data-v="2">'
          + '下一步：医嘱与处方 ›</button>';
      }
      return '<button class="btn btn-secondary" data-act="tx-check-save">暂存</button>'
        + '<button class="btn btn-primary" data-act="tx-check-submit">提交并下一步</button>';
    }
    if (f.step === 2) {
      if (detail.diagnosed_at) {
        return '<button class="btn btn-ghost" data-act="tx-undiagnose">撤回诊断</button>'
          + '<button class="btn btn-primary" data-act="tx-next3">进入执行与办结 ›</button>';
      }
      return '<button class="btn btn-secondary" data-act="tx-plan-save">暂存</button>'
        + '<button class="btn btn-primary" data-act="tx-diagnose">诊断完成</button>';
    }
    if (!detail.diagnosed_at) {
      return '<button class="btn btn-primary" data-act="tx-step" data-v="2">‹ 回到医嘱与处方</button>';
    }
    const must = this.txMustItems();
    if (!detail.done_at) {
      const left = must.filter(k => !f.draft.items[k].isDone).length;
      const blocked = must.length > 0 && left > 0;
      return '<button class="btn btn-primary" data-act="tx-treat-done"' + (blocked ? ' disabled' : '')
        + '>' + (blocked ? '还有 ' + left + ' 项未完成' : '诊疗完成（转待出院）') + '</button>';
    }
    return '<button class="btn btn-secondary" data-act="tx-back">返回看板</button>'
      + '<button class="btn btn-primary" data-act="tx-discharge">去出院管理</button>';
  },

  /* ==================== 事件分发（全部走委托） ==================== */
  onTreatmentAct(act, el) {
    const f = this.state.treatmentFlow;
    const k = el.dataset.v;
    const i = el.dataset.d;

    if (act === 'ttab') {
      this.state.treatmentTab = k;
      document.querySelectorAll('#page-treatment .sub-tab')
        .forEach(t => t.classList.toggle('active', t.dataset.v === k));
      this.renderTreatmentContent();
      return;
    }
    if (act === 'tx-filter') { this.state.treatmentFilter = k; this.renderBoard(); return; }
    if (act === 'tx-refresh') { this.renderTreatmentFlow(); return; }
    if (act === 'tx-open') { this.openTreatmentFlow(k, el.dataset.tid || ''); return; }
    if (act === 'tx-back') { this.state.treatmentFlow = null; this.renderTreatmentFlow(); return; }
    if (act === 'tx-discharge') {
      this.state.treatmentFlow = null;
      this.navigateTo('discharge');
      return;
    }
    if (!f) return;

    // 检查单已提交 / 已办结 → 第 1 步只读。
    // ⚠ 光把 `<input>` 设 `disabled` 不够：体温 chips、常见病 chips、
    //   传染病分段按钮、皮肤黏膜 chips 都是 `<span>` / `<button>`，
    //   `disabled` 拦不住点击 —— 点了照样改 `draft`，只是保存时被服务端 400。
    const step1Locked = f.step === 1
      && (f.detail.checked_at || f.detail.is_disposed);
    if (step1Locked && ['tx-temp', 'tx-common', 'tx-common-other',
                        'tx-test', 'tx-skin'].indexOf(act) >= 0) {
      return;
    }

    if (act === 'tx-step') {
      const n = Number(k);
      if (n <= f.maxStep) { f.step = n; this.renderFlowView(); }
      return;
    }
    if (act === 'tx-temp') {
      const d = f.draft;
      if (d.temperatureFlag === k) { d.temperatureFlag = ''; d.temperature = ''; }
      else { d.temperatureFlag = k; d.temperature = el.dataset.d; }
      this.renderFlowView();
      return;
    }
    if (act === 'tx-common') {
      const d = f.draft;
      let picked = d.commonFindings.slice();
      if (k === '无异常') {
        // 「无异常」与其它项**互斥** —— 服务端也会拦（`apply_check`），
        // 前端必须同口径，否则用户看到「无异常 + 有跳蚤」同时点亮，
        // 一点保存就被 400 打回来，却不知道哪里错了。
        picked = picked.indexOf('无异常') >= 0 ? [] : ['无异常'];
      } else {
        picked = picked.filter(x => x !== '无异常');
        picked = picked.indexOf(k) >= 0 ? picked.filter(x => x !== k) : picked.concat([k]);
      }
      d.commonFindings = picked;
      this.renderFlowView();
      return;
    }
    if (act === 'tx-common-other') {
      f.draft.commonOtherOn = !f.draft.commonOtherOn;
      if (!f.draft.commonOtherOn) f.draft.commonOther = '';
      this.renderFlowView();
      const box = document.getElementById('txCommonOther');
      if (box && f.draft.commonOtherOn) box.focus();
      return;
    }
    if (act === 'tx-test') { f.draft.infectious[k] = i; this.renderFlowView(); return; }
    if (act === 'tx-skin') {
      f.draft.skinMucosa = f.draft.skinMucosa === k ? '' : k;
      if (f.draft.skinMucosa !== '其他') f.draft.skinOther = '';
      this.renderFlowView();
      const box = document.getElementById('txSkinOther');
      if (box && f.draft.skinMucosa === '其他') box.focus();
      return;
    }
    if (act === 'tx-item-toggle') { f.open[k] = !f.open[k]; this.refreshItemCard(k); return; }
    if (act === 'tx-date-quick') {
      const item = f.draft.items[k];
      if (item) item.plannedDate = this.addDays(i);
      this.refreshItemCard(k);
      return;
    }
    if (act === 'tx-advice-tpl') {
      const t = (((f.detail || {}).options || {}).advice_templates || [])[Number(k)];
      if (t) f.draft.advice = f.draft.advice ? (f.draft.advice.replace(/\s*$/, '') + '\n' + t) : t;
      this.renderFlowView();
      return;
    }
    if (act === 'tx-note-tpl') {
      const meta = this.txItemMeta(k) || {};
      const t = (meta.note_tpl || [])[Number(i)];
      const item = f.draft.items[k];
      if (item && t) item.notes = item.notes ? (item.notes.replace(/\s*$/, '') + '；' + t) : t;
      this.refreshItemCard(k);
      return;
    }
    if (act === 'tx-rx-del') {
      const item = f.draft.items[k];
      if (item) item.rx.splice(Number(i), 1);
      this.refreshItemCard(k);
      return;
    }
    if (act === 'tx-med-open') { this.openMedPicker(k); return; }
    if (act === 'tx-record-open') { this.openSterilizationModal(); return; }
    if (act === 'tx-chip-open') { this.openChipPicker(k); return; }
    if (act === 'tx-check-save') { this.saveTreatmentCheck(true); return; }
    if (act === 'tx-check-submit') { this.saveTreatmentCheck(false); return; }
    if (act === 'tx-plan-save') { this.saveTreatmentPlan(false); return; }
    if (act === 'tx-diagnose') { this.diagnoseTreatment(); return; }
    if (act === 'tx-undiagnose') { this.undiagnoseTreatment(); return; }
    if (act === 'tx-next3') {
      f.step = 3; f.maxStep = Math.max(f.maxStep, 3); this.renderFlowView(); return;
    }
    if (act === 'tx-exec') { this.executeTreatmentItem(k); return; }
    if (act === 'tx-undo') { this.undoTreatmentItem(k); return; }
    if (act === 'tx-treat-done') { this.markTreatmentDone(); return; }
    if (act === 'tx-disposal') {
      const prev = f.draft.disposal;
      f.draft.disposal = k;
      this.renderFlowView();
      // 「诊疗完成」之后点建议 = 直接改库（出院四个去向按它分流），
      // 之前只是给马上要提交的表单选值。
      if (f.detail.done_at && prev !== k) this.txSaveAdvice(k, prev);
      return;
    }
    if (act === 'tx-photo') { this.txUploadPhoto(); return; }
  },

  /* 诊疗后照片上传（B2）：弹层选文件 + 预览（与领养页 uploadPhoto 同款交互），
     但走诊疗单独立接口 —— 落 Pet.photo_treatment 且**不碰**领养展示位。
     成功后用服务端返回的整份 detail 重建流程、停在当前步骤，
     照片 url 回显靠 detail.pet.photo_treatment。 */
  txUploadPhoto() {
    const f = this.state.treatmentFlow;
    if (!f || (f.detail || {}).is_disposed) return;
    TNR_UI.modal({
      title: '上传诊疗后照片',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="form-group"><label class="form-label">选择图片文件</label>'
          + '<input type="file" accept="image/*" id="txPhotoInput" class="form-input" style="padding:4px;" /></div>'
          + '<div id="txPhotoPreview" style="margin-top:var(--sp-3);"></div>'
          + '<div style="display:flex;gap:var(--sp-3);justify-content:flex-end;margin-top:var(--sp-4);">'
          + '<button class="btn btn-secondary" data-close>取消</button>'
          + '<button class="btn btn-primary" id="txPhotoUploadBtn">上传</button></div>';
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelector('#txPhotoInput').addEventListener('change', e => {
          const file = e.target.files[0];
          if (!file) return;
          const reader = new FileReader();
          reader.onload = ev => {
            bodyEl.querySelector('#txPhotoPreview').innerHTML =
              '<img src="' + ev.target.result + '" style="width:100%;border-radius:var(--r-md);" />';
          };
          reader.readAsDataURL(file);
        });
        bodyEl.querySelector('#txPhotoUploadBtn').onclick = async () => {
          const file = bodyEl.querySelector('#txPhotoInput').files[0];
          if (!file) { TNR_UI.toast('请选择图片文件', 'warning'); return; }
          try {
            const res = await TNR_API.uploadTreatmentPhoto(f.detail.id, file);
            TNR_UI.assertOk(res, '照片上传失败');
            closeFn();
            this.startTreatmentFlow(res.data, f.step);
            TNR_UI.toast('诊疗后照片已保存', 'success');
          } catch (e) {
            TNR_UI.toast(e.message || '上传失败', 'danger');
          }
        };
      }
    });
  },

  onTreatmentInput(target) {
    const f = this.state.treatmentFlow;
    if (!f || !target || !target.dataset) return;
    const d = target.dataset;
    if (d.inp) {
      f.draft[d.inp] = target.value;
      // 传染病快检项**按物种**下发，改了种类必须重渲染，否则「猫的 FPV」
      // 会跟着狗的检查单一起提交，被服务端 400 拒绝。
      if (d.inp === 'species') this.renderFlowView();
      // 体温手改后自动刷新判定：点 chip 会自动填数值，但反过来「手改数值」
      // 原本不联动判定 —— 测出 40.2℃ 判定却停在「正常」，会被服务端的
      // 交叉校验 400 打回。阈值从 `check_options()` 下发，与服务端
      // `fever_judge()` 同一条线，前端不另抄一份。
      if (d.inp === 'temperature') this.syncTempFlag(target.value);
      return;
    }
    if (d.plan) {
      const item = f.draft.items[d.plan];
      if (!item) return;
      item[d.f] = target.value;
      this.refreshItemState(d.plan);
    }
  },

  /* 体温数值 → 判定 chip 的实时同步（≥ 阈值判发烧，阈值由 check_options 下发）。
     只直接 toggle chip 的 class，**不整块重渲染** —— renderFlowView 会让
     输入框失焦，用户一次只能输一个字符。清空 / 输入到一半（非数字）时
     不动判定：草稿判定保留，提交前的必填校验与服务端交叉校验兜底。 */
  syncTempFlag(raw) {
    const f = this.state.treatmentFlow;
    if (!f) return;
    const th = ((f.detail || {}).options || {}).fever_threshold;
    if (th === undefined || th === null) return;
    const v = parseFloat(raw);
    if (String(raw).trim() === '' || isNaN(v)) return;
    const flag = v >= Number(th) ? 'fever' : 'normal';
    f.draft.temperatureFlag = flag;
    document.querySelectorAll('#page-treatment [data-act="tx-temp"]').forEach(ch => {
      const on = ch.dataset.v === flag;
      ch.classList.toggle('active', on);
      ch.classList.toggle('ok', on && flag === 'normal');
      ch.classList.toggle('warn', on && flag === 'fever');
    });
  },

  addDays(n) {
    const d = new Date();
    d.setDate(d.getDate() + Number(n || 0));
    const p = (x) => String(x).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  },

  /* ==================== 写操作 ==================== */
  txCheckPayload() {
    const d = this.state.treatmentFlow.draft;
    // `temperature` 一律显式传（空串 = 清空）。不传的话「用户把体温删了」
    // 会保留旧值，而服务端「提交必须填体温」的校验会拿旧值放行 —— 界面空白、
    // 单据却有体温，且不报错。
    return {
      species: d.species, gender: d.gender, breed: d.breed,
      color: d.color, weight: d.weight,
      temperature: String(d.temperature).trim() === '' ? null : d.temperature,
      temperature_flag: d.temperatureFlag,
      common_findings: d.commonFindings,
      common_other: d.commonOther,
      infectious_tests: d.infectious,
      skin_mucosa: d.skinMucosa,
      skin_other: d.skinOther,
      check_note: d.checkNote
    };
  },

  async saveTreatmentCheck(isDraft) {
    const f = this.state.treatmentFlow;
    if (!f) return;
    const d = f.draft;
    if (!isDraft) {
      if (String(d.temperature).trim() === '') {
        TNR_UI.toast('请先完成「体温检测」再提交', 'warning'); return;
      }
      if (!d.temperatureFlag) {
        TNR_UI.toast('请选择体温判定（正常 / 发烧）再提交', 'warning'); return;
      }
    }
    try {
      const payload = this.txCheckPayload();
      payload.draft = !!isDraft;
      const res = await TNR_API.treatmentCheck(f.detail.id, payload);
      TNR_UI.assertOk(res, '检查单保存失败');
      // 暂存停在原步骤（提交才前进），否则「点了暂存却被弹到第二步」。
      this.startTreatmentFlow(res.data, isDraft ? f.step : 2);
      TNR_UI.toast(isDraft ? '检查单已暂存' : '检查单已提交，进入医嘱与处方', 'success');
    } catch (e) {
      TNR_UI.toast(e.message || '保存失败', 'danger');
    }
  },

  /* 与后端 `_replace_items()` **同口径**的前端预检。
     前端漏拦不会造成脏数据（服务端也会拒），但用户会看到一句与自己操作
     对不上的报错；这里提前拦并把出问题的项目自动展开。 */
  txPlanProblem() {
    const f = this.state.treatmentFlow;
    const keys = Object.keys(f.draft.items);
    for (let idx = 0; idx < keys.length; idx++) {
      const k = keys[idx];
      const meta = this.txItemMeta(k) || {};
      const item = f.draft.items[k];
      if (meta.needs_chip && item.plannedDate && !String(item.chipNo).trim()) {
        f.open[k] = true;
        this.renderFlowView();
        return (meta.name || k) + '：填写了预计执行时间就必须填芯片号';
      }
      if (this.txItemState(k).half) {
        f.open[k] = true;
        this.renderFlowView();
        return (meta.name || k) + '：已选处方但未填预计执行时间，请补上';
      }
      /* 绝育病历（第五十一轮改了时机）：这一步**可以整份留空**（手术还没做，
         六栏本来还不存在），但只要落了哪怕一栏就必须六栏齐全（0/6 或 6/6，
         不能 3/6）。半途病历入库会印成「绝育手术：是、手术日期：—」的归档单，
         还会进政府端统计 —— 服务端 `save_sterilization()` 同口径整单拒绝。
         ⚠ 不能写成「一律要求填全」—— 那等于逼医生术前编一份假病历。 */
      if (meta.needs_record) {
        const filled = this.txItemState(k).recFilled;
        if (filled > 0 && filled < this.STER_REC_FIELDS.length) {
          f.open[k] = true;
          this.renderFlowView();
          return '绝育手术病历尚未填写完整：要么现在六栏一次填全，'
            + '要么全部清空、等术后在「执行与办结」补录';
        }
      }
    }
    return '';
  },

  txPlanPayload() {
    const f = this.state.treatmentFlow;
    const items = Object.keys(f.draft.items).map(k => {
      const it = f.draft.items[k];
      return {
        key: k,
        planned_date: it.plannedDate || '',
        chip_no: it.chipNo || '',
        notes: it.notes || '',
        rx_other: it.rxOther || '',
        rx: it.rx.slice()
      };
    });
    return { advice: f.draft.advice || '', items: items,
             sterilization: f.draft.sterilization || null };
  },

  async saveTreatmentPlan(advance) {
    const f = this.state.treatmentFlow;
    if (!f) return;
    const bad = this.txPlanProblem();
    if (bad) { TNR_UI.toast(bad, 'warning'); return; }
    try {
      const res = await TNR_API.treatmentPlan(f.detail.id, this.txPlanPayload());
      TNR_UI.assertOk(res, '医嘱与处方保存失败');
      this.startTreatmentFlow(res.data, advance ? 3 : f.step);
      TNR_UI.toast('医嘱与处方已暂存', 'success');
    } catch (e) {
      TNR_UI.toast(e.message || '保存失败', 'danger');
    }
  },

  async diagnoseTreatment() {
    const f = this.state.treatmentFlow;
    if (!f) return;
    const bad = this.txPlanProblem();
    if (bad) { TNR_UI.toast(bad, 'warning'); return; }
    if (!String(f.draft.advice).trim() && !this.txMustItems().length) {
      TNR_UI.toast('请至少填写医嘱，或为某个处置项目填写时间与处方', 'warning'); return;
    }
    try {
      const saveRes = await TNR_API.treatmentPlan(f.detail.id, this.txPlanPayload());
      TNR_UI.assertOk(saveRes, '医嘱与处方保存失败');
      const res = await TNR_API.treatmentDiagnose(f.detail.id);
      TNR_UI.assertOk(res, '诊断完成失败');
      this.startTreatmentFlow(res.data, 3);
      TNR_UI.toast('诊断完成，进入执行阶段', 'success');
    } catch (e) {
      TNR_UI.toast(e.message || '操作失败', 'danger');
    }
  },

  async undiagnoseTreatment() {
    const f = this.state.treatmentFlow;
    if (!f) return;
    const ok = await TNR_UI.confirm('撤回诊断后可以继续修改医嘱与处方，确定撤回吗？', '撤回诊断');
    if (!ok) return;
    try {
      const res = await TNR_API.treatmentUndiagnose(f.detail.id);
      TNR_UI.assertOk(res, '撤回诊断失败');
      this.startTreatmentFlow(res.data, 2);
      TNR_UI.toast('已撤回诊断，可继续修改医嘱与处方', 'warning');
    } catch (e) {
      TNR_UI.toast(e.message || '撤回失败', 'danger');
    }
  },

  async executeTreatmentItem(itemId) {
    const f = this.state.treatmentFlow;
    if (!f) return;
    if (!itemId) { TNR_UI.toast('请先保存医嘱与处方，再执行项目', 'warning'); return; }
    try {
      const res = await TNR_API.treatmentExecuteItem(f.detail.id, itemId);
      TNR_UI.assertOk(res, '执行失败');
      this.startTreatmentFlow(res.data, 3);
      TNR_UI.toast(res.message || '已执行完成，库存已扣减', 'success');
    } catch (e) {
      TNR_UI.toast(e.message || '执行失败', 'danger');
    }
  },

  async undoTreatmentItem(itemId) {
    const f = this.state.treatmentFlow;
    if (!f) return;
    const ok = await TNR_UI.confirm('撤销后该项的库存会退回、芯片会退回「可用」，确定撤销吗？', '撤销执行');
    if (!ok) return;
    try {
      const res = await TNR_API.treatmentUndoItem(f.detail.id, itemId);
      TNR_UI.assertOk(res, '撤销失败');
      this.startTreatmentFlow(res.data, 3);
      TNR_UI.toast(res.message || '已撤销执行', 'warning');
    } catch (e) {
      TNR_UI.toast(e.message || '撤销失败', 'danger');
    }
  },

  /* 待出院阶段改「建议去向」（第五十七轮）。成功后用服务端返回的详情重建
     流程状态，建议值、提示与出院分流全部跟着刷新；失败时把选中态回滚成
     服务端仍保留的旧值 —— 否则医生以为改好了，出院管理照旧选不到。 */
  async txSaveAdvice(key, prev) {
    const f = this.state.treatmentFlow;
    if (!f || !f.detail) return;
    try {
      const res = await TNR_API.treatmentSetAdvice(f.detail.id, key);
      TNR_UI.assertOk(res, '建议去向修改失败');
      this.startTreatmentFlow(res.data, 3);
      TNR_UI.toast(res.message || '建议去向已保存', 'success');
    } catch (e) {
      f.draft.disposal = prev || '';
      this.renderFlowView();
      TNR_UI.toast(e.message || '建议去向修改失败', 'danger');
    }
  },

  async markTreatmentDone() {
    const f = this.state.treatmentFlow;
    if (!f) return;
    try {
      // 当前选中的只是「建议去向」：后端写 `disposal_advised`，动物进入
      // 「待出院」。真正的办结由出院管理里的单据完成（见各出院接口）。
      const res = await TNR_API.treatmentDone(f.detail.id, f.draft.disposal);
      TNR_UI.assertOk(res, '诊疗完成失败');
      this.startTreatmentFlow(res.data, 3);
      TNR_UI.toast(res.message || '诊疗完成，动物进入待出院', 'success');
    } catch (e) {
      TNR_UI.toast(e.message || '操作失败', 'danger');
    }
  },

  /* ==================== 药品弹层 ====================
     处方药品走**现有物料体系**（`Material` 的「药品 / 疫苗 / 驱虫药」三类），
     不新建独立药品字典 —— 与后端 `services_treatment.RX_CATEGORIES` 同口径。
     类别常量在这里再写一份是**故意**的：它是「前端筛选口径」而不是业务真源，
     真源仍是后端 `_replace_items()` 的校验（类别不对会被 400 拒绝）。 */
  openMedPicker(key) {
    const f = this.state.treatmentFlow;
    if (!f) return;
    const item = f.draft.items[key];
    if (!item) return;
    const RX_CATS = ['medicine', 'vaccine', 'dewormer'];
    const CATS = [['all', '全部'], ['medicine', '药品'], ['vaccine', '疫苗'], ['dewormer', '驱虫药']];
    let group = 'all';
    let kw = '';

    TNR_UI.modal({
      title: '从库存药品中选择',
      size: 'lg',
      body: (bodyEl) => {
        bodyEl.innerHTML = '<div class="form-group" style="margin-bottom:0;">'
          + '<input class="form-input" data-role="kw" placeholder="搜索药品名称 / 批号">'
          + '</div>'
          + '<div data-role="cats" style="display:flex;gap:var(--sp-2);flex-wrap:wrap;margin:var(--sp-3) 0;"></div>'
          + '<div data-role="rows"></div>';
        const rowsEl = bodyEl.querySelector('[data-role="rows"]');
        const catsEl = bodyEl.querySelector('[data-role="cats"]');
        const kwEl = bodyEl.querySelector('[data-role="kw"]');

        const renderRows = () => {
          const meds = (this._txMaterials || []).filter(m => {
            if (RX_CATS.indexOf(m.category) < 0) return false;
            if (group !== 'all' && m.category !== group) return false;
            if (!kw) return true;
            const hay = (String(m.name || '') + ' ' + String(m.batch_no || '')).toLowerCase();
            return hay.indexOf(kw) >= 0;
          });
          if (!meds.length) {
            rowsEl.innerHTML = '<div class="tx-empty">该条件下暂无可用药品</div>';
            return;
          }
          rowsEl.innerHTML = meds.map(m => {
            const on = item.rx.some(id => String(id) === String(m.id));
            const stock = Number(m.hospital_stock || 0);
            const out = stock <= 0;
            return '<div style="display:flex;align-items:center;gap:var(--sp-3);'
              + 'padding:10px 11px;border:1px solid ' + (on ? 'var(--bamboo-green)' : 'var(--border-light)')
              + ';border-radius:var(--r-lg);margin-bottom:8px;'
              + (on ? 'background:var(--bamboo-light);' : '') + (out ? 'opacity:.55;' : '') + '">'
              + '<div style="flex:1;min-width:0;">'
              + '<div style="font-size:var(--fs-md);font-weight:600;">' + this.escape(m.name)
              + (out ? ' <span class="badge badge-default">无库存</span>' : '') + '</div>'
              + '<div style="font-size:var(--fs-xs);color:var(--ink-light);margin-top:2px;">'
              + this.escape(m.specification || '') + ' · '
              + this.escape(TNR_API.getMaterialCategoryText(m.category))
              + (m.batch_no ? ' · 批号 ' + this.escape(m.batch_no) : '') + '</div></div>'
              + '<div style="flex:none;text-align:right;">'
              + '<div style="font-size:var(--fs-xs);color:var(--ink-muted);margin-bottom:3px;">本院库存 '
              + '<b class="' + (stock <= 3 ? 'text-danger' : '') + '">' + stock + '</b></div>'
              + (out ? '<button class="btn btn-sm btn-secondary" disabled>不可选</button>'
                     : '<button class="btn btn-sm ' + (on ? 'btn-success' : 'btn-outline')
                       + '" data-role="pick" data-v="' + m.id + '">'
                       + (on ? '✓ 已选' : '＋1 头份') + '</button>')
              + '</div></div>';
          }).join('');
          rowsEl.querySelectorAll('[data-role="pick"]').forEach(btn => {
            btn.addEventListener('click', () => {
              const mid = btn.dataset.v;
              const at = item.rx.findIndex(id => String(id) === String(mid));
              if (at >= 0) item.rx.splice(at, 1); else item.rx.push(mid);
              renderRows();
              this.refreshItemCard(key);
            });
          });
        };

        const renderCats = () => {
          catsEl.innerHTML = CATS.map(c => '<button class="btn btn-sm '
            + (group === c[0] ? 'btn-primary' : 'btn-secondary') + '" data-role="cat" data-v="'
            + c[0] + '">' + c[1] + '</button>').join('');
          catsEl.querySelectorAll('[data-role="cat"]').forEach(btn => {
            btn.addEventListener('click', () => { group = btn.dataset.v; renderCats(); renderRows(); });
          });
        };

        // 搜索框**不进重渲染区域**：整块重画会让输入框失焦、光标跳走。
        kwEl.addEventListener('input', () => {
          kw = String(kwEl.value || '').trim().toLowerCase();
          renderRows();
        });
        renderCats();
        renderRows();
      },
      actions: '<button class="btn btn-primary" data-action="close">完成</button>'
    });
  },

  /* ==================== 芯片选择弹层 ==================== */
  async openChipPicker(key) {
    const f = this.state.treatmentFlow;
    if (!f) return;
    const item = f.draft.items[key];
    if (!item) return;
    const meta = this.txItemMeta(key) || {};

    TNR_UI.modal({
      title: '选择可用芯片 · ' + this.escape(meta.name || ''),
      size: 'lg',
      body: async (bodyEl) => {
        bodyEl.innerHTML = '<div class="loading-state">加载可用芯片...</div>';
        let chips = [];
        try {
          // ⚠ `getData` 已经解包（返回 `data.data` 数组本身）—— 再取 `.data`
          // 恒为 undefined，会把「有芯片」渲染成「本院暂无可用芯片」。
          chips = await TNR_API.getAvailableChips(meta.materialId || null) || [];
        } catch (e) {
          bodyEl.innerHTML = '<div class="tx-empty">加载失败：' + this.escape(e.message || '') + '</div>';
          return;
        }
        if (!chips.length) {
          bodyEl.innerHTML = '<div class="tx-empty">本院暂无可用芯片。<br>请先确认芯片物料已采购/下发并签收。</div>';
          return;
        }
        const cur = String(item.chipNo || '').trim();
        bodyEl.innerHTML = '<div class="form-group" style="margin-bottom:var(--sp-3);">'
          + '<input class="form-input" data-role="kw" placeholder="搜索芯片号">'
          + '</div><div data-role="rows"></div>';
        const rowsEl = bodyEl.querySelector('[data-role="rows"]');
        const kwEl = bodyEl.querySelector('[data-role="kw"]');
        let kw = '';

        const renderRows = () => {
          const list = chips.filter(ch => {
            if (!kw) return true;
            return String(ch.number || '').toLowerCase().includes(kw);
          });
          if (!list.length) {
            rowsEl.innerHTML = '<div class="tx-empty">没有匹配的芯片号</div>';
            return;
          }
          rowsEl.innerHTML = list.map(ch => {
            const on = cur === String(ch.number || '');
            return '<div style="display:flex;align-items:center;gap:var(--sp-3);'
              + 'padding:10px 11px;border:1px solid ' + (on ? 'var(--bamboo-green)' : 'var(--border-light)')
              + ';border-radius:var(--r-lg);margin-bottom:8px;'
              + (on ? 'background:var(--bamboo-light);' : '') + '">'
              + '<div style="flex:1;min-width:0;">'
              + '<div style="font-size:var(--fs-md);font-weight:600;font-family:var(--font-mono);">' + this.escape(ch.number || '') + '</div>'
              + '<div style="font-size:var(--fs-xs);color:var(--ink-light);margin-top:2px;">'
              + this.escape(ch.materialName || '') + (ch.batchNo ? ' · 批号 ' + this.escape(ch.batchNo) : '') + '</div></div>'
              + '<div style="flex:none;">'
              + '<button class="btn btn-sm ' + (on ? 'btn-success' : 'btn-outline') + '" data-role="pick" data-v="' + this.escape(ch.number || '') + '">'
              + (on ? '✓ 已选' : '选择') + '</button></div></div>';
          }).join('');
          rowsEl.querySelectorAll('[data-role="pick"]').forEach(btn => {
            btn.addEventListener('click', () => {
              item.chipNo = btn.dataset.v;
              renderRows();
              this.refreshItemCard(key);
            });
          });
        };
        kwEl.addEventListener('input', () => {
          kw = String(kwEl.value || '').trim().toLowerCase();
          renderRows();
        });
        renderRows();
      },
      actions: '<button class="btn btn-primary" data-action="close">完成</button>'
    });
  },


  /* 绝育手术病历弹窗（第四十七轮从旧「快速登记」表单并入三步流程）。
   *
   * 读写的是 `state.treatmentFlow.draft.sterilization`，不再有独立的
   * `treatmentData` —— 旧表单整块删掉后那里已经不存在。
   *
   * ⚠ 第五十一轮：保存去哪里**按诊疗单走到哪一步分三条路**，别合并成一条 ——
   *  ① 未诊断 → 只写前端草稿，随「医嘱与处方」由 `txPlanPayload()` 一起提交；
   *     此时六栏可以**全部清空**（0/6 合法：手术还没做），所以这里不能像
   *     过去那样「少一栏就不给保存」，否则医生只剩「术前编一份假病历」一条路。
   *  ② 已诊断（手术已做完）→ 直接 POST `treatmentSterilization()`。
   *     不能走 `treatmentPlan()`：那条接口在已诊断后一律拒绝（防绕过前端改
   *     处方、而执行阶段按处方扣库存会账实不符），对病历来说它是关着的。
   *  ③ 已办结 → 只读。病历是归档事实，改不得。
   * 「必须六栏齐全」由服务端两处闸门保证：`save_sterilization()`（填了就须填全）
   *  与 `execute_item()`（病历类项目执行前必须齐全）。 */
  openSterilizationModal() {
    const f = this.state.treatmentFlow;
    if (!f) { TNR_UI.toast('请先进入诊疗流程', 'warning'); return; }
    const detail = f.detail || {};
    const ro = !!detail.is_disposed;
    const postOp = !!detail.diagnosed_at && !ro;
    const ex = f.draft.sterilization || {};
    const dis = ro ? ' disabled' : '';
    TNR_UI.modal({
      title: ro ? '绝育手术病历（已办结 · 只读）'
        : (postOp ? '绝育手术病历 · 术后补录' : '绝育手术病历'),
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML =
          '<div class="form-row"><div class="form-group"><label class="form-label">手术日期<span class="req">*</span></label><input type="date" class="form-input" id="sterSurgeryDate" value="' + this.escape(ex.surgery_date || TNR_UI.todayStr()) + '"' + dis + ' /></div>' +
          '<div class="form-group"><label class="form-label">主刀医生<span class="req">*</span></label><input type="text" class="form-input" id="sterSurgeon" value="' + this.escape(ex.surgeon || '') + '" placeholder="请输入医生姓名"' + dis + ' /></div></div>' +
          '<div class="form-group"><label class="form-label">诊断<span class="req">*</span></label><textarea class="form-textarea" id="sterDiagnosis" placeholder="请输入诊断信息"' + dis + '>' + this.escape(ex.diagnosis || '') + '</textarea></div>' +
          '<div class="form-group"><label class="form-label">麻醉方式<span class="req">*</span></label><select class="form-select" id="sterAnesthesia"' + dis + '><option value="">请选择</option>' + ['吸入麻醉', '注射麻醉', '复合麻醉'].map(o =>
            '<option value="' + o + '" ' + (ex.anesthesia === o ? 'selected' : '') + '>' + o + '</option>').join('') + '</select></div>' +
          '<div class="form-group"><label class="form-label">手术过程<span class="req">*</span></label><textarea class="form-textarea" id="sterProcedure" placeholder="请描述手术过程"' + dis + '>' + this.escape(ex.procedure || '') + '</textarea></div>' +
          '<div class="form-group"><label class="form-label">恢复情况<span class="req">*</span></label><textarea class="form-textarea" id="sterRecovery" placeholder="请描述术后恢复情况"' + dis + '>' + this.escape(ex.recovery || '') + '</textarea></div>' +
          (ro ? '' : '<div class="tx-tip inf" style="margin:0 0 var(--sp-3);"><span>ℹ</span><div>'
            + (postOp ? '病历写进档案后，这张单才能点「治疗完成」执行绝育。'
              : '手术还没做的话可以整份留空 —— 到「执行与办结」再补录。'
                + '一旦填了任何一栏，就必须六栏齐全才能保存。')
            + '</div></div>') +
          '<div style="display:flex;gap:var(--sp-3);justify-content:flex-end;">'
          + (!postOp && !ro && this.STER_DRAFT_FIELDS.some(k => !!String(ex[k] || '').trim())
            ? '<button class="btn btn-ghost" id="sterClearBtn">清空，术后补录</button>' : '')
          + '<button class="btn btn-secondary" data-close>' + (ro ? '关闭' : '取消') + '</button>'
          + (ro ? '' : '<button class="btn btn-primary" id="sterSaveBtn">'
            + (postOp ? '提交病历' : '保存病历') + '</button>') + '</div>';
        const closeBtn = bodyEl.querySelector('[data-close]');
        if (closeBtn) closeBtn.onclick = closeFn;
        const clearBtn = bodyEl.querySelector('#sterClearBtn');
        if (clearBtn) clearBtn.onclick = () => {
          // 留空是术前的常态，但「手术日期」默认带了今天 —— 让用户逐栏删
          // 才能清空太反直觉，所以给一个明确入口。六栏一起归零 = 0/6 合法。
          const blank = {};
          this.STER_REC_FIELDS.forEach(k => { blank[k] = ''; });
          this.state.treatmentFlow.draft.sterilization = blank;
          closeFn();
          this.refreshItemCard('sterilization');
          TNR_UI.toast('手术病历已留空，术后到「执行与办结」补录', 'info');
        };
        const saveBtn = bodyEl.querySelector('#sterSaveBtn');
        if (!saveBtn) return;
        saveBtn.onclick = async () => {
          const pick = id => String(bodyEl.querySelector(id).value || '').trim();
          const d = {
            surgery_date: pick('#sterSurgeryDate'), surgeon: pick('#sterSurgeon'),
            diagnosis: pick('#sterDiagnosis'), anesthesia: pick('#sterAnesthesia'),
            procedure: pick('#sterProcedure'), recovery: pick('#sterRecovery')
          };
          const vals = this.STER_REC_FIELDS.map(k => d[k]);
          const filled = vals.filter(v => !!v).length;
          if (filled < vals.length) {              // 半份病历：哪栏空就点名哪栏
            const miss = this.STER_REC_FIELDS
              .filter((k, i) => !vals[i]).map(k => this.STER_REC_LABELS[k]);
            TNR_UI.toast('还差：' + miss.join('、')
              + (postOp ? '' : '；要留到术后再填，请点「清空，术后补录」'), 'warning');
            return;
          }
          this.state.treatmentFlow.draft.sterilization = d;
          if (!postOp) TNR_UI.toast('病历已暂存，保存医嘱时一并提交', 'success');
          if (!postOp) {
            closeFn();
            // 不整页重渲染：那样会丢掉同一步里其它没保存的输入。
            // 只刷这张卡片的摘要与按钮即可。
            this.refreshItemCard('sterilization');
            return;
          }
          // ② 术后补录：直接落库。成功后用服务端详情重建本地状态 ——
          //    「治疗完成」按钮的病历闸门读的就是这份库内数据。
          saveBtn.disabled = true;
          try {
            const res = await TNR_API.treatmentSterilization(detail.id, d);
            TNR_UI.assertOk(res, '病历保存失败');
            closeFn();
            this.startTreatmentFlow(res.data, 3);
            TNR_UI.toast(res.message || '绝育手术病历已保存', 'success');
          } catch (e) {
            saveBtn.disabled = false;
            TNR_UI.toast(e.message || '病历保存失败', 'danger');
          }
        };
      }
    });
  },

  async renderTreatmentList() {
    const c = document.getElementById('treatmentContent');
    if (!c) return;
    c.innerHTML = '<div class="loading-state">加载中...</div>';

    try {
      const treatments = await TNR_API.getTreatmentsStrict();
      if (treatments.length === 0) {
        c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📋</div><div class="empty-state-text">暂无诊疗记录</div></div>';
        return;
      }

      const rows = treatments.map(t => {
        const items = [];
        if (t.items_sterilization) items.push('<span class="badge badge-cinnabar">绝育</span>');
        if (t.items_vaccine) items.push('<span class="badge badge-info">疫苗</span>');
        if (t.items_deworming) items.push('<span class="badge badge-success">驱虫</span>');
        if (t.items_chip) items.push('<span class="badge badge-warning">芯片</span>');
        return {
          pet_code: t.pet_code || '',
          _items: items.join(' ') || '—',
          hospital_name: t.hospital_name || '',
          _doctor: this.escape(t.sterilization_surgeon || t.operator_name || '—'),
          created_at: t.created_at,
          _status: this.treatmentStatusBadge(t.status),
          _actions: '<button class="btn-link" data-view-treatment="' + t.id + '">详情</button>',
        };
      });

      /* 列表卡不再渲染标题；筛选条放列表容器内 —— 本页有「诊疗操作 / 诊疗记录」两个
         子标签，筛选条只对记录有意义，放 sub-tabs 下面常驻会在另一个标签页里显得多余。
         ⚠ 筛选条的 id 直接写在 `.filter-bar` 上，**不要**再套一层 `<div id="...">`：
         那一层与筛选条同高，`position: sticky` 的包含块就是它自己，冻结直接失效。 */
      c.innerHTML = TNR_UI.renderFilterBar([
        { key: 'keyword', label: '搜索', type: 'search', placeholder: '宠物编号 / 医院 / 医生' },
        { key: 'start_date', label: '开始日期', type: 'date' },
        { key: 'end_date', label: '结束日期', type: 'date' }
      ], '', { id: 'treatListFilterBar' }) + '<div id="treatTableHolder"></div>';
      const treatBar = document.getElementById('treatListFilterBar');
      const renderTreatTable = () => {
        const v = TNR_UI.getFilterValues(treatBar);
        const list = rows.filter(r => {
          // 同上：`created_at` 是 UTC 串，必须按本地时区取日期再比较。
          if (!TNR_UI.inDateRange(r.created_at, v.start_date, v.end_date)) return false;
          if (!v.keyword) return true;
          return [r.pet_code, r.hospital_name, r._doctor]
            .filter(Boolean).join(' ').toLowerCase().includes(v.keyword);
        });
        TNR_UI.mountTable('treatTableHolder', { id: 'hospital.treatments', columns: [
            { title: '宠物编号', key: 'pet_code' },
            { title: '诊疗项目', key: '_items', sortable: false },
            { title: '医院', key: 'hospital_name' },
            { title: '医生', key: '_doctor', sortable: false },
            { title: '日期', key: 'created_at', render: r => this.formatDateTime(r.created_at) },
            { title: '状态', key: '_status', sortable: false },
            { title: '操作', key: '_actions', sortable: false },
          ], data: list, emptyText: '暂无诊疗记录' });
      };
      renderTreatTable();
      TNR_UI.bindFilter(renderTreatTable, c);

      TNR_UI.delegateClick(c, {
        'data-view-treatment': (id) => this.viewTreatmentDetail(id)
      });
    } catch (e) {
      c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">加载失败</div></div>';
    }
  },

  async viewTreatmentDetail(treatmentId) {
    try {
      const res = await fetch('/api/business/treatments/' + treatmentId + '/');
      const result = await res.json();
      if (!result.success) { TNR_UI.toast(result.message || '加载失败', 'danger'); return; }
      const t = result.data;
      const pet = t.pet || {};

      let html = '<div class="detail-section"><div class="detail-section-title">宠物信息</div>' + TNR_UI.renderDescList([
        {label: '宠物编号', value: this.escape(t.pet_code)},
        {label: '名称', value: this.escape(pet.name || '—')},
        {label: '物种/品种', value: pet.species ? (this.escape(pet.species) + (pet.breed ? ' / ' + this.escape(pet.breed) : '')) : '—'},
        {label: '性别/年龄', value: (pet.gender || pet.age) ? ((pet.gender ? this.escape(pet.gender) : '') + (pet.gender && pet.age ? ' / ' : '') + (pet.age ? this.escape(pet.age) : '')) : '—'}
      ]) + '</div>';

      html += '<div class="detail-section"><div class="detail-section-title">诊疗项目</div>' + TNR_UI.renderDescList([
        {label: '绝育手术', value: t.items_sterilization ? '<span class="badge badge-success">已执行</span>' : '<span class="badge badge-default">未执行</span>'},
        {label: '疫苗接种', value: t.items_vaccine ? '<span class="badge badge-success">已执行</span>' : '<span class="badge badge-default">未执行</span>'},
        {label: '驱虫护理', value: t.items_deworming ? '<span class="badge badge-success">已执行</span>' : '<span class="badge badge-default">未执行</span>'},
        {label: '芯片植入', value: t.items_chip ? '<span class="badge badge-success">已执行</span>' : '<span class="badge badge-default">未执行</span>'}
      ]) + '</div>';

      if (t.sterilization_surgeon) {
        html += '<div class="detail-section"><div class="detail-section-title">绝育病历档案</div>' + TNR_UI.renderDescList([
          {label: '手术日期', value: this.formatDate(t.sterilization_surgery_date)},
          {label: '主刀医生', value: this.escape(t.sterilization_surgeon)},
          {label: '麻醉方式', value: this.escape(t.sterilization_anesthesia)},
          {label: '诊断', value: this.escape(t.sterilization_diagnosis), full: true},
          {label: '手术过程', value: this.escape(t.sterilization_procedure), full: true},
          {label: '恢复情况', value: this.escape(t.sterilization_recovery), full: true}
        ]) + '</div>';
      }
      if (t.vaccine_type) {
        html += '<div class="detail-section"><div class="detail-section-title">疫苗接种记录</div>' + TNR_UI.renderDescList([
          {label: '疫苗类型', value: this.escape(t.vaccine_type)},
          {label: '批号', value: this.escape(t.vaccine_batch_no)},
          {label: '接种日期', value: this.formatDate(t.vaccine_date)},
          {label: '数量', value: (t.vaccine_quantity || 0) + '支'}
        ]) + '</div>';
      }
      if (t.deworming_type) {
        html += '<div class="detail-section"><div class="detail-section-title">驱虫护理记录</div>' + TNR_UI.renderDescList([
          {label: '驱虫药类型', value: this.escape(t.deworming_type)},
          {label: '批号', value: this.escape(t.deworming_batch_no)},
          {label: '用药日期', value: this.formatDate(t.deworming_date)},
          {label: '数量', value: (t.deworming_quantity || 0) + '盒'}
        ]) + '</div>';
      }
      if (t.chip_no) {
        html += '<div class="detail-section"><div class="detail-section-title">芯片植入记录</div>' + TNR_UI.renderDescList([
          {label: '芯片编号', value: this.escape(t.chip_no)},
          {label: '植入日期', value: this.formatDate(t.chip_date)}
        ]) + '</div>';
      }
      html += '<div class="detail-section"><div class="detail-section-title">其他信息</div>' + TNR_UI.renderDescList([
        {label: '医院', value: this.escape(t.hospital_name)},
        {label: '操作人', value: this.escape(t.operator_name)},
        {label: '创建时间', value: this.formatDateTime(t.created_at)},
        {label: '状态', value: this.treatmentStatusBadge(t.status)}
      ]) + '</div>';

      TNR_UI.drawer({title: '诊疗记录详情 · ' + this.escape(t.pet_code),
        body: html + TNR_UI.printDocBtn('treatment', t.id)});
    } catch (e) {
      TNR_UI.toast(e.message || '加载失败', 'danger');
    }
  },

  /* ========== 4. 出院管理（第五十四轮：四个去向并入二级菜单） ==========
   * 出院只有四种去向：领养 / 死亡出库 / 领回 / 放归。
   * 它们不是四个独立页面，而是出院的一级子状态 —— 因此侧栏不再保留
   * 「领养信息维护」「安乐死处置」，全部收进出院页顶部的二级菜单。
   *
   * 领养再分三态（与用户口径一一对应）：
   *   待领养 = 诊疗完成、资料可维护、推送领养大厅；
   *   已预约 = 捕捉点已批准领养人 / 线下已登记，动物仍在院，待出库交接；
   *   已出库 = 领养完成，只读。
   *
   * 死亡出库 = 先登记安乐死 / 死亡，再填「死亡出库单」把遗体交给
   * 无害化处理厂（交接人、接收机构、接收人、接收单位、签字、可选照片）。
   *
   * 领回 = 主人到场认领：交接单 + 主人签字。
   *
   * 放归 = 捕捉点转运的反向：医院勾选待放归动物开「运回单」→ 捕捉点
   * 签收（接收单）→ 动物进入捕捉点的待放归列表，再由捕捉点与物业签字放归。
   * 运回单状态：待确认 / 已确认出库 / 已驳回 / 已撤回。
   */
  DSP_DOC_KEY: {
    adoption: 'adoption',
    euthanasia: 'euthanasia',
    owner_return: 'owner_return',
    return_order: 'return_order'
  },

  dspPet(petId) {
    const map = (this._dsp && this._dsp.petMap) || {};
    return map[String(petId)] || null;
  },

  dspPetCell(pet, fallbackCode) {
    const code = (pet && pet.code) || fallbackCode || '—';
    const name = pet ? (pet.name || '') : '';
    const attr = pet ? TNR_API.petAttrText(pet) : '';
    return '<div style="font-weight:500;">' + this.escape(code)
      + (name ? ' · ' + this.escape(name) : '') + '</div>'
      + (attr ? '<div class="fs-sm text-muted">' + this.escape(attr) + '</div>' : '');
  },

  dspImgTag(url, alt) {
    return url
      ? '<img src="' + this.escape(url) + '" alt="' + this.escape(alt || '照片') + '"'
        + ' style="max-width:100%;max-height:180px;border-radius:var(--r-md);border:1px solid var(--border-light);">'
      : '<span class="text-muted">—</span>';
  },

  dspSigTag(sig) {
    return sig
      ? '<img src="' + sig + '" alt="电子签名"'
        + ' style="max-width:240px;max-height:110px;border:1px solid var(--border-light);border-radius:var(--r-md);background:#fff;">'
      : '<span class="text-muted">—</span>';
  },

  dspUserLabel() {
    const u = this.currentUser() || {};
    return u.name || u.username || '';
  },

  dspStaffName() {
    const u = this.currentUser() || {};
    return u.name || u.username || '';
  },

  /* 已被领养流程占用的动物：待领出领养单 ∪ 已批准未出库的线上申请。
     捕捉点批准申请后不会改 `Pet.status`，所以凡按状态筛「可出院动物」
     的列表都必须再排一次这个集合。 */
  dspReservedPetIds() {
    const d = this._dsp || {};
    const reserved = new Set();
    (d.adoptions || []).forEach(a => {
      if (a.status === 'pending_claim' && a.pet_id) reserved.add(String(a.pet_id));
    });
    (d.applications || []).forEach(a => {
      if (a.status === 'approved' && a.pet_id) reserved.add(String(a.pet_id));
    });
    return reserved;
  },

  /* 该动物当前的建议去向 key；`undefined` = 不在「待出院」流程里
     （后端对它不做建议校验，列表按旧口径放行），`''` = 在流程里但没补录。 */
  dspAdviceOf(petId) {
    return ((this._dsp || {}).adviceMap || {})[String(petId)];
  },

  /* 候选列表能否出现这只动物 —— 与后端 `discharge_candidate_error()` 同判据。 */
  dspAdviceAllows(pet, advisedKeys) {
    const advised = this.dspAdviceOf(pet.id);
    if (advised === undefined) return true;
    return advisedKeys.indexOf(advised) !== -1;
  },

  /* 诊疗完成却没记建议去向的动物：四个去向都选不到它，
     必须先到诊疗看板补录（出院页给出直达入口，否则用户只看到「暂无动物」）。 */
  dspNoAdvicePets() {
    const d = this._dsp || {};
    const out = [];
    (d.boardPending || []).forEach(r => {
      if (!r.pet_id || (r.disposal_advised || '')) return;
      const pet = (d.petMap || {})[String(r.pet_id)];
      if (pet && !pet.is_deleted) out.push({ pet: pet, row: r });
    });
    return out;
  },

  /* 出院页顶部的「待补录建议去向」横幅。
     这四个去向的候选列表都按建议去向分流，没补录的动物在四个页签里都不出现 ——
     不给出直达入口的话，操作员看到的只是「暂无动物」，只能去诊疗看板自己翻。 */
  dspAdviceNotice() {
    const list = this.dspNoAdvicePets();
    if (!list.length) return '';
    const MAX = 12;
    const chips = list.slice(0, MAX).map(it =>
      '<button type="button" class="chip" data-dspadvice="1"'
      + ' data-pet="' + this.escape(it.row.pet_id) + '"'
      + ' data-tid="' + this.escape(it.row.treatment_id || '') + '">'
      + this.escape(it.row.pet_code || (it.pet && it.pet.code) || '—')
      + '</button>').join('');
    return '<div class="dsp-notice">'
      + '<div class="tx"><b>' + list.length + ' 只动物「诊疗完成」时没有记录建议去向</b>'
      + '<i>领养 / 死亡 / 领回 / 放归四个去向都选不到它。点下面的动物编号到诊疗流程第三步'
      + '补录或修改建议去向，再回来办理出院。'
      + (list.length > MAX ? '（仅列出前 ' + MAX + ' 只）' : '') + '</i></div>'
      + '<div class="chips">' + chips + '</div></div>';
  },

  /* 从出院页跳进诊疗流程：先切到诊疗页（流程视图挂在 #treatmentContent 上），
     再打开这张单并落在第三步「建议去向」。 */
  dspGotoAdvice(petId, tid) {
    this.state.treatmentTab = 'flow';
    this.state.treatmentFilter = 'pending_discharge';
    this.navigateTo('treatment');
    this.openTreatmentFlow(petId, tid, 3);
  },

  /* 诊疗完成、尚未办结的动物（看板 `pending_discharge` 桶）。
     第五十六轮起诊疗页只记「建议去向」，动物仍是 `in_treatment`；
     各去向的候选列表必须把这一类算进来，否则诊疗完成后在出院管理里
     再也找不到它（页面显示「暂无动物」，其实刚治完一只）。
     `advisedKeys` 传去向 key 列表：**没给建议（空串）的动物不进任何去向** ——
     后端同样会 400，让它出现在列表里只会让操作员白点一次。 */
  dspPendingPets(advisedKeys) {
    const d = this._dsp || {};
    const reserved = this.dspReservedPetIds();
    const out = [];
    const seen = new Set();
    (d.boardPending || []).forEach(r => {
      if (!r.pet_id || reserved.has(String(r.pet_id))) return;
      const pet = (d.petMap || {})[String(r.pet_id)];
      if (!pet || pet.is_deleted) return;
      if (advisedKeys.indexOf(r.disposal_advised || '') === -1) return;
      if (seen.has(String(pet.id))) return;
      seen.add(String(pet.id));
      out.push(pet);
    });
    return out;
  },

  /* 待领养 = 已诊疗完成、尚未被预约的动物。
     捕捉点批准线上申请后**不会**改 `Pet.status`，动物仍是 pending_adopt；
     不按申请单排除的话，同一只动物会同时出现在「待领养」和「已预约」，
     数量与操作入口都对不上。
     建议去向不是「领养」的待出院动物同样要排除 —— 后端 `adoption_register`
     会 400，列在这里只会让操作员白点一次（第五十七轮）。 */
  dspReadyPets() {
    const d = this._dsp || {};
    const reserved = this.dspReservedPetIds();
    const ready = (d.pets || []).filter(p =>
      p.status === 'pending_adopt' && !p.is_deleted && !reserved.has(String(p.id))
      && this.dspAdviceAllows(p, ['pending_adopt']));
    const seen = new Set(ready.map(p => String(p.id)));
    this.dspPendingPets(['pending_adopt']).forEach(p => {
      if (!seen.has(String(p.id))) ready.push(p);
    });
    return ready;
  },

  /* 已预约 = 已有领养单待出库（pending_claim）∪ 已批准但还没生成领养单的申请。
     同一只动物两处都有时只保留领养单行 —— 出库接口也应走领养单。 */
  dspReservedRows() {
    const d = this._dsp || {};
    const petMap = d.petMap || {};
    const pending = (d.adoptions || []).filter(a => a.status === 'pending_claim');
    const withAdoption = new Set(pending.map(a => String(a.pet_id)));
    const apps = (d.applications || []).filter(a => {
      if (a.status !== 'approved' || !a.pet_id) return false;
      if (withAdoption.has(String(a.pet_id))) return false;
      const pet = petMap[String(a.pet_id)] || a.pet || null;
      return !pet || pet.status !== 'adopted';
    });
    const rows = pending.map(a => ({
      source: 'adoption',
      id: a.id,
      pet_id: a.pet_id,
      pet: petMap[String(a.pet_id)] || a.pet || null,
      pet_code: a.pet_code || '',
      name: a.adopter_name || '',
      phone: a.adopter_phone || '',
      id_card: a.adopter_id_card || '',
      address: a.adopter_address || '',
      origin: '线下登记',
      at: TNR_UI.firstDate(a.created_at, a.adopted_at),
      raw: a
    }));
    apps.forEach(a => {
      rows.push({
        source: 'application',
        id: a.id,
        pet_id: a.pet_id,
        pet: petMap[String(a.pet_id)] || a.pet || null,
        pet_code: a.pet_code || '',
        name: a.applicant_name || '',
        phone: a.applicant_phone || '',
        id_card: a.applicant_id_card || '',
        address: a.applicant_address || '',
        origin: '线上申请',
        at: TNR_UI.firstDate(a.applied_at, a.reviewed_at),
        raw: a
      });
    });
    return rows;
  },

  dspCompletedAdoptions() {
    return ((this._dsp && this._dsp.adoptions) || []).filter(a => a.status === 'completed');
  },

  /* 领回候选：只有建议去向确实是「主人领回」的待出院动物才列进来。
     原先这里完全不看建议，医生建议「领养」的猫能从领回页被办成已领回。 */
  dspOwnerPets() {
    const reserved = this.dspReservedPetIds();
    return ((this._dsp && this._dsp.pets) || [])
      .filter(p => (p.status === 'pending_adopt' || p.status === 'in_treatment')
        && !p.is_deleted && !reserved.has(String(p.id))
        && this.dspAdviceAllows(p, ['owner_returned']));
  },

  /* 待放归 = 待领养动物，且没有未办结的旧版放归单、未被领养流程预留。
     运回单开出后动物变成 return_pending，自然从本列表消失。
     建议去向不是「放回原小区」的待出院动物同样排除（后端会 400）。 */
  dspReleaseCandidates() {
    const d = this._dsp || {};
    const reserved = this.dspReservedPetIds();
    const openReleasePets = new Set();
    (d.releases || []).forEach(r => {
      if (TNR_UI.OPEN_RELEASE_STATUS.indexOf(r.status) === -1) return;
      const id = r.pet_id || (r.pet && r.pet.id);
      if (id) openReleasePets.add(String(id));
    });
    const candidates = (d.pets || []).filter(p =>
      p.status === 'pending_adopt' && !p.is_deleted
      && !reserved.has(String(p.id)) && !openReleasePets.has(String(p.id))
      && this.dspAdviceAllows(p, ['released']));
    const seen = new Set(candidates.map(p => String(p.id)));
    this.dspPendingPets(['released']).forEach(p => {
      if (!seen.has(String(p.id)) && !openReleasePets.has(String(p.id))) {
        candidates.push(p);
      }
    });
    return candidates;
  },

  dspTabCount(kind) {
    const d = this._dsp || {};
    if (kind === 'adopt') return this.dspReadyPets().length + this.dspReservedRows().length;
    if (kind === 'dead') return (d.euth || []).filter(e => !e.outbound_at).length;
    if (kind === 'release') return (d.returnOrders || []).filter(o => o.status === 'pending').length;
    return null;
  },

  async render_discharge() {
    const page = document.getElementById('page-discharge');
    page.innerHTML = '<div class="loading-state">加载中...</div>';
    try {
      /* 全部严格读：任何一条失败都要显式报错。
         静默读会把 403/500 渲染成「这个去向没有动物」，操作员会以为
         这些流程真的没有待办。 */
      const [board, pets, adoptions, applications, euth, returns, returnOrders, releases, listings, returnShelters] = await Promise.all([
        TNR_API.getTreatmentBoard(),
        TNR_API.getHospitalPetsStrict(),
        TNR_API.getAdoptionsStrict(),
        TNR_API.getAdoptionApplicationsStrict(),
        TNR_API.getEuthanasiaStrict(),
        TNR_API.getOwnerReturnsStrict(),
        TNR_API.getReturnOrdersStrict(),
        TNR_API.getReleasesStrict(),
        TNR_API.getHallListingsStrict(),
        /* 运回单的捕捉点选项：通用机构接口按区县过滤，区县医院看不到市级捕捉点，
           所以走专用接口。读失败必须可见 —— 选项静默为空等于「运回单开不出来」。 */
        TNR_API.getData('/api/business/return-orders/shelters/')
      ]);
      const petMap = {};
      (pets || []).forEach(p => { petMap[String(p.id)] = p; });
      const boardPending = (board && board.pending_discharge) || [];
      /* 待出院动物的建议去向索引（petId → disposal_advised，空串 = 没记）。
         出院四个去向的候选列表全部按它分流，判据与后端
         `services_discharge.discharge_candidate_error()` 一一对应：
         在 `pending_discharge` 桶里 = 有一张「诊疗完成、未办结」的单，
         后端会校验建议；不在里面的（历史存量、没走诊疗流程）不校验。 */
      const adviceMap = {};
      boardPending.forEach(r => {
        if (r.pet_id) adviceMap[String(r.pet_id)] = r.disposal_advised || '';
      });
      this._dsp = {
        boardPending: boardPending,
        adviceMap: adviceMap,
        pets: pets || [],
        petMap: petMap,
        adoptions: adoptions || [],
        applications: applications || [],
        euth: euth || [],
        returns: returns || [],
        returnOrders: returnOrders || [],
        releases: releases || [],
        listings: listings || [],
        returnShelters: returnShelters || []
      };
      this.renderDischargeShell();
    } catch (e) {
      page.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚠️</div>'
        + '<div class="empty-state-text">出院管理加载失败</div>'
        + '<div class="empty-state-desc">' + this.escape(e.message || '请稍后重试') + '</div></div>';
    }
  },

  renderDischargeShell() {
    const page = document.getElementById('page-discharge');
    if (!page) return;
    const TABS = [
      {k: 'adopt', ic: '🏠', n: '领养', d: '待领养 · 已预约 · 已出库'},
      {k: 'dead', ic: '🕯️', n: '死亡出库', d: '安乐死 / 死亡 / 无害化交接'},
      {k: 'owner', ic: '🔁', n: '领回', d: '主人认领并签字接回'},
      {k: 'release', ic: '🌿', n: '放归', d: '运回捕捉点 → 签字放归'}
    ];
    let html = '<div class="dsp-tabs">';
    TABS.forEach(t => {
      const cnt = this.dspTabCount(t.k);
      html += '<button type="button" class="dsp-tab'
        + (this.state.dischargeTab === t.k ? ' active' : '') + '" data-dspmod="' + t.k + '">'
        + '<span class="ic">' + t.ic + '</span>'
        + '<span class="tx"><b>' + t.n + '</b><i>' + t.d + '</i></span>'
        + (cnt === null ? '' : '<span class="cnt">' + cnt + '</span>')
        + '</button>';
    });
    html += '</div>' + this.dspAdviceNotice() + '<div id="dspContent"></div>';
    page.innerHTML = html;

    /* 委托绑在稳定容器 `#page-discharge` 上，一次注册全部动作。
       视图重渲染只换 innerHTML，监听器不会随子节点丢失。 */
    TNR_UI.delegateClick(page, {
      'data-dspmod': (v) => {
        this.state.dischargeTab = v;
        this.renderDischargeShell();
      },
      'data-dspadopt': (v) => {
        this.state.adoptionTab = v;
        this.renderDischargeShell();
      },
      'data-dsprefresh': () => this.render_discharge(),
      'data-dspadvice': (v, el) => this.dspGotoAdvice(
        el.dataset.pet, el.dataset.tid || ''),
      'data-dspact': (v, el) => this.onDspAction(v, el)
    }, 'discharge');
    this.renderDspTab();
  },

  renderDspTab() {
    const c = document.getElementById('dspContent');
    if (!c) return;
    const kind = this.state.dischargeTab;
    if (kind === 'adopt') this.renderDspAdoption(c);
    else if (kind === 'dead') this.renderDspDeath(c);
    else if (kind === 'owner') this.renderDspOwner(c);
    else this.renderDspRelease(c);
  },

  dspSubTab(key, label, count, activeKey) {
    return '<div class="tab-item' + (activeKey === key ? ' active' : '')
      + '" data-dspadopt="' + key + '">' + label + ' (' + count + ')</div>';
  },

  dspDateMatch(value, v) {
    return TNR_UI.inDateRange(value, v.start_date, v.end_date);
  },

  dspKeyword(v, parts) {
    if (!v.keyword) return true;
    return parts.filter(Boolean).join(' ').toLowerCase().indexOf(v.keyword) !== -1;
  },

  /* ---------- 领养 ---------- */
  renderDspAdoption(c) {
    const sub = this.state.adoptionTab || 'ready';
    const ready = this.dspReadyPets();
    const reserved = this.dspReservedRows();
    const completed = this.dspCompletedAdoptions();
    c.innerHTML = '<div class="tabs dsp-subtabs">'
      + this.dspSubTab('ready', '已诊疗好 · 待领养', ready.length, sub)
      + this.dspSubTab('reserved', '已预约', reserved.length, sub)
      + this.dspSubTab('outbound', '已出库', completed.length, sub)
      + '</div><div id="dspAdoptBody"></div>';
    const body = document.getElementById('dspAdoptBody');
    if (sub === 'reserved') this.renderDspAdoptReserved(body);
    else if (sub === 'outbound') this.renderDspAdoptOutbound(body);
    else this.renderDspAdoptReady(body);
  },

  renderDspAdoptReady(body) {
    const d = this._dsp || {};
    const pendingApps = (d.applications || []).filter(a => a.status === 'pending');
    // 诊疗完成、等出院的动物：状态列显示「待出院」而不是 Pet.status 的「诊疗中」。
    const pendingIds = new Set(
      this.dspPendingPets(['pending_adopt']).map(p => String(p.id)));
    const listingByPet = {};
    (d.listings || []).forEach(l => {
      const id = l.pet_id || (l.pet && l.pet.id);
      if (id) listingByPet[String(id)] = l;
    });

    body.innerHTML = TNR_UI.renderFilterBar([
      {key: 'keyword', label: '搜索', type: 'search', placeholder: '动物编号 / 名称 / 品种 / 简介'},
      {key: 'hall', label: '领养大厅', type: 'select', options: [
        {value: 'on', label: '已上架'},
        {value: 'off', label: '未上架'}
      ]}
    ], '<button type="button" class="btn btn-secondary btn-sm" data-dsprefresh="1">刷新</button>',
      {id: 'dspAdoptBar'})
      + (pendingApps.length
        ? '<div class="card mb-4"><div class="card-header"><div class="card-title">'
          + '<span class="card-title-bar"></span>待审核领养申请 (' + pendingApps.length + ')</div>'
          + '<div class="fs-sm text-muted">申请人在领养大厅提交，审核通过后进入「已预约」</div></div>'
          + '<div class="card-body-flush"><div id="dspApplTable"></div></div></div>'
        : '')
      + '<div class="card"><div class="card-body-flush"><div id="dspReadyTable"></div></div></div>';

    const mount = () => {
      const v = TNR_UI.getFilterValues(document.getElementById('dspAdoptBar'));
      if (pendingApps.length) {
        const appRows = pendingApps.map(a => ({
          _pet: this.dspPetCell(this.dspPet(a.pet_id), a.pet_code),
          applicant_name: a.applicant_name || '',
          applicant_phone: a.applicant_phone || '',
          _qual: this.escape((a.qualification || '').slice(0, 40)),
          applied_at: a.applied_at || a.created_at || '',
          _actions: '<div class="dsp-acts">'
            + '<button class="btn btn-sm btn-success" data-dspact="appl-review" data-id="' + a.id + '" data-action="approve">通过</button>'
            + '<button class="btn btn-sm btn-danger" data-dspact="appl-review" data-id="' + a.id + '" data-action="reject">拒绝</button>'
            + '<button class="btn btn-sm btn-secondary" data-dspact="appl-detail" data-id="' + a.id + '">详情</button>'
            + '</div>'
        })).filter(r => this.dspKeyword(v, [r._pet, r.applicant_name, r.applicant_phone, r._qual]));
        TNR_UI.mountTable('#dspApplTable', {
          id: 'hospital.discharge.applications',
          columns: [
            {title: '动物', key: '_pet', sortable: false},
            {title: '申请人', key: 'applicant_name'},
            {title: '电话', key: 'applicant_phone'},
            {title: '资质说明', key: '_qual', sortable: false},
            {title: '申请时间', key: 'applied_at', render: r => this.formatDateTime(r.applied_at)},
            {title: '操作', key: '_actions', sortable: false, align: 'right'}
          ],
          data: appRows,
          emptyText: '暂无待审核申请'
        });
      }

      let rows = this.dspReadyPets().map(p => {
        const listing = listingByPet[String(p.id)] || null;
        const inHall = !!(listing && listing.is_active);
        const intro = (listing && listing.intro) || p.description || '';
        const personality = (listing && listing.personality) || '';
        const bodyCondition = (listing && listing.body_condition) || '';
        return {
          _pet: this.dspPetCell(p),
          _status: pendingIds.has(String(p.id))
            ? TNR_UI.boardStageBadge('pending_discharge')
            : TNR_UI.petStatusBadge(p.status),
          _hall: inHall
            ? '<span class="badge badge-success">已上架</span>'
            : '<span class="badge badge-warning">未上架</span>',
          _intro: this.escape(intro ? intro.slice(0, 40) : '—'),
          _actions: '<div class="dsp-acts">'
            + '<button class="btn btn-sm btn-primary" data-dspact="adopt-info" data-pet="' + p.id + '">维护资料</button>'
            + (inHall
              ? '<button class="btn btn-sm btn-secondary" data-dspact="adopt-hall" data-pet="' + p.id + '" data-active="0">下架</button>'
              : '<button class="btn btn-sm btn-gold" data-dspact="adopt-hall" data-pet="' + p.id + '" data-active="1">发布至大厅</button>')
            + '</div>',
          _hay: [p.code, p.name, p.species, p.breed, p.gender, intro, personality, bodyCondition, inHall ? '已上架' : '未上架'],
          _date: p.created_at || '',
          _hallOn: inHall
        };
      });
      rows = rows.filter(r => {
        if (v.hall === 'on' && !r._hallOn) return false;
        if (v.hall === 'off' && r._hallOn) return false;
        if (!this.dspDateMatch(r._date, v)) return false;
        return this.dspKeyword(v, r._hay);
      });
      TNR_UI.mountTable('#dspReadyTable', {
        id: 'hospital.discharge.adopt.ready',
        columns: [
          {title: '动物', key: '_pet', sortable: false},
          {title: '动物状态', key: '_status', sortable: false},
          {title: '领养大厅', key: '_hall', sortable: false},
          {title: '简介', key: '_intro', sortable: false},
          {title: '操作', key: '_actions', sortable: false, align: 'right'}
        ],
        data: rows,
        emptyText: '暂无待领养动物'
      });
    };
    mount();
    TNR_UI.bindFilter(mount, document.getElementById('dspAdoptBar'));
  },

  renderDspAdoptReserved(body) {
    body.innerHTML = TNR_UI.renderFilterBar([
      {key: 'keyword', label: '搜索', type: 'search', placeholder: '动物编号 / 领养人 / 电话 / 单号'},
      {key: 'origin', label: '预约来源', type: 'select', options: [
        {value: '线下登记', label: '线下登记'},
        {value: '线上申请', label: '线上申请'}
      ]}
    ], '<button type="button" class="btn btn-secondary btn-sm" data-dsprefresh="1">刷新</button>',
      {id: 'dspReservedBar'})
      + '<div class="card"><div class="card-body-flush"><div id="dspReservedTable"></div></div></div>';

    const mount = () => {
      const v = TNR_UI.getFilterValues(document.getElementById('dspReservedBar'));
      const rows = this.dspReservedRows().filter(r => {
        if (v.origin && v.origin !== r.origin) return false;
        if (!this.dspDateMatch(r.at, v)) return false;
        return this.dspKeyword(v, [r.pet_code, r.name, r.phone, r.id_card, r.address,
          r.origin, r.raw.ledger_no, r.raw.qualification]);
      }).map(r => ({
        _pet: this.dspPetCell(r.pet, r.pet_code),
        _adopter: '<div style="font-weight:500;">' + this.escape(r.name || '—') + '</div>'
          + '<div class="fs-sm text-muted">' + this.escape(r.phone || '') + '</div>',
        _origin: '<span class="badge ' + (r.source === 'application' ? 'badge-info' : 'badge-default') + '">'
          + r.origin + '</span>'
          + (r.raw.ledger_no ? '<div class="fs-sm text-muted">' + this.escape(r.raw.ledger_no) + '</div>' : ''),
        _status: r.source === 'application'
          ? TNR_UI.applyStatusBadge(r.raw.status)
          : TNR_UI.adoptionStatusBadge(r.raw.status),
        _at: this.formatDateTime(r.at),
        _actions: '<div class="dsp-acts">'
          + '<button class="btn btn-sm btn-primary" data-dspact="adopt-outbound"'
          + ' data-src="' + r.source + '" data-id="' + r.id + '">出库</button>'
          + '<button class="btn btn-sm btn-secondary" data-dspact="adopt-detail"'
          + ' data-src="' + r.source + '" data-id="' + r.id + '">详情</button>'
          + '</div>',
        _date: r.at || ''
      }));
      TNR_UI.mountTable('#dspReservedTable', {
        id: 'hospital.discharge.adopt.reserved',
        columns: [
          {title: '动物', key: '_pet', sortable: false},
          {title: '领养人', key: '_adopter', sortable: false},
          {title: '预约来源', key: '_origin', sortable: false},
          {title: '状态', key: '_status', sortable: false},
          {title: '预约时间', key: '_at', sortable: false},
          {title: '操作', key: '_actions', sortable: false, align: 'right'}
        ],
        data: rows,
        emptyText: '暂无已预约动物'
      });
    };
    mount();
    TNR_UI.bindFilter(mount, document.getElementById('dspReservedBar'));
  },

  renderDspAdoptOutbound(body) {
    body.innerHTML = TNR_UI.renderFilterBar([
      {key: 'keyword', label: '搜索', type: 'search', placeholder: '动物编号 / 领养人 / 电话 / 单号'}
    ], '<button type="button" class="btn btn-secondary btn-sm" data-dsprefresh="1">刷新</button>',
      {id: 'dspOutboundBar'})
      + '<div class="card"><div class="card-body-flush"><div id="dspOutboundTable"></div></div></div>';

    const mount = () => {
      const v = TNR_UI.getFilterValues(document.getElementById('dspOutboundBar'));
      const rows = this.dspCompletedAdoptions().filter(a => {
        const at = TNR_UI.firstDate(a.outbound_at, a.adopted_at, a.created_at);
        if (!this.dspDateMatch(at, v)) return false;
        return this.dspKeyword(v, [a.pet_code, a.adopter_name, a.adopter_phone,
          a.ledger_no, a.handover_name, a.handoverName]);
      }).map(a => {
        const at = TNR_UI.firstDate(a.outbound_at, a.adopted_at, a.created_at);
        return {
          _pet: this.dspPetCell(this.dspPet(a.pet_id), a.pet_code),
          _adopter: '<div>' + this.escape(a.adopter_name || '—') + '</div>'
            + '<div class="fs-sm text-muted">' + this.escape(a.adopter_phone || '') + '</div>',
          _out: '<span class="badge badge-success">已出库</span>',
          _date: this.formatDateTime(at),
          _handover: this.escape(a.handover_name || a.handoverName || '—'),
          _actions: '<div class="dsp-acts">'
            + '<button class="btn btn-sm btn-secondary" data-dspact="adopt-detail"'
            + ' data-src="adoption" data-id="' + a.id + '">详情</button>'
            + '<button class="btn btn-sm btn-secondary" data-dspact="print"'
            + ' data-doc="adoption" data-id="' + a.id + '">打印</button>'
            + '</div>',
          _dateRaw: at || ''
        };
      });
      TNR_UI.mountTable('#dspOutboundTable', {
        id: 'hospital.discharge.adopt.outbound',
        columns: [
          {title: '动物', key: '_pet', sortable: false},
          {title: '领养人', key: '_adopter', sortable: false},
          {title: '状态', key: '_out', sortable: false},
          {title: '出库日期', key: '_date', sortable: false},
          {title: '交接人', key: '_handover', sortable: false},
          {title: '操作', key: '_actions', sortable: false, align: 'right'}
        ],
        data: rows,
        emptyText: '暂无已出库领养记录'
      });
    };
    mount();
    TNR_UI.bindFilter(mount, document.getElementById('dspOutboundBar'));
  },

  /* ---------- 死亡出库 ---------- */
  renderDspDeath(c) {
    c.innerHTML = TNR_UI.renderFilterBar([
      {key: 'keyword', label: '搜索', type: 'search', placeholder: '动物编号 / 原因 / 接收单位 / 操作人'},
      {key: 'out', label: '出库状态', type: 'select', options: [
        {value: 'todo', label: '待出库'},
        {value: 'done', label: '已出库'}
      ]}
    ], '<button type="button" class="btn btn-primary btn-sm" data-dspact="euth-create">登记安乐死 / 死亡</button>'
      + '<button type="button" class="btn btn-secondary btn-sm" data-dsprefresh="1">刷新</button>',
      {id: 'dspEuthBar'})
      + '<div class="card"><div class="card-body-flush"><div id="dspEuthTable"></div></div></div>';

    const mount = () => {
      const v = TNR_UI.getFilterValues(document.getElementById('dspEuthBar'));
      const rows = ((this._dsp && this._dsp.euth) || []).filter(e => {
        const done = !!e.outbound_at;
        if (v.out === 'todo' && done) return false;
        if (v.out === 'done' && !done) return false;
        const at = TNR_UI.firstDate(e.outbound_at, e.euthanized_at, e.created_at);
        if (!this.dspDateMatch(at, v)) return false;
        return this.dspKeyword(v, [e.pet_code, e.reason, e.condition,
          e.receiving_org, e.receiver_name, e.operator_name, e.ledger_no]);
      }).map(e => {
        const done = !!e.outbound_at;
        return {
          _pet: this.dspPetCell(this.dspPet(e.pet_id), e.pet_code),
          _kind: e.kind === 'dead'
            ? '<span class="badge badge-danger">死亡 / 无害化</span>'
            : '<span class="badge badge-danger">安乐死</span>',
          _reason: this.escape(e.reason || '—'),
          _date: e.euthanized_at || '',
          _out: done
            ? '<span class="badge badge-success">已出库</span>'
              + '<div class="fs-sm text-muted">' + this.formatDate(e.outbound_at) + '</div>'
            : '<span class="badge badge-warning">待出库</span>',
          _receiver: done
            ? this.escape(e.receiving_org || '—')
            : '—',
          _actions: '<div class="dsp-acts">'
            + (done
              ? '<button class="btn btn-sm btn-secondary" data-dspact="euth-detail" data-id="' + e.id + '">详情</button>'
                + '<button class="btn btn-sm btn-secondary" data-dspact="print" data-doc="euthanasia" data-id="' + e.id + '">打印</button>'
              : '<button class="btn btn-sm btn-primary" data-dspact="euth-outbound" data-id="' + e.id + '">死亡出库单</button>'
                + '<button class="btn btn-sm btn-secondary" data-dspact="euth-detail" data-id="' + e.id + '">详情</button>')
            + '</div>',
          _dateRaw: TNR_UI.firstDate(e.outbound_at, e.euthanized_at, e.created_at) || ''
        };
      });
      TNR_UI.mountTable('#dspEuthTable', {
        id: 'hospital.discharge.euthanasia',
        columns: [
          {title: '动物', key: '_pet', sortable: false},
          {title: '处置方式', key: '_kind', sortable: false},
          {title: '原因', key: '_reason', sortable: false},
          {title: '处置日期', key: '_date', sortable: false,
            render: r => this.formatDate(r._date)},
          {title: '出库状态', key: '_out', sortable: false},
          {title: '接收机构', key: '_receiver', sortable: false},
          {title: '操作', key: '_actions', sortable: false, align: 'right'}
        ],
        data: rows,
        emptyText: '暂无死亡 / 安乐死记录'
      });
    };
    mount();
    TNR_UI.bindFilter(mount, document.getElementById('dspEuthBar'));
  },

  /* ---------- 领回 ---------- */
  renderDspOwner(c) {
    c.innerHTML = TNR_UI.renderFilterBar([
      {key: 'keyword', label: '搜索', type: 'search', placeholder: '动物编号 / 主人 / 电话 / 单号'}
    ], '<button type="button" class="btn btn-primary btn-sm" data-dspact="owner-create">办理领回</button>'
      + '<button type="button" class="btn btn-secondary btn-sm" data-dsprefresh="1">刷新</button>',
      {id: 'dspOwnerBar'})
      + '<div class="card"><div class="card-body-flush"><div id="dspOwnerTable"></div></div></div>';

    const mount = () => {
      const v = TNR_UI.getFilterValues(document.getElementById('dspOwnerBar'));
      const rows = ((this._dsp && this._dsp.returns) || []).filter(r => {
        const at = TNR_UI.firstDate(r.return_time, r.created_at);
        if (!this.dspDateMatch(at, v)) return false;
        return this.dspKeyword(v, [r.pet_code, r.owner_name, r.owner_phone,
          r.owner_id_card, r.owner_address, r.reason, r.ledger_no]);
      }).map(r => ({
        _pet: this.dspPetCell(this.dspPet(r.pet_id), r.pet_code),
        _owner: '<div style="font-weight:500;">' + this.escape(r.owner_name || '—') + '</div>'
          + '<div class="fs-sm text-muted">' + this.escape(r.owner_phone || '') + '</div>',
        _time: this.formatDateTime(TNR_UI.firstDate(r.return_time, r.created_at)),
        _status: '<span class="badge badge-success">已领回</span>',
        _actions: '<div class="dsp-acts">'
          + '<button class="btn btn-sm btn-secondary" data-dspact="owner-detail" data-id="' + r.id + '">详情</button>'
          + '<button class="btn btn-sm btn-secondary" data-dspact="print" data-doc="owner_return" data-id="' + r.id + '">打印</button>'
          + '</div>',
        _dateRaw: TNR_UI.firstDate(r.return_time, r.created_at) || ''
      }));
      TNR_UI.mountTable('#dspOwnerTable', {
        id: 'hospital.discharge.owner',
        columns: [
          {title: '动物', key: '_pet', sortable: false},
          {title: '主人', key: '_owner', sortable: false},
          {title: '领回时间', key: '_time', sortable: false},
          {title: '状态', key: '_status', sortable: false},
          {title: '操作', key: '_actions', sortable: false, align: 'right'}
        ],
        data: rows,
        emptyText: '暂无主人领回记录'
      });
    };
    mount();
    TNR_UI.bindFilter(mount, document.getElementById('dspOwnerBar'));
  },

  /* ---------- 放归（运回捕捉点） ---------- */
  renderDspRelease(c) {
    const orders = (this._dsp && this._dsp.returnOrders) || [];
    c.innerHTML = '<div class="card mb-4"><div class="card-header">'
      + '<div class="card-title"><span class="card-title-bar"></span>待放归动物</div>'
      + '<div class="fs-sm text-muted">勾选动物 → 生成运回单 → 捕捉点签收接收单</div></div>'
      + '<div class="card-body">'
      + TNR_UI.renderFilterBar([
        {key: 'keyword', label: '搜索', type: 'search', placeholder: '动物编号 / 名称 / 品种'}
      ], '', {id: 'dspPickBar'})
      + '<div id="dspPickList" class="dsp-pick-list"></div>'
      + '<div class="form-group mt-3"><label class="form-label">运回备注</label>'
      + '<input class="form-input" id="dspReturnNote" placeholder="交接说明（选填）"></div>'
      + '<div class="dsp-actions" style="justify-content:flex-start;">'
      + '<button type="button" class="btn btn-primary" data-dspact="return-create">生成运回单</button>'
      + '</div></div></div>'
      + TNR_UI.renderFilterBar([
        {key: 'keyword', label: '搜索', type: 'search', placeholder: '运回单号 / 动物编号 / 捕捉点'},
        {key: 'status', label: '单据状态', type: 'select', options: [
          {value: 'pending', label: '待确认'},
          {value: 'received', label: '已确认出库'},
          {value: 'rejected', label: '已驳回'},
          {value: 'withdrawn', label: '已撤回'}
        ]}
      ], '<button type="button" class="btn btn-secondary btn-sm" data-dsprefresh="1">刷新</button>',
        {id: 'dspReturnBar'})
      + '<div class="card"><div class="card-body-flush"><div id="dspReturnTable"></div></div></div>';

    const mountPicks = () => {
      const pickBar = document.getElementById('dspPickBar');
      if (!pickBar) return;
      const v = TNR_UI.getFilterValues(pickBar);
      const list = this.dspReleaseCandidates().filter(p =>
        this.dspKeyword(v, [p.code, p.name, p.species, p.breed, p.gender]));
      const wrap = document.getElementById('dspPickList');
      if (!list.length) {
        wrap.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🏡</div>'
          + '<div class="empty-state-text">暂无待放归动物</div>'
          + '<div class="empty-state-desc">需诊疗完成且未被领养 / 放归占用</div></div>';
        return;
      }
      const shelves = {};
      list.forEach(p => { if (p.shelter_name) shelves[p.shelter_name] = 1; });
      const tip = Object.keys(shelves).length > 1
        ? '<div class="dsp-hint">这些动物原属不同捕捉点；生成运回单时可统一选择市级或本院所在区县的捕捉点。</div>'
        : '';
      wrap.innerHTML = tip + list.map(p =>
        '<label class="dsp-pick">'
        + '<input type="checkbox" value="' + p.id + '" data-petpick="1">'
        + '<span class="dsp-pick-main">' + this.dspPetCell(p) + '</span>'
        + '<span class="badge badge-info">待放归</span>'
        + '<span class="fs-sm text-muted">' + this.escape(p.shelter_name || '') + '</span>'
        + '</label>').join('');
    };

    const mountOrders = () => {
      const bar = document.getElementById('dspReturnBar');
      if (!bar) return;
      const v = TNR_UI.getFilterValues(bar);
      const rows = orders.filter(o => {
        if (v.status && v.status !== o.status) return false;
        const at = TNR_UI.firstDate(o.created_at, o.received_at);
        if (!this.dspDateMatch(at, v)) return false;
        const codes = (o.pets || []).map(p => p.code).join(' ') || o.pet_codes;
        return this.dspKeyword(v, [o.ledger_no, codes, o.shelter_name,
          o.hospital_name, o.note, o.receiver_name, o.reject_reason]);
      }).map(o => {
        const pets = o.pets || [];
        const codes = pets.length ? pets.map(p => p.code).join('、') : (o.pet_codes || '');
        return {
          _no: '<div class="font-mono fs-sm" style="color:var(--cinnabar);">'
            + this.escape(o.ledger_no || o.id) + '</div>'
            + '<div class="fs-sm text-muted">' + this.formatDateTime(o.created_at) + '</div>',
          _pets: '<div>' + this.escape(codes || '—') + '</div>'
            + '<div class="fs-sm text-muted">共 ' + (o.pet_count || pets.length || 0) + ' 只</div>',
          _shelter: this.escape(o.shelter_name || '—'),
          _status: TNR_UI.returnOrderStatusBadge(o.status)
            + (o.received_at
              ? '<div class="fs-sm text-muted">' + this.formatDate(o.received_at) + '</div>' : ''),
          _actions: '<div class="dsp-acts">'
            + (o.status === 'pending'
              ? '<button class="btn btn-sm btn-danger" data-dspact="return-withdraw" data-id="' + o.id + '">撤回</button>' : '')
            + '<button class="btn btn-sm btn-secondary" data-dspact="return-detail" data-id="' + o.id + '">详情</button>'
            + '<button class="btn btn-sm btn-secondary" data-dspact="print" data-doc="return_order" data-id="' + o.id + '">打印</button>'
            + '</div>',
          _date: TNR_UI.firstDate(o.created_at, o.received_at) || ''
        };
      });
      TNR_UI.mountTable('#dspReturnTable', {
        id: 'hospital.discharge.returnOrders',
        columns: [
          {title: '运回单', key: '_no', sortable: false},
          {title: '动物', key: '_pets', sortable: false},
          {title: '捕捉点', key: '_shelter', sortable: false},
          {title: '状态', key: '_status', sortable: false},
          {title: '操作', key: '_actions', sortable: false, align: 'right'}
        ],
        data: rows,
        emptyText: '暂无运回单'
      });
    };

    mountPicks();
    mountOrders();
    TNR_UI.bindFilter(mountPicks, document.getElementById('dspPickBar'));
    TNR_UI.bindFilter(mountOrders, document.getElementById('dspReturnBar'));
  },

  onDspAction(act, el) {
    const id = el.dataset.id || '';
    if (act === 'adopt-info') this.dspOpenAdoptInfo(el.dataset.pet);
    else if (act === 'adopt-hall') this.dspToggleHall(el.dataset.pet, el.dataset.active === '1');
    else if (act === 'appl-review') this.dspReviewApplication(id, el.dataset.action);
    else if (act === 'appl-detail') this.dspViewApplication(id);
    else if (act === 'adopt-outbound') this.dspOpenAdoptionOutbound(el.dataset.src, id);
    else if (act === 'adopt-detail') this.dspViewAdoption(el.dataset.src, id);
    else if (act === 'euth-create') this.dspOpenEuthCreate();
    else if (act === 'euth-outbound') this.dspOpenEuthOutbound(id);
    else if (act === 'euth-detail') this.dspViewEuth(id);
    else if (act === 'owner-create') this.dspOpenOwnerCreate();
    else if (act === 'owner-detail') this.dspViewOwner(id);
    else if (act === 'return-create') this.dspOpenReturnCreate();
    else if (act === 'return-withdraw') this.dspWithdrawReturn(id);
    else if (act === 'return-detail') this.dspViewReturnOrder(id);
    else if (act === 'print') this.dspPrint(el.dataset.doc, id);
  },

  dspPrint(doc, id) {
    const key = this.DSP_DOC_KEY[doc];
    if (!key || !id) return;
    window.open('/print/' + key + '/' + id + '/', '_blank', 'noopener');
  },

  /* ---------- 领养资料维护 / 大厅上下架 ---------- */
  dspOpenAdoptInfo(petId) {
    const pet = this.dspPet(petId);
    if (!pet) { TNR_UI.toast('动物档案已刷新，请重试', 'warning'); return; }
    const listing = ((this._dsp.listings || []).find(l =>
      String(l.pet_id || (l.pet && l.pet.id)) === String(petId))) || null;
    const intro = (listing && listing.intro) || pet.description || '';
    const personality = (listing && listing.personality) || '';
    const bodyCondition = (listing && listing.body_condition) || '';
    const inHall = !!(listing && listing.is_active);

    /* 系统照片全部来自捕捉 / 诊疗档案，这里只决定"显不显示"。
       `listing.photos` 是后端按可见性整理后的清单（含隐藏项与 visible 标记），
       没有 listing 时退回到动物档案字段。 */
    const SYSTEM_PHOTOS = [
      ['photo_capture', '捕捉照片'],
      ['photo_group', '整体合影'],
      ['photo_before', '术前照片'],
      ['photo_after', '术后照片'],
      ['photo_treatment', '诊疗后照片']
    ];
    const listed = (listing && listing.photos) || [];
    const hiddenSet = new Set((listing && (listing.hidden_photos || listing.hiddenPhotos)) || []);
    const systemPhotos = SYSTEM_PHOTOS
      .map(([field, label]) => {
        const row = listed.find(p => p.field === field);
        return {field: field, label: label, url: (row && row.url) || pet[field] || '',
                visible: !hiddenSet.has(field)};
      })
      .filter(p => p.url);
    const extraPhotos = (listing && (listing.extra_photos || listing.extraPhotos)) || [];

    const systemPhotoHtml = systemPhotos.length
      ? '<div class="dsp-photo-grid">' + systemPhotos.map(p =>
        '<div class="dsp-photo">'
        + '<div class="dsp-photo-box"><img src="' + this.escape(p.url) + '" alt="'
        + this.escape(p.label) + '"></div>'
        + '<div class="dsp-photo-label">' + this.escape(p.label) + '</div>'
        + '<label class="dsp-check"><input type="checkbox" data-photo-visible="'
        + p.field + '"' + (p.visible ? ' checked' : '') + '> 大厅显示</label>'
        + '</div>').join('') + '</div>'
      : '<div class="dsp-note">暂无捕捉或诊疗照片；捕捉点、诊疗环节产生后会自动带入，无需在此重复上传。</div>';

    const extraPhotoHtml = extraPhotos.length
      ? '<div class="dsp-photo-grid">' + extraPhotos.map(p =>
        '<div class="dsp-photo">'
        + '<div class="dsp-photo-box"><img src="' + this.escape(p.image || '') + '" alt="补充照片"></div>'
        + '<label class="dsp-check"><input type="checkbox" data-del-extra="' + p.id + '"> 删除</label>'
        + '</div>').join('') + '</div>'
      : '';

    TNR_UI.modal({
      title: '维护领养资料 · ' + pet.code,
      size: 'lg',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="dsp-form">'
          + '<div class="dsp-fld full"><label>动物简介</label>'
          + '<textarea class="form-textarea" id="dspIntro" rows="3" placeholder="供领养大厅展示">'
          + this.escape(intro) + '</textarea></div>'
          + '<div class="dsp-fld"><label>性格特点</label>'
          + '<input class="form-input" id="dspPersonality" value="' + this.escape(personality) + '"'
          + ' placeholder="如：温顺、亲人"></div>'
          + '<div class="dsp-fld"><label>健康状况</label>'
          + '<input class="form-input" id="dspHealth" value="' + this.escape(bodyCondition) + '"'
          + ' placeholder="如：已绝育、已免疫"></div>'
          + '<div class="dsp-fld full"><label>系统已有照片（捕捉 / 诊疗环节自动带入）</label>'
          + systemPhotoHtml + '</div>'
          + '<div class="dsp-fld full"><label>补充照片（仅补拍说明，可多选）</label>'
          + extraPhotoHtml
          + '<label class="dsp-photo-label" style="text-align:left;margin-top:8px;">'
          + '选择新照片<input type="file" id="dspExtraFiles" accept="image/*" multiple></label>'
          + '<div class="dsp-photo-grid" id="dspNewPreviews" style="margin-top:8px;"></div>'
          + '<div class="dsp-note">已上传的补充照片可勾选删除；新选的照片在保存时上传。</div></div>'
          + '<div class="dsp-fld full"><label class="dsp-check">'
          + '<input type="checkbox" id="dspPublish"' + (inHall ? ' checked' : '') + '>'
          + ' 发布到领养大厅（潜在领养人可见）</label></div>'
          + '</div>'
          + '<div class="dsp-actions">'
          + '<button type="button" class="btn btn-secondary" data-close>取消</button>'
          + '<button type="button" class="btn btn-primary" id="dspInfoSave">保存资料</button>'
          + '</div>';
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelector('#dspExtraFiles').addEventListener('change', e => {
          const preview = bodyEl.querySelector('#dspNewPreviews');
          preview.innerHTML = Array.from(e.target.files || []).map(file =>
            '<div class="dsp-photo"><div class="dsp-photo-box" data-new-preview>'
            + '<img alt="待上传"></div><div class="dsp-photo-label">待上传</div></div>').join('');
          const boxes = preview.querySelectorAll('[data-new-preview]');
          Array.from(e.target.files || []).forEach((file, i) => {
            if (!boxes[i]) return;
            const reader = new FileReader();
            reader.onload = ev => {
              boxes[i].innerHTML = '<img src="' + ev.target.result + '" alt="预览">';
            };
            reader.readAsDataURL(file);
          });
        });
        bodyEl.querySelector('#dspInfoSave').onclick = async () => {
          const btn = bodyEl.querySelector('#dspInfoSave');
          btn.disabled = true;
          try {
            const hidden_photos = [];
            bodyEl.querySelectorAll('[data-photo-visible]').forEach(cb => {
              if (!cb.checked) hidden_photos.push(cb.dataset.photoVisible);
            });
            const deleteIds = Array.from(bodyEl.querySelectorAll('[data-del-extra]:checked'))
              .map(cb => Number(cb.dataset.delExtra));
            const files = bodyEl.querySelector('#dspExtraFiles').files || [];
            if (files.length > 6) {
              TNR_UI.toast('补充照片一次最多上传 6 张', 'warning');
              btn.disabled = false;
              return;
            }
            const fd = new FormData();
            fd.append('intro', bodyEl.querySelector('#dspIntro').value.trim());
            fd.append('personality', bodyEl.querySelector('#dspPersonality').value.trim());
            fd.append('body_condition', bodyEl.querySelector('#dspHealth').value.trim());
            fd.append('is_active', bodyEl.querySelector('#dspPublish').checked ? '1' : '0');
            fd.append('hidden_photos', JSON.stringify(hidden_photos));
            if (deleteIds.length) {
              fd.append('delete_photo_ids', JSON.stringify(deleteIds));
            }
            Array.from(files).forEach(f => fd.append('extra_photos', f));
            const res = await TNR_API.saveAdoptionInfo(petId, fd);
            TNR_UI.assertOk(res, '资料保存失败');
            TNR_UI.toast('领养资料已保存', 'success');
            closeFn();
            this.render_discharge();
          } catch (e) {
            TNR_UI.toast(e.message || '保存失败', 'danger');
            btn.disabled = false;
          }
        };
      }
    });
  },

  async dspToggleHall(petId, active) {
    try {
      const res = await TNR_API.editAdoptionInfo(petId, {is_active: active});
      TNR_UI.assertOk(res, active ? '发布失败' : '下架失败');
      TNR_UI.toast(active ? '已发布至领养大厅' : '已从领养大厅下架', 'success');
      this.render_discharge();
    } catch (e) {
      TNR_UI.toast(e.message || '操作失败', 'danger');
    }
  },

  async dspReviewApplication(id, action) {
    const label = action === 'approve' ? '通过' : '拒绝';
    const ok = await TNR_UI.confirm('确认' + label + '该领养申请？', '审核领养申请');
    if (!ok) return;
    try {
      const reviewNote = action === 'reject' ? (window.prompt('请输入拒绝原因：') || '') : '';
      const res = await TNR_API.post(
        TNR_API.BASE + 'adoptions/applications/' + id + '/review/',
        {action: action, review_note: reviewNote});
      TNR_UI.assertOk(res, '审核失败');
      TNR_UI.toast(res.message || '审核完成', 'success');
      this.render_discharge();
    } catch (e) {
      TNR_UI.toast(e.message || '审核失败', 'danger');
    }
  },

  dspViewApplication(id) {
    const a = ((this._dsp && this._dsp.applications) || [])
      .find(x => String(x.id) === String(id));
    if (!a) { TNR_UI.toast('申请数据已刷新，请重试', 'warning'); return; }
    TNR_UI.drawer({
      title: '领养申请 · ' + this.escape(a.pet_code || ''),
      body: '<div class="detail-section"><div class="detail-section-title">申请信息</div>'
        + TNR_UI.renderDescList([
          {label: '动物', value: this.dspPetCell(this.dspPet(a.pet_id), a.pet_code), full: true},
          {label: '申请人', value: this.escape(a.applicant_name || '—')},
          {label: '联系电话', value: this.escape(a.applicant_phone || '—')},
          {label: '身份证号', value: this.escape(a.applicant_id_card || '—')},
          {label: '住址', value: this.escape(a.applicant_address || '—'), full: true},
          {label: '资质说明', value: this.escape(a.qualification || '—'), full: true},
          {label: '申请理由', value: this.escape(a.reason || '—'), full: true},
          {label: '申请时间', value: this.formatDateTime(a.applied_at)},
          {label: '审核状态', value: this.escape(a.status_display || a.status_display_text || a.status || '—')}
        ]) + '</div>'
    });
  },

  /* ---------- 领养出库交接单 ---------- */
  /* 出库照片的手机选择器：拍照 / 相册（本地文件）两个入口。
     单个 file input 在部分手机浏览器里只弹文件管理器，现场拍的照片反而
     找不到；选图后先本地压缩（手机原图 3~5MB，直接传慢且易撞上限）。
     onPick(null) 表示清除已选照片。 */
  dspPhotoPickHtml(key, label, hint) {
    return '<div class="dsp-fld full"><label>' + label + '</label>'
      + '<div class="dsp-photo-pick">'
      + '<button type="button" class="btn btn-secondary" data-photo-cam="' + key + '">📷 拍照</button>'
      + '<button type="button" class="btn btn-secondary" data-photo-lib="' + key + '">🖼 相册 / 文件</button>'
      + '</div>'
      + '<input type="file" accept="image/*" capture="environment"'
      + ' data-photo-input-cam="' + key + '" style="display:none">'
      + '<input type="file" accept="image/*" data-photo-input-lib="' + key + '" style="display:none">'
      + '<div class="dsp-photo-picked" data-photo-preview="' + key + '"></div>'
      + '<div class="dsp-note">' + hint + '</div>'
      + '</div>';
  },

  dspBindPhotoPick(bodyEl, key, onPick) {
    const camInput = bodyEl.querySelector('[data-photo-input-cam="' + key + '"]');
    const libInput = bodyEl.querySelector('[data-photo-input-lib="' + key + '"]');
    const preview = bodyEl.querySelector('[data-photo-preview="' + key + '"]');
    bodyEl.querySelector('[data-photo-cam="' + key + '"]').onclick = () => camInput.click();
    bodyEl.querySelector('[data-photo-lib="' + key + '"]').onclick = () => libInput.click();
    const handle = async (input) => {
      const raw = input.files && input.files[0];
      if (!raw) return;
      preview.textContent = '处理中…';
      const file = await TNR_UI.compressImage(raw);
      preview.innerHTML = '<div class="dsp-photo-picked-box">'
        + '<img alt="已选照片">'
        + '<button type="button" class="btn btn-secondary btn-sm">重新选择</button></div>';
      const img = preview.querySelector('img');
      const reader = new FileReader();
      reader.onload = ev => { img.src = ev.target.result; };
      reader.readAsDataURL(file);
      preview.querySelector('button').onclick = () => {
        camInput.value = '';
        libInput.value = '';
        preview.innerHTML = '';
        onPick(null);
      };
      onPick(file);
    };
    camInput.onchange = () => handle(camInput);
    libInput.onchange = () => handle(libInput);
  },

  dspOpenAdoptionOutbound(source, id) {
    const row = this.dspReservedRows().find(r =>
      r.source === source && String(r.id) === String(id));
    if (!row) { TNR_UI.toast('预约数据已刷新，请重试', 'warning'); return; }
    TNR_UI.modal({
      title: '领养出库 · ' + (row.pet_code || ''),
      size: 'lg',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="tx-tip inf"><span>ℹ</span><div>'
          + '填写领养出库单与交接信息。确认后领养单完成、动物状态变为「已领养」，不可再操作。</div></div>'
          + '<div class="dsp-summary">'
          + '<div><span>动物</span><b>' + this.escape(row.pet_code || '—') + '</b></div>'
          + '<div><span>领养人</span><b>' + this.escape(row.name || '—') + '</b></div>'
          + '<div><span>联系电话</span><b>' + this.escape(row.phone || '—') + '</b></div>'
          + '<div><span>预约来源</span><b>' + row.origin + '</b></div>'
          + '</div>'
          + '<div class="dsp-form">'
          + '<div class="dsp-fld"><label>出库日期<span class="req">*</span></label>'
          + '<input class="form-input" type="date" id="dspOutDate" value="' + TNR_UI.todayStr() + '"></div>'
          + '<div class="dsp-fld"><label>交接人<span class="req">*</span></label>'
          + '<input class="form-input" id="dspOutHandover" value="' + this.escape(this.dspStaffName()) + '"'
          + ' placeholder="医院交接人"></div>'
          + '<div class="dsp-fld full"><label>领养人签字<span class="req">*</span></label>'
          + '<div class="sig-field" id="dspOutSig"></div></div>'
          + this.dspPhotoPickHtml('out', '交接照片（非必需）',
            '手机可直接拍照，或从相册 / 本地文件选择；选图后自动压缩')
          + '</div>'
          + '<div class="dsp-actions">'
          + '<button type="button" class="btn btn-secondary" data-close>取消</button>'
          + '<button type="button" class="btn btn-primary" id="dspOutSubmit">确认出库</button>'
          + '</div>';
        const sig = TNR_UI.initSignatureField(bodyEl.querySelector('#dspOutSig'),
          {title: '领养人签字'});
        let outPhoto = null;
        this.dspBindPhotoPick(bodyEl, 'out', f => { outPhoto = f; });
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelector('#dspOutSubmit').onclick = async () => {
          const date = bodyEl.querySelector('#dspOutDate').value;
          const handover = bodyEl.querySelector('#dspOutHandover').value.trim();
          if (!date) { TNR_UI.toast('请选择出库日期', 'warning'); return; }
          if (!handover) { TNR_UI.toast('请填写交接人', 'warning'); return; }
          if (sig.isEmpty()) { TNR_UI.toast('请完成领养人签字', 'warning'); return; }
          const fd = new FormData();
          fd.append('outbound_at', date);
          fd.append('handover_name', handover);
          fd.append('signature', sig.getDataURL());
          if (outPhoto) fd.append('handover_photo', outPhoto);
          const btn = bodyEl.querySelector('#dspOutSubmit');
          btn.disabled = true;
          try {
            const res = source === 'application'
              ? await TNR_API.outboundAdoptionApplication(id, fd)
              : await TNR_API.outboundAdoption(id, fd);
            TNR_UI.assertOk(res, '领养出库失败');
            TNR_UI.toast('领养出库完成', 'success');
            closeFn();
            this.render_discharge();
          } catch (e) {
            TNR_UI.toast(e.message || '出库失败', 'danger');
            btn.disabled = false;
          }
        };
      }
    });
  },

  dspViewAdoption(source, id) {
    if (source === 'application') { this.dspViewApplication(id); return; }
    const a = ((this._dsp && this._dsp.adoptions) || [])
      .find(x => String(x.id) === String(id));
    if (!a) { TNR_UI.toast('领养单已刷新，请重试', 'warning'); return; }
    const photo = a.handover_photo || a.handoverPhoto || '';
    const rows = [
      {label: '动物', value: this.dspPetCell(this.dspPet(a.pet_id), a.pet_code), full: true},
      {label: '单据编号', value: this.escape(a.ledger_no || '—')},
      {label: '领养人', value: this.escape(a.adopter_name || '—')},
      {label: '联系电话', value: this.escape(a.adopter_phone || '—')},
      {label: '身份证号', value: this.escape(a.adopter_id_card || '—')},
      {label: '住址', value: this.escape(a.adopter_address || '—'), full: true},
      {label: '出库日期', value: this.formatDate(a.outbound_at || a.adopted_at)},
      {label: '交接人', value: this.escape(a.handover_name || a.handoverName || '—')},
      {label: '操作人', value: this.escape(a.operator_name || '—')}
    ];
    TNR_UI.drawer({
      title: '领养出库单 · ' + this.escape(a.pet_code || ''),
      body: '<div class="detail-section"><div class="detail-section-title">交接信息</div>'
        + TNR_UI.renderDescList(rows)
        + '<div class="detail-section-title" style="margin-top:var(--sp-3);">领养人签字</div>'
        + this.dspSigTag(a.signature)
        + '<div class="detail-section-title" style="margin-top:var(--sp-3);">交接照片</div>'
        + this.dspImgTag(photo, '交接照片')
        + '</div>' + TNR_UI.printDocBtn('adoption', a.id)
    });
  },

  /* ---------- 死亡登记 / 死亡出库 ---------- */
  dspOpenEuthCreate() {
    const reserved = this.dspReservedPetIds();
    // 建议去向已给「领养 / 放归 / 领回」的动物不在本列表里：后端
    // `euthanasia_create` 会 400，要登记死亡得先改建议去向（第五十七轮）。
    const eligible = ((this._dsp && this._dsp.pets) || [])
      .filter(p => (p.status === 'in_treatment' || p.status === 'pending_adopt')
        && !reserved.has(String(p.id))
        && this.dspAdviceAllows(p, ['euthanized', 'dead']));
    if (!eligible.length) {
      TNR_UI.toast('暂无可登记的动物（需诊疗中或待领养，且建议去向为死亡 / 安乐死）',
        'warning');
      return;
    }
    TNR_UI.modal({
      title: '登记安乐死 / 死亡',
      size: 'lg',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="tx-tip inf"><span>ℹ</span><div>'
          + '本步只登记临床处置；遗体离院还要在列表中办理「死亡出库单」。</div></div>'
          + '<div class="dsp-form">'
          + '<div class="dsp-fld full"><label>动物<span class="req">*</span></label>'
          + '<select class="form-select" id="dspEuthPet"><option value="">请选择</option>'
          + eligible.map(p => '<option value="' + p.id + '">' + this.escape(p.code)
            + (p.name ? ' · ' + this.escape(p.name) : '') + '（'
            + TNR_UI.petStatusText(p.status) + '）</option>').join('')
          + '</select></div>'
          + '<div class="dsp-fld full"><label>处置方式<span class="req">*</span></label>'
          + '<div class="dsp-chips" id="dspEuthKind">'
          + '<span class="on" data-v="euthanasia">安乐死</span>'
          + '<span data-v="dead">死亡 / 无害化</span></div></div>'
          + '<div class="dsp-fld"><label>处置日期<span class="req">*</span></label>'
          + '<input class="form-input" type="date" id="dspEuthDate" value="' + TNR_UI.todayStr() + '"></div>'
          + '<div class="dsp-fld"><label>原因<span class="req">*</span></label>'
          + '<input class="form-input" id="dspEuthReason" placeholder="如：严重疾病、无法救治"></div>'
          + '<div class="dsp-fld full"><label>动物状况</label>'
          + '<textarea class="form-textarea" id="dspEuthCondition" rows="2"'
          + ' placeholder="详细描述动物状况及处置原因"></textarea></div>'
          + '</div>'
          + '<div class="dsp-actions">'
          + '<button type="button" class="btn btn-secondary" data-close>取消</button>'
          + '<button type="button" class="btn btn-danger" id="dspEuthSubmit">确认登记</button>'
          + '</div>';
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelectorAll('#dspEuthKind span').forEach(sp => {
          sp.onclick = () => {
            bodyEl.querySelectorAll('#dspEuthKind span').forEach(x => x.classList.remove('on'));
            sp.classList.add('on');
          };
        });
        bodyEl.querySelector('#dspEuthSubmit').onclick = async () => {
          const petId = bodyEl.querySelector('#dspEuthPet').value;
          const date = bodyEl.querySelector('#dspEuthDate').value;
          const reason = bodyEl.querySelector('#dspEuthReason').value.trim();
          const condition = bodyEl.querySelector('#dspEuthCondition').value.trim();
          const chip = bodyEl.querySelector('#dspEuthKind span.on');
          const kind = chip ? chip.dataset.v : 'euthanasia';
          if (!petId) { TNR_UI.toast('请选择动物', 'warning'); return; }
          if (!date) { TNR_UI.toast('请选择处置日期', 'warning'); return; }
          if (!reason) { TNR_UI.toast('请填写原因', 'warning'); return; }
          const btn = bodyEl.querySelector('#dspEuthSubmit');
          btn.disabled = true;
          try {
            const res = await TNR_API.createEuthanasia({
              pet_id: Number(petId), kind: kind, reason: reason,
              condition: condition, euthanized_at: date
            });
            TNR_UI.assertOk(res, '登记失败');
            TNR_UI.toast('已登记，请在列表中继续办理死亡出库单；出库完成才算出院', 'success');
            closeFn();
            this.render_discharge();
          } catch (e) {
            TNR_UI.toast(e.message || '登记失败', 'danger');
            btn.disabled = false;
          }
        };
      }
    });
  },

  dspOpenEuthOutbound(id) {
    const e = ((this._dsp && this._dsp.euth) || [])
      .find(x => String(x.id) === String(id));
    if (!e) { TNR_UI.toast('死亡记录已刷新，请重试', 'warning'); return; }
    if (e.outbound_at) { TNR_UI.toast('该记录已完成死亡出库', 'warning'); return; }
    TNR_UI.modal({
      title: '死亡出库单 · ' + (e.pet_code || ''),
      size: 'lg',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="tx-tip inf"><span>ℹ</span><div>'
          + '死亡出库单是遗体交给无害化处理厂的交接凭证；照片为非必需。</div></div>'
          + '<div class="dsp-form">'
          + '<div class="dsp-fld"><label>出库日期<span class="req">*</span></label>'
          + '<input class="form-input" type="date" id="dspEoDate" value="' + TNR_UI.todayStr() + '"></div>'
          + '<div class="dsp-fld"><label>交接人<span class="req">*</span></label>'
          + '<input class="form-input" id="dspEoHandover" value="' + this.escape(this.dspStaffName()) + '"'
          + ' placeholder="医院交接人"></div>'
          + '<div class="dsp-fld"><label>接收机构（无害化处理厂）<span class="req">*</span></label>'
          + '<input class="form-input" id="dspEoOrg" placeholder="如：XX无害化处理厂"></div>'
          + '<div class="dsp-fld"><label>接收人<span class="req">*</span></label>'
          + '<input class="form-input" id="dspEoReceiver" placeholder="接收人姓名"></div>'
          + '<div class="dsp-fld full"><label>原因<span class="req">*</span></label>'
          + '<textarea class="form-textarea" id="dspEoReason" rows="2">'
          + this.escape(e.reason || '') + '</textarea></div>'
          + '<div class="dsp-fld full"><label>接收人签字<span class="req">*</span></label>'
          + '<div class="sig-field" id="dspEoSig"></div></div>'
          + this.dspPhotoPickHtml('eo', '出库照片（非必需）',
            '手机可直接拍照，或从相册 / 本地文件选择；选图后自动压缩')
          + '</div>'
          + '<div class="dsp-actions">'
          + '<button type="button" class="btn btn-secondary" data-close>取消</button>'
          + '<button type="button" class="btn btn-primary" id="dspEoSubmit">确认出库</button>'
          + '</div>';
        const sig = TNR_UI.initSignatureField(bodyEl.querySelector('#dspEoSig'),
          {title: '接收人签字'});
        let eoPhoto = null;
        this.dspBindPhotoPick(bodyEl, 'eo', f => { eoPhoto = f; });
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelector('#dspEoSubmit').onclick = async () => {
          const date = bodyEl.querySelector('#dspEoDate').value;
          const handover = bodyEl.querySelector('#dspEoHandover').value.trim();
          const org = bodyEl.querySelector('#dspEoOrg').value.trim();
          const receiver = bodyEl.querySelector('#dspEoReceiver').value.trim();
          const reason = bodyEl.querySelector('#dspEoReason').value.trim();
          if (!date) { TNR_UI.toast('请选择出库日期', 'warning'); return; }
          if (!handover) { TNR_UI.toast('请填写交接人', 'warning'); return; }
          if (!org) { TNR_UI.toast('请填写接收机构（无害化处理厂）', 'warning'); return; }
          if (!receiver) { TNR_UI.toast('请填写接收人', 'warning'); return; }
          if (!reason) { TNR_UI.toast('请填写原因', 'warning'); return; }
          if (sig.isEmpty()) { TNR_UI.toast('请完成接收人签字', 'warning'); return; }
          const fd = new FormData();
          fd.append('outbound_at', date);
          fd.append('handover_name', handover);
          fd.append('receiving_org', org);
          fd.append('receiver_name', receiver);
          fd.append('reason', reason);
          fd.append('signature', sig.getDataURL());
          if (eoPhoto) fd.append('photo', eoPhoto);
          const btn = bodyEl.querySelector('#dspEoSubmit');
          btn.disabled = true;
          try {
            const res = await TNR_API.outboundEuthanasia(id, fd);
            TNR_UI.assertOk(res, '死亡出库失败');
            TNR_UI.toast('死亡出库登记成功', 'success');
            closeFn();
            this.render_discharge();
          } catch (err) {
            TNR_UI.toast(err.message || '出库失败', 'danger');
            btn.disabled = false;
          }
        };
      }
    });
  },

  dspViewEuth(id) {
    const e = ((this._dsp && this._dsp.euth) || [])
      .find(x => String(x.id) === String(id));
    if (!e) { TNR_UI.toast('死亡记录已刷新，请重试', 'warning'); return; }
    const photo = e.photo || '';
    TNR_UI.drawer({
      title: (e.kind === 'dead' ? '死亡 / 无害化' : '安乐死') + ' · ' + this.escape(e.pet_code || ''),
      body: '<div class="detail-section"><div class="detail-section-title">处置信息</div>'
        + TNR_UI.renderDescList([
          {label: '动物', value: this.dspPetCell(this.dspPet(e.pet_id), e.pet_code), full: true},
          {label: '单据编号', value: this.escape(e.ledger_no || '—')},
          {label: '处置方式', value: e.kind === 'dead' ? '死亡 / 无害化' : '安乐死'},
          {label: '处置日期', value: this.formatDate(e.euthanized_at)},
          {label: '原因', value: this.escape(e.reason || '—'), full: true},
          {label: '动物状况', value: this.escape(e.condition || '—'), full: true},
          {label: '出库日期', value: this.formatDate(e.outbound_at)},
          {label: '交接人', value: this.escape(e.handover_name || '—')},
          {label: '接收机构', value: this.escape(e.receiving_org || '—')},
          {label: '接收人', value: this.escape(e.receiver_name || '—')},
          {label: '操作人', value: this.escape(e.operator_name || '—')}
        ])
        + '<div class="detail-section-title" style="margin-top:var(--sp-3);">接收人签字</div>'
        + this.dspSigTag(e.signature)
        + '<div class="detail-section-title" style="margin-top:var(--sp-3);">出库照片</div>'
        + this.dspImgTag(photo, '出库照片')
        + '</div>' + TNR_UI.printDocBtn('euthanasia', e.id)
    });
  },

  /* ---------- 主人领回交接单 ---------- */
  dspOpenOwnerCreate() {
    const eligible = this.dspOwnerPets();
    if (!eligible.length) {
      TNR_UI.toast('暂无可领回的动物（需诊疗中或待领养）', 'warning');
      return;
    }
    TNR_UI.modal({
      title: '主人领回 · 交接单',
      size: 'lg',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="tx-tip inf"><span>ℹ</span><div>'
          + '核对主人身份与联系方式，填写交接信息并由主人签字。领回后动物状态变为「已领回」。</div></div>'
          + '<div class="dsp-form">'
          + '<div class="dsp-fld full"><label>动物<span class="req">*</span></label>'
          + '<select class="form-select" id="dspOrPet"><option value="">请选择</option>'
          + eligible.map(p => '<option value="' + p.id + '">' + this.escape(p.code)
            + (p.name ? ' · ' + this.escape(p.name) : '') + '（'
            + TNR_UI.petStatusText(p.status) + '）</option>').join('')
          + '</select></div>'
          + '<div class="dsp-fld"><label>主人姓名<span class="req">*</span></label>'
          + '<input class="form-input" id="dspOrName"></div>'
          + '<div class="dsp-fld"><label>联系电话<span class="req">*</span></label>'
          + '<input class="form-input" id="dspOrPhone"></div>'
          + '<div class="dsp-fld"><label>身份证号</label>'
          + '<input class="form-input" id="dspOrIdCard"></div>'
          + '<div class="dsp-fld"><label>领回日期<span class="req">*</span></label>'
          + '<input class="form-input" type="date" id="dspOrDate" value="' + TNR_UI.todayStr() + '"></div>'
          + '<div class="dsp-fld full"><label>住址</label>'
          + '<input class="form-input" id="dspOrAddress"></div>'
          + '<div class="dsp-fld full"><label>领回原因<span class="req">*</span></label>'
          + '<textarea class="form-textarea" id="dspOrReason" rows="2"></textarea></div>'
          + '<div class="dsp-fld full"><label>主人签字<span class="req">*</span></label>'
          + '<div class="sig-field" id="dspOrSig"></div></div>'
          + '</div>'
          + '<div class="dsp-actions">'
          + '<button type="button" class="btn btn-secondary" data-close>取消</button>'
          + '<button type="button" class="btn btn-primary" id="dspOrSubmit">确认领回</button>'
          + '</div>';
        const sig = TNR_UI.initSignatureField(bodyEl.querySelector('#dspOrSig'),
          {title: '主人签字'});
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelector('#dspOrSubmit').onclick = async () => {
          const petId = bodyEl.querySelector('#dspOrPet').value;
          const name = bodyEl.querySelector('#dspOrName').value.trim();
          const phone = bodyEl.querySelector('#dspOrPhone').value.trim();
          const idCard = bodyEl.querySelector('#dspOrIdCard').value.trim();
          const date = bodyEl.querySelector('#dspOrDate').value;
          const address = bodyEl.querySelector('#dspOrAddress').value.trim();
          const reason = bodyEl.querySelector('#dspOrReason').value.trim();
          if (!petId) { TNR_UI.toast('请选择动物', 'warning'); return; }
          if (!name) { TNR_UI.toast('请填写主人姓名', 'warning'); return; }
          if (!phone) { TNR_UI.toast('请填写联系电话', 'warning'); return; }
          if (!date) { TNR_UI.toast('请选择领回日期', 'warning'); return; }
          if (!reason) { TNR_UI.toast('请填写领回原因', 'warning'); return; }
          if (sig.isEmpty()) { TNR_UI.toast('请完成主人签字', 'warning'); return; }
          const btn = bodyEl.querySelector('#dspOrSubmit');
          btn.disabled = true;
          try {
            let bl = null;
            try {
              const r = await TNR_API.checkBlacklist(idCard, phone);
              bl = r && r.data;
            } catch (err) {
              TNR_UI.toast('黑名单校验失败，请重试：' + (err.message || '未知错误'), 'danger');
              btn.disabled = false;
              return;
            }
            if (bl && (bl.name || bl.idCard)) {
              TNR_UI.toast('该主人在黑名单中，无法办理领回：' + (bl.reason || ''), 'danger');
              btn.disabled = false;
              return;
            }
            const res = await TNR_API.createOwnerReturn({
              pet_id: Number(petId), owner_name: name, owner_phone: phone,
              owner_id_card: idCard, owner_address: address, reason: reason,
              return_time: date, signature: sig.getDataURL()
            });
            TNR_UI.assertOk(res, '领回办理失败');
            TNR_UI.toast('主人领回登记成功，已计入出院', 'success');
            closeFn();
            this.render_discharge();
          } catch (err) {
            TNR_UI.toast(err.message || '办理失败', 'danger');
            btn.disabled = false;
          }
        };
      }
    });
  },

  dspViewOwner(id) {
    const r = ((this._dsp && this._dsp.returns) || [])
      .find(x => String(x.id) === String(id));
    if (!r) { TNR_UI.toast('领回记录已刷新，请重试', 'warning'); return; }
    TNR_UI.drawer({
      title: '主人领回单 · ' + this.escape(r.pet_code || ''),
      body: '<div class="detail-section"><div class="detail-section-title">交接信息</div>'
        + TNR_UI.renderDescList([
          {label: '动物', value: this.dspPetCell(this.dspPet(r.pet_id), r.pet_code), full: true},
          {label: '单据编号', value: this.escape(r.ledger_no || '—')},
          {label: '主人姓名', value: this.escape(r.owner_name || '—')},
          {label: '联系电话', value: this.escape(r.owner_phone || '—')},
          {label: '身份证号', value: this.escape(r.owner_id_card || '—')},
          {label: '住址', value: this.escape(r.owner_address || '—'), full: true},
          {label: '领回时间', value: this.formatDateTime(TNR_UI.firstDate(r.return_time, r.created_at))},
          {label: '领回原因', value: this.escape(r.reason || '—'), full: true},
          {label: '操作人', value: this.escape(r.operator_name || '—')}
        ])
        + '<div class="detail-section-title" style="margin-top:var(--sp-3);">主人签字</div>'
        + this.dspSigTag(r.signature)
        + '</div>' + TNR_UI.printDocBtn('owner_return', r.id)
    });
  },

  /* ---------- 运回单（放归反向物流） ---------- */
  dspOpenReturnCreate() {
    const picked = Array.from(
      document.querySelectorAll('#dspPickList input[data-petpick]:checked'))
      .map(x => Number(x.value));
    if (!picked.length) {
      TNR_UI.toast('请先勾选待放归动物', 'warning');
      return;
    }
    const pets = picked.map(id => this.dspPet(id)).filter(Boolean);
    if (pets.length !== picked.length) {
      TNR_UI.toast('部分动物档案已刷新，请重新勾选', 'warning');
      return;
    }
    const shelves = (this._dsp && this._dsp.returnShelters) || [];
    if (!shelves.length) {
      TNR_UI.toast('未加载到可用的捕捉点，请刷新页面后重试', 'warning');
      return;
    }
    /* 默认选中：全部动物原本属于同一个捕捉点时沿用该捕捉点；
       来自不同捕捉点时留给操作员选（可选市级）。 */
    const petShelterIds = Array.from(new Set(pets.map(p => String(p.shelter_id || ''))));
    let shelterId = petShelterIds.length === 1 ? petShelterIds[0] : '';
    if (!shelves.some(s => String(s.id) === shelterId)) {
      shelterId = String(shelves[0].id);
    }
    const noteEl = document.getElementById('dspReturnNote');
    const note = noteEl ? noteEl.value.trim() : '';

    TNR_UI.modal({
      title: '生成运回单',
      size: 'lg',
      body: (bodyEl, closeFn) => {
        bodyEl.innerHTML = '<div class="tx-tip inf"><span>ℹ</span><div>'
          + '运回单提交后为「待确认」，动物状态变为「待运回捕捉点」。'
          + '捕捉点签收后进入其待放归列表；签收前医院可以撤回。</div></div>'
          + '<div class="dsp-fld full"><label>接收捕捉点<span class="req">*</span></label>'
          + '<select class="form-select" id="dspReturnShelter">'
          + shelves.map(s => '<option value="' + s.id + '"'
            + (String(s.id) === shelterId ? ' selected' : '') + '>'
            + this.escape(s.name) + (s.is_city ? '（市级）' : '（' + this.escape(s.district_name || '') + '）')
            + '</option>').join('')
          + '</select></div>'
          + '<div class="dsp-hint">可选市级捕捉点，或本院所在区县的捕捉点；不能选其他区县。</div>'
          + '<div class="dsp-return-pets">' + pets.map(p =>
            '<div class="dsp-return-pet"><b>' + this.escape(p.code) + '</b>'
            + '<span>' + this.escape(p.name || '') + '</span></div>').join('') + '</div>'
          + (note ? '<div class="dsp-note">备注：' + this.escape(note) + '</div>' : '')
          + '<div class="dsp-actions">'
          + '<button type="button" class="btn btn-secondary" data-close>取消</button>'
          + '<button type="button" class="btn btn-primary" id="dspReturnSubmit">确认生成</button>'
          + '</div>';
        bodyEl.querySelector('[data-close]').onclick = closeFn;
        bodyEl.querySelector('#dspReturnSubmit').onclick = async () => {
          const btn = bodyEl.querySelector('#dspReturnSubmit');
          const shelterSelect = bodyEl.querySelector('#dspReturnShelter');
          if (!shelterSelect.value) {
            TNR_UI.toast('请选择接收捕捉点', 'warning');
            return;
          }
          btn.disabled = true;
          try {
            const res = await TNR_API.createReturnOrder({
              pet_ids: picked,
              note: note,
              shelter_id: Number(shelterSelect.value)
            });
            TNR_UI.assertOk(res, '运回单创建失败');
            TNR_UI.toast('运回单已提交，等待捕捉点确认接收', 'success');
            closeFn();
            this.render_discharge();
          } catch (e) {
            TNR_UI.toast(e.message || '创建失败', 'danger');
            btn.disabled = false;
          }
        };
      }
    });
  },

  async dspWithdrawReturn(id) {
    const ok = await TNR_UI.confirm(
      '确定撤回该运回单？撤回后动物回到「待领养」，可重新选择出院去向。',
      '撤回运回单');
    if (!ok) return;
    try {
      const res = await TNR_API.withdrawReturnOrder(id);
      TNR_UI.assertOk(res, '撤回失败');
      TNR_UI.toast('运回单已撤回，动物已回到待领养', 'success');
      this.render_discharge();
    } catch (e) {
      TNR_UI.toast(e.message || '撤回失败', 'danger');
    }
  },

  dspViewReturnOrder(id) {
    const o = ((this._dsp && this._dsp.returnOrders) || [])
      .find(x => String(x.id) === String(id));
    if (!o) { TNR_UI.toast('运回单已刷新，请重试', 'warning'); return; }
    const pets = o.pets || [];
    const petHtml = pets.length
      ? '<div class="dsp-return-pets">' + pets.map(p =>
        '<div class="dsp-return-pet"><b>' + this.escape(p.code) + '</b>'
        + '<span>' + this.escape(p.name || '') + '</span>'
        + '<span class="badge badge-default">' + this.escape(p.status_display || p.status || '') + '</span>'
        + '</div>').join('') + '</div>'
      : '<div class="text-muted">' + this.escape(o.pet_codes || '—') + '</div>';

    TNR_UI.drawer({
      title: '运回单 · ' + this.escape(o.ledger_no || ''),
      body: '<div class="detail-section"><div class="detail-section-title">单据信息</div>'
        + TNR_UI.renderDescList([
          {label: '医院', value: this.escape(o.hospital_name || '—')},
          {label: '捕捉点', value: this.escape(o.shelter_name || '—')},
          {label: '状态', value: TNR_UI.returnOrderStatusBadge(o.status)},
          {label: '动物数量', value: (o.pet_count || pets.length || 0) + ' 只'},
          {label: '创建时间', value: this.formatDateTime(o.created_at)},
          {label: '接收时间', value: this.formatDate(o.received_at)},
          {label: '接收人', value: this.escape(o.receiver_name || '—')},
          {label: '接收人电话', value: this.escape(o.receiver_phone || '—')},
          {label: '备注', value: this.escape(o.note || '—'), full: true},
          {label: '驳回原因', value: this.escape(o.reject_reason || '—'), full: true}
        ]) + '</div>'
        + '<div class="detail-section"><div class="detail-section-title">动物清单</div>'
        + petHtml + '</div>'
        + '<div class="detail-section"><div class="detail-section-title">捕捉点接收人签字</div>'
        + this.dspSigTag(o.signature) + '</div>'
        + TNR_UI.printDocBtn('return_order', o.id)
    });
  },

  /* ========== 5. 物料库存管理 ========== */
  async render_material() {
    const page = document.getElementById('page-material');
    page.innerHTML = '<div class="loading-state">加载中...</div>';

    /* 严格读：物料/流水是整页的**唯一数据源**，失败必须说出来。
       静默读返回 `[]`，这一页会渲染成「六个页签全是 0、台账暂无记录」——
       与「本院真的没有物料」在界面上完全一样。 */
    let txns;
    let materials;
    try {
      [txns, materials] = await Promise.all([
        TNR_API.getMaterialTransactionsStrict(),
        TNR_API.getMaterialsStrict()
      ]);
    } catch (e) {
      page.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚠️</div>'
        + '<div class="empty-state-text">物料数据加载失败</div>'
        + '<div class="empty-state-desc">' + this.escape(e.message || '请稍后重试') + '</div>'
        + '<button type="button" class="btn btn-secondary mt-4" id="materialReloadBtn">重新加载</button></div>';
      const retry = page.querySelector('#materialReloadBtn');
      if (retry) retry.onclick = () => this.render_material();
      return;
    }
    const pending = txns.filter(t => t.type === 'dispatch' && !(t.note || '').includes('[已签收]')).length;
    const adjustments = txns.filter(t => t.type === 'adjustment').length;
    // 出库单计数（报废/退回/调拨/盘点）。与异动**分开计数**：
    // 两者都是「减少库存」，但异动是纠账、出库是货物离库，
    // 台账要能分别按 `adjustment` / `outbound` 汇总。
    const outbounds = txns.filter(t => t.type === 'outbound').length;

    page.innerHTML = '<div class="tabs">' +
      '<div class="tab-item ' + (this.state.materialTab === 'pending' ? 'active' : '') + '" data-mtab="pending">待接收物料 (' + pending + ')</div>' +
      '<div class="tab-item ' + (this.state.materialTab === 'stock' ? 'active' : '') + '" data-mtab="stock">实时库存</div>' +
      '<div class="tab-item ' + (this.state.materialTab === 'chip' ? 'active' : '') + '" data-mtab="chip">芯片库存</div>' +
      '<div class="tab-item ' + (this.state.materialTab === 'adjustment' ? 'active' : '') + '" data-mtab="adjustment">库存异动 (' + adjustments + ')</div>' +
      // 「出库报备」而不是「出库」：捕捉点端已有一个叫「下发出库」的页签，
      // 那是把货发给别的机构（`dispatch`），与这里的报废/退回/调拨/盘点
      // 是两码事。名字相近会让操作员点错页签，而两个表单的字段完全不同。
      '<div class="tab-item ' + (this.state.materialTab === 'outboundDecl' ? 'active' : '') + '" data-mtab="outboundDecl">出库报备 (' + outbounds + ')</div>' +
      '<div class="tab-item ' + (this.state.materialTab === 'ledger' ? 'active' : '') + '" data-mtab="ledger">出入库台账 (' + txns.length + ')</div>' +
      '</div>' +
      // 五个标签页共用一条筛选条，位置紧跟 .tabs（CSS 里 .tabs 钉 top:0、.filter-bar 钉 top:46px）。
      TNR_UI.renderFilterBar([
        { key: 'keyword', label: '搜索', type: 'search', placeholder: '物料 / 单号 / 批号 / 操作人 / 芯片号' },
        { key: 'start_date', label: '开始日期', type: 'date' },
        { key: 'end_date', label: '结束日期', type: 'date' }
      ], '', { id: 'materialFilterBar' }) +
      '<div id="materialTabContent"></div>';

    page.querySelectorAll('[data-mtab]').forEach(el => {
      el.addEventListener('click', () => {
        this.state.materialTab = el.dataset.mtab;
        page.querySelectorAll('.tab-item').forEach(t => {
          t.classList.toggle('active', t.dataset.mtab === this.state.materialTab);
        });
        this.renderMaterialTab(txns, materials);
      });
    });

    this._matCache = txns;
    /* 物料主数据一次拉齐，页签切换/筛选只做本地过滤。
       原先「库存异动」「出库报备」各自在每次筛选时重拉一遍物料 ——
       在搜索框里每敲一个字就多一次请求，且失败被空 catch 吞掉。 */
    this._matMaterials = materials;
    this.renderMaterialTab(txns, materials);
    TNR_UI.bindFilter(() => this.renderMaterialTab(this._matCache || [], this._matMaterials || []), page);
  },

  /* 物料管理页四个标签页共用一条筛选条（#materialFilterBar），统一在这里取值与判定。
     日期字段各页不同（date 为主），故 inRange 按值传入。 */
  _matQuery() {
    const bar = document.getElementById('materialFilterBar');
    const v = TNR_UI.getFilterValues(bar);
    return {
      ok: (hay) => !v.keyword || String(hay || '').toLowerCase().includes(v.keyword),
      inRange: (d) => TNR_UI.inDateRange(d, v.start_date, v.end_date)
    };
  },

  async renderMaterialTab(txns, materials) {
    const c = document.getElementById('materialTabContent');
    if (!c) return;
    if (this.state.materialTab === 'pending') this.renderMaterialPending(c, txns);
    else if (this.state.materialTab === 'stock') this.renderMaterialStock(c, materials);
    else if (this.state.materialTab === 'chip') this.renderMaterialChip(c);
    else if (this.state.materialTab === 'adjustment') this.renderMaterialAdjustment(c, txns, materials);
    else if (this.state.materialTab === 'outboundDecl') this.renderMaterialOutbound(c, txns, materials);
    else if (this.state.materialTab === 'ledger') this.renderMaterialLedger(c, txns);
  },

  renderMaterialPending(c, txns) {
    const q = this._matQuery();
    const dispatches = txns.filter(t => t.type === 'dispatch' && !(t.note || '').includes('[已签收]'))
      .filter(t => q.inRange(t.date)
        && q.ok([t.material_name, t.ledger_no, t.batch_no, t.operator_name].join(' ')));
    if (dispatches.length === 0) {
      c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📦</div><div class="empty-state-text">暂无待接收物料</div><div class="empty-state-desc">所有下发的物料已确认接收</div></div>';
      return;
    }
    const rows = dispatches.map(t => {
      return {
        _mat: '<div style="font-weight:500;">' + this.escape(t.material_name || '') + '</div><div style="font-size:var(--fs-xs);color:var(--ink-light);">' + this.escape(t.ledger_no || '') + '</div>',
        _qty: (t.quantity || 0) + ' ' + this.escape(t.unit || ''),
        batch_no: t.batch_no || '—',
        operator_name: t.operator_name || '',
        date: t.date,
        _actions: '<button class="btn btn-success btn-sm" data-mreceive="' + t.id + '">确认入库</button>',
      };
    });
    TNR_UI.mountTable(c, { id: 'hospital.material.pending', columns: [
        { title: '物料名称', key: '_mat', sortable: false },
        { title: '数量', key: '_qty', sortable: false },
        { title: '批号', key: 'batch_no' },
        { title: '下发方', key: 'operator_name' },
        { title: '下发日期', key: 'date' },
        { title: '操作', key: '_actions', sortable: false },
      ], data: rows, emptyText: '暂无待接收物料' });
    TNR_UI.delegateClick(c, {
      'data-mreceive': (id) => this.confirmMaterialReceive(id)
    });
  },

  renderMaterialStock(c, materialsAll) {
    const q = this._matQuery();
    const allMats = materialsAll || [];
    const materials = allMats.filter(m =>
      q.ok([m.name, m.specification, m.batch_no, m.category].join(' ')));
    if (allMats.length === 0) {
      c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📦</div><div class="empty-state-text">暂无物料</div></div>';
      return;
    }
    if (materials.length === 0) {
      c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🔍</div><div class="empty-state-text">没有符合筛选条件的物料</div></div>';
      return;
    }
    const cards = materials.map(m => {
      const stock = m.hospital_stock || 0;
      const safety = m.safety_stock || 1;
      const isLow = stock < safety;
      const pct = Math.min(100, (stock / (safety * 2)) * 100);
      const color = isLow ? 'var(--rouge)' : (stock < safety * 1.5 ? 'var(--autumn-gold)' : 'var(--bamboo-green)');
      return '<div class="card"><div class="card-body"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--sp-3);">' +
        '<div><div style="font-weight:600;font-size:var(--fs-lg);">' + this.escape(m.name) + '</div><div style="font-size:var(--fs-sm);color:var(--ink-muted);">' + TNR_API.getMaterialCategoryText(m.category) + ' · ' + this.escape(m.specification || '') + '</div></div>' +
        '<span class="badge ' + (isLow ? 'badge-danger' : 'badge-success') + '">' + (isLow ? '库存不足' : '正常') + '</span></div>' +
        '<div style="display:flex;align-items:center;gap:var(--sp-4);"><div style="flex:1;">' +
        '<div class="stock-bar"><div class="stock-bar-fill" style="width:' + pct + '%;background:' + color + ';"></div></div>' +
        '<div style="display:flex;justify-content:space-between;margin-top:4px;font-size:var(--fs-xs);color:var(--ink-light);"><span>当前库存</span><span>安全库存</span></div></div>' +
        '<div style="text-align:right;"><div style="font-family:var(--font-display);font-size:var(--fs-2xl);font-weight:700;color:' + color + ';">' + stock + '</div><div style="font-size:var(--fs-xs);color:var(--ink-light);">/ ' + safety + ' ' + this.escape(m.unit || '') + '</div></div></div>' +
        '<div class="desc-list mt-4"><div class="desc-item"><div class="desc-label">供应商</div><div class="desc-value">' + this.escape(m.supplier || '—') + '</div></div>' +
        '<div class="desc-item"><div class="desc-label">批号</div><div class="desc-value">' + this.escape(m.batch_no || '—') + '</div></div>' +
        '<div class="desc-item"><div class="desc-label">有效期</div><div class="desc-value">' + this.formatDate(m.expiry_date) + '</div></div></div>' +
        // B5：批次行内展开。主表那对批号/效期只是「最近一次入库」的快照 ——
        // 老批次还剩多少、是否临期，点开批次明细才看得到（展开逻辑在 TNR_UI）。
        '<div style="margin-top:var(--sp-3);"><button type="button" class="btn btn-outline btn-sm" data-batchcard="' + m.id + '">批次明细</button>' +
        '<div id="batchbox-' + m.id + '" style="display:none;margin-top:var(--sp-2);"></div></div></div></div>';
    }).join('');
    c.innerHTML = '<div class="stat-grid">' + cards + '</div>';
    // 批次展开走事件委托：卡片由筛选条重渲染，逐卡绑事件会失效。
    // ⚠ 标记防重：#materialTabContent 这个节点跨渲染复用，每次都
    // addEventListener 会让一次点击触发 N 次展开请求。
    if (!c._batchCardBound) {
      c._batchCardBound = true;
      c.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-batchcard]');
        if (!btn) return;
        TNR_UI.toggleCardBatches(btn.dataset.batchcard,
          document.getElementById('batchbox-' + btn.dataset.batchcard));
      });
    }
  },

  async renderMaterialAdjustment(c, txns, materials) {
    materials = materials || [];

    const q = this._matQuery();
    // 异动原因枚举（第五十轮）。key 与 `MaterialTransaction.ADJUSTMENT_REASON_CHOICES`
    // 一一对应；历史行 `reason` 为空，回退显示当时的自由文本 `note`。
    const ADJ_REASON_TEXT = {
      expired: '过期', invalid: '失效', gain: '盘盈',
      loss: '盘亏', return: '退回', other: '其他'
    };
    const adj = txns.filter(t => t.type === 'adjustment')
      .filter(t => q.inRange(t.date)
        && q.ok([t.material_name, t.reason, t.note, t.operator_name].join(' ')));
    let adjHtml = '<div class="empty-state" style="padding:var(--sp-6);"><div class="empty-state-icon">📋</div><div class="empty-state-text">暂无异动记录</div></div>';
    const adjRows = adj.map(t => ({
      material_name: t.material_name || '',
      _reason: ADJ_REASON_TEXT[t.reason] || t.note || '—',
      note: ADJ_REASON_TEXT[t.reason] ? (t.note || '—') : '—',
      // 负数量是盘盈 / 退回入库（增加库存），用 + 显式标出来，
      // 只看数字会以为又扣了一笔。
      _qty: (t.quantity > 0 ? '-' : '+') + Math.abs(t.quantity || 0)
        + ' ' + this.escape(t.unit || ''),
      date: t.date,
      operator_name: t.operator_name || '',
    }));
    if (adj.length > 0) adjHtml = '<div id="hospitalAdjTable"></div>';

    c.innerHTML = '<div class="card"><div class="card-header"><div class="card-title"><span class="card-title-bar"></span>库存异动报备</div></div><div class="card-body">' +
      '<div class="form-section"><div class="form-section-header"><div class="form-section-header-icon">📝</div><div class="form-section-title">新增异动报备</div></div><div class="form-section-body">' +
      '<div class="form-row"><div class="form-group"><label class="form-label">物料<span class="req">*</span></label><select class="form-select" id="adjMaterial"><option value="">请选择</option>' +
      materials.map(m => '<option value="' + m.id + '">' + this.escape(m.name) + ' (当前:' + (m.hospital_stock || 0) + (m.unit || '') + ')</option>').join('') +
      '</select></div><div class="form-group"><label class="form-label">数量<span class="req">*</span></label><input type="number" class="form-input" id="adjQty" value="1" />' +
      '<div class="fs-sm text-muted mt-2">正数 = 扣减库存；<b>负数 = 增加库存</b>（盘盈 / 退回）</div></div>' +
      '<div class="form-group" id="adjBatchGroup"><label class="form-label">批号（可选）</label>' +
      '<select class="form-select" id="adjBatch"><option value="">不指定（按近效期先出）</option></select>' +
      '<div class="fs-sm text-muted mt-2">有库存的批次都会列出；过期报废可直接指定批次，只扣该批。不选则按近效期先出</div></div>' +
      '<div class="form-group" id="adjChipGroup" style="display:none;"><label class="form-label">芯片起始号<span class="req">*</span></label><input class="form-input" id="adjChipStart" placeholder="如 042060107000177" inputmode="numeric"><div class="fs-sm text-muted mt-2">按「首号 + 数量」作废连续号码的芯片，作废后不可恢复</div>'
      + '<div class="mt-2" id="adjChipSegments"></div>'
      + '<div class="fs-sm mt-2" id="adjChipPreview"><span class="text-muted">填写起始号后，这里会预览将要作废的号码范围</span></div></div>' +
      // B5：提交前预览「会扣哪几批」—— 异动正是处理过期物料的出口，
      // 预览不跳过过期批次（txn_type='adjustment'），操作员能明确看到
      // 报废的就是那批过期的。
      '<div class="form-group"><div class="fs-sm mt-2" id="adjBatchHint"><span class="text-muted">填写数量后，这里会预览将要扣减的批次</span></div></div>' +
      '<div class="form-group"><label class="form-label">异动原因<span class="req">*</span></label>' +
      '<select class="form-select" id="adjReason"><option value="">请选择</option>' +
      '<option value="expired">过期</option><option value="invalid">失效</option>' +
      '<option value="gain">盘盈</option><option value="loss">盘亏</option>' +
      '<option value="return">退回</option><option value="other">其他</option></select>' +
      '<div class="fs-sm text-muted mt-2">过期 / 失效 / 盘亏填正数；盘盈 / 退回填负数</div></div>' +
      '<div class="form-group"><label class="form-label">备注</label><textarea class="form-textarea" id="adjNote" placeholder="补充说明（可选）"></textarea></div>' +
      '<div style="display:flex;justify-content:flex-end;"><button class="btn btn-primary" id="adjSubmitBtn">提交报备</button></div></div></div></div></div>' +
      '<div class="card mt-5"><div class="card-body-flush">' + adjHtml + '</div></div>';

    if (adj.length > 0) {
      TNR_UI.mountTable('hospitalAdjTable', { id: 'hospital.material.adjustment', columns: [
          { title: '物料', key: 'material_name' },
          { title: '原因', key: '_reason' },
          { title: '数量', key: '_qty', sortable: false },
          { title: '备注', key: 'note' },
          { title: '日期', key: 'date' },
          { title: '操作人', key: 'operator_name' },
        ], data: adjRows, emptyText: '暂无异动记录' });
    }
    // 异动数量可正可负：默认步进 `min=1` 会让负数根本走不到。
    TNR_UI.bindQtyStepper('adjQty', { min: -99999 });
    // 芯片物料按号码作废（「首号 + 数量」口径）：选到芯片时才亮出起始号，
    // 否则后端 `chip_batch_void()` 拿不到号码直接 400 —— 而界面上
    // 原来没有任何地方能填，芯片报废这条路完全走不通。
    // 作废是**不可恢复**操作，void 预检在提交前逐颗核对；
    // 号段摘要与捕捉点端同一份实现（TNR_UI），两端口径不会漂移。
    const adjPreview = TNR_UI.bindChipPreview('void', 'adjChipStart', 'adjQty', 'adjChipPreview',
      () => ({ material_id: document.getElementById('adjMaterial').value }));
    const adjToggleChip = () => {
      const m = materials.find(x => String(x.id) === String(document.getElementById('adjMaterial').value));
      const isChip = !!(m && m.category === 'chip');
      document.getElementById('adjChipGroup').style.display = isChip ? '' : 'none';
      if (isChip) {
        TNR_UI.renderChipSegments('adjChipSegments', m.id, (start) => {
          document.getElementById('adjChipStart').value = start;
          if (adjPreview) adjPreview.rerun();
        });
      } else {
        document.getElementById('adjChipSegments').innerHTML = '';
      }
    };
    // B5：批次扣减预览（adjustment 不跳过过期批次 —— 异动正是过期物料的出口）
    const adjBatchPreview = TNR_UI.bindBatchPreview('adjQty', 'adjBatchHint',
      () => document.getElementById('adjMaterial')?.value || null, 'adjustment',
      () => document.getElementById('adjBatch')?.value || '');
    /* 批次下拉：列出该物料**所有还有余量的批次**，过期批单独标注。
       过期报废往往要按批清理（同一药品不同批号效期不同），只给「不指定」
       会强迫操作员先 FEFO 扣到别的批次；指定批号后服务端只扣该批。 */
    const adjLoadBatches = async () => {
      const m = materials.find(x => String(x.id) === String(document.getElementById('adjMaterial').value));
      const isChip = !!(m && m.category === 'chip');
      const group = document.getElementById('adjBatchGroup');
      const batchEl = document.getElementById('adjBatch');
      if (group) group.style.display = (isChip || !m) ? 'none' : '';
      if (!batchEl) return;
      batchEl.innerHTML = '<option value="">不指定（按近效期先出）</option>';
      if (isChip || !m) return;
      try {
        const res = await TNR_API.getMaterialBatches(m.id);
        const today = TNR_UI.todayStr();
        ((res && res.batches) || []).filter(b => (b.quantity || 0) > 0).forEach(b => {
          const opt = document.createElement('option');
          opt.value = b.batch_no;
          const exp = b.expiry_date ? ' 效期' + b.expiry_date : '';
          const expired = b.expiry_date && b.expiry_date < today ? '（已过期）' : '';
          opt.textContent = b.batch_no + expired + '（余 ' + b.quantity + (m.unit || '') + exp + '）';
          batchEl.appendChild(opt);
        });
      } catch (e) { /* 批次拉取失败不阻塞表单：留空 = 走 FEFO */ }
    };
    c.querySelector('#adjMaterial')?.addEventListener('change', async () => {
      adjToggleChip();
      await adjLoadBatches();
      // 物料/批次切换 → 批次预览重跑（bindBatchPreview 只监听数量输入，
      // 这两个维度的变化由这里驱动）
      if (adjBatchPreview) adjBatchPreview.rerun();
    });
    c.querySelector('#adjBatch')?.addEventListener('change', () => {
      if (adjBatchPreview) adjBatchPreview.rerun();
    });
    c.querySelector('#adjSubmitBtn')?.addEventListener('click', () => this.saveStockAdjustment(materials));
  },

  async saveStockAdjustment(materials) {
    const mid = document.getElementById('adjMaterial').value;
    const qtyRaw = document.getElementById('adjQty').value;
    const qty = parseInt(qtyRaw, 10);
    const reason = document.getElementById('adjReason').value;
    const note = (document.getElementById('adjNote')?.value || '').trim();
    if (!mid) { TNR_UI.toast('请选择物料', 'warning'); return; }
    if (isNaN(qty)) { TNR_UI.toast('请填写异动数量（可填负数表示盘盈 / 退回）', 'warning'); return; }
    if (qty === 0) { TNR_UI.toast('异动数量不能为0', 'warning'); return; }
    if (!reason) { TNR_UI.toast('请选择异动原因', 'warning'); return; }
    // 原因与方向绑定：服务端有同一道判据，这里先挡是为了不让用户白填一次。
    const DEC = ['expired', 'invalid', 'loss'];
    const INC = ['gain', 'return'];
    if (DEC.indexOf(reason) !== -1 && qty < 0) {
      TNR_UI.toast('「' + document.querySelector('#adjReason option:checked').textContent
        + '」表示减少库存，数量必须为正数', 'warning'); return;
    }
    if (INC.indexOf(reason) !== -1 && qty > 0) {
      TNR_UI.toast('「' + document.querySelector('#adjReason option:checked').textContent
        + '」表示增加库存，数量必须为负数', 'warning'); return;
    }
    // 芯片物料：异动 = 按「首号 + 数量」作废号码。负数盘盈不知道恢复哪些
    // 号码，服务端直接拒绝 —— 前端也提前说清楚。
    const mat = (materials || []).find(x => String(x.id) === String(mid));
    const isChip = !!(mat && mat.category === 'chip');
    if (isChip && qty < 0) {
      TNR_UI.toast('芯片异动只支持正数（按号码作废）；盘盈请通过采购 / 入库办理', 'warning');
      return;
    }
    // 空号码先在前端挡一道，后端 `chip_batch_void()` 还有第二道。
    const chipStart = isChip
      ? (document.getElementById('adjChipStart')?.value || '').trim() : '';
    if (isChip && !chipStart) { TNR_UI.toast('请填写芯片起始号', 'warning'); return; }
    // 芯片不认批号定向（号码才是真源），与出库单同口径。
    const batchNo = isChip ? '' : (document.getElementById('adjBatch')?.value || '');

    try {
      const res = await TNR_API.adjustStock({
        material_id: parseInt(mid), quantity: qty, reason: reason, note: note,
        chip_start: chipStart, batch_no: batchNo
      });
      if (res.success) {
        TNR_UI.toast(res.message || '异动报备已提交', 'success');
        this.render_material();
      } else {
        TNR_UI.toast(res.message || '提交失败', 'danger');
      }
    } catch (e) {
      TNR_UI.toast(e.message || '操作失败', 'danger');
    }
  },

  /* ---------- 物料出库（报废 / 退回 / 调拨 / 盘点） ----------
   *
   * 与「库存异动」的差异，前端必须**照单呈现**，不能合并成一个表单：
   *   ① 出库原因四选一（报废/退回/调拨/盘点），异动是六类原因；
   *   ② 出库可以选批号（定向扣减），异动仅药品可按批；
   *   ③ 出库数量与异动都可为负（盘盈 / 退回入库），但方向与原因的对应关系不同。
   * 这些都是服务端判据，前端只负责「让用户填得对」—— 判据不在这里重写。
   */
  async renderMaterialOutbound(c, txns, materials) {
    materials = materials || [];

    const q = this._matQuery();
    const outRows = (txns || []).filter(t => t.type === 'outbound')
      .filter(t => q.inRange(t.date)
        && q.ok([t.material_name, t.reason, t.note, t.operator_name].join(' ')));
    const REASON_TEXT = { scrap: '报废', return: '退回', transfer: '调拨', inventory: '盘点' };
    const listHtml = outRows.length > 0
      ? '<div id="hospitalOutboundTable"></div>'
      : '<div class="empty-state" style="padding:var(--sp-6);"><div class="empty-state-icon">📤</div><div class="empty-state-text">暂无关库记录</div></div>';

    c.innerHTML = '<div class="card"><div class="card-header"><div class="card-title"><span class="card-title-bar"></span>物料出库报备</div></div><div class="card-body">' +
      '<div class="form-section"><div class="form-section-header"><div class="form-section-header-icon">📤</div><div class="form-section-title">新增出库</div></div><div class="form-section-body">' +
      '<div class="form-row"><div class="form-group"><label class="form-label">物料<span class="req">*</span></label><select class="form-select" id="outMaterial"><option value="">请选择</option>' +
      materials.map(m => '<option value="' + m.id + '">' + this.escape(m.name) + ' (当前:' + (m.hospital_stock || 0) + (m.unit || '') + ')</option>').join('') +
      '</select></div>' +
      // 数量：**允许负号**。`type="number"` 本身就能输负值，
      // 但 stepper 的 min 不能钉成 1 —— 那会让盘盈根本填不出来。
      '<div class="form-group"><label class="form-label">出库数量<span class="req">*</span></label><input type="number" class="form-input" id="outQty" value="1" />' +
      '<div class="fs-sm text-muted mt-2">正数 = 出库扣减；<b>负数 = 盘盈 / 退回入库</b>（增加库存）</div></div></div>' +
      // 批号：**不选走 FEFO**（近效期先出）。芯片物料不出这个下拉 ——
      // 芯片按号码逐颗管理，批号只是流水上的快照字段。
      '<div class="form-row"><div class="form-group"><label class="form-label">批号（可选）</label><select class="form-select" id="outBatch"><option value="">不指定（按近效期先出）</option></select>' +
      '<div class="fs-sm text-muted mt-2">指定后只扣这一批；该批余量不足会直接报错，不会改扣别的批次</div></div>' +
      '<div class="form-group"><label class="form-label">出库原因<span class="req">*</span></label><select class="form-select" id="outReason"><option value="">请选择</option>' +
      '<option value="scrap">报废</option><option value="return">退回</option>' +
      '<option value="transfer">调拨</option><option value="inventory">盘点</option></select></div></div>' +
      '<div class="form-group" id="outChipGroup" style="display:none;"><label class="form-label">芯片起始号<span class="req">*</span></label><input class="form-input" id="outChipStart" placeholder="如 042060107000177" inputmode="numeric">' +
      '<div class="fs-sm text-muted mt-2">按「首号 + 数量」作废连续号码的芯片，作废后不可恢复</div>' +
      '<div class="mt-2" id="outChipSegments"></div>' +
      '<div class="fs-sm mt-2" id="outChipPreview"><span class="text-muted">填写起始号后，这里会预览将要作废的号码范围</span></div></div>' +
      // 提交前预览「会扣哪几批」—— 出库正是报废过期物料的出口，
      // 预览不跳过过期批次（txn_type='outbound'）。
      '<div class="form-group"><div class="fs-sm mt-2" id="outBatchHint"><span class="text-muted">填写数量后，这里会预览将要扣减的批次</span></div></div>' +
      '<div class="form-group"><label class="form-label">备注</label><textarea class="form-textarea" id="outNote" placeholder="补充说明（可选）"></textarea></div>' +
      '<div style="display:flex;justify-content:flex-end;"><button class="btn btn-primary" id="outSubmitBtn">提交出库</button></div></div></div></div></div>' +
      '<div class="card mt-5"><div class="card-body-flush">' + listHtml + '</div></div>';

    if (outRows.length > 0) {
      TNR_UI.mountTable('hospitalOutboundTable', { id: 'hospital.material.outbound', columns: [
          { title: '物料', key: 'material_name' },
          { title: '批号', key: 'batch_no' },
          { title: '原因', key: '_reason' },
          { title: '数量', key: '_qty', sortable: false },
          { title: '备注', key: 'note' },
          { title: '日期', key: 'date' },
          { title: '操作人', key: 'operator_name' },
        ], data: outRows.map(t => ({
          material_name: t.material_name || '',
          batch_no: t.batch_no || '—',
          _reason: REASON_TEXT[t.reason] || '—',
          // 负数量（盘盈）要显式标成 + 方向：只看数字会以为是出库
          _qty: (t.quantity > 0 ? '-' + t.quantity : '+' + Math.abs(t.quantity))
            + ' ' + this.escape(t.unit || ''),
          note: t.note || '—',
          date: t.date,
          operator_name: t.operator_name || '',
        })), emptyText: '暂无关库记录' });
    }

    // 允许负数：`bindQtyStepper` 缺省 min 会读 input 的 min 属性、
    // 没有就钉成 1 —— 那会让步进按钮走不到负数（手输倒是能进），
    // 而盘盈主要靠步进凑数，等于「填不出来」。
    TNR_UI.bindQtyStepper('outQty', { min: -99999 });
    const outToggleChip = async () => {
      const m = materials.find(x => String(x.id) === String(document.getElementById('outMaterial').value));
      const isChip = !!(m && m.category === 'chip');
      const batchEl = document.getElementById('outBatch');
      const batchGroup = batchEl ? batchEl.closest('.form-group') : null;
      document.getElementById('outChipGroup').style.display = isChip ? '' : 'none';
      // 芯片不认批号定向（号码才是真源）—— 直接藏掉这个下拉，
      // 而不是留一个「填了也不生效」的输入框（比撞 400 更难发现）。
      if (batchGroup) batchGroup.style.display = isChip ? 'none' : '';
      if (isChip) {
        TNR_UI.renderChipSegments('outChipSegments', m.id, (start) => {
          document.getElementById('outChipStart').value = start;
          if (outPreview) outPreview.rerun();
        });
      } else {
        document.getElementById('outChipSegments').innerHTML = '';
      }
      // 物料切换 → 重拉该物料的批次下拉 + 批次预览
      if (batchEl) {
        batchEl.innerHTML = '<option value="">不指定（按近效期先出）</option>';
        if (!isChip && m) {
          try {
            const res = await TNR_API.getMaterialBatches(m.id);
            const rows = (res && res.batches) || [];
            rows.filter(b => (b.quantity || 0) > 0).forEach(b => {
              const opt = document.createElement('option');
              opt.value = b.batch_no;
              const exp = b.expiry_date ? ' 效期' + b.expiry_date : '';
              opt.textContent = b.batch_no + '（余 ' + b.quantity + (m.unit || '') + exp + '）';
              batchEl.appendChild(opt);
            });
          } catch (e) { /* 批次拉取失败不阻塞表单：留空 = 走 FEFO */ }
        }
      }
      if (outBatchPreview) outBatchPreview.rerun();
    };
    const outPreview = TNR_UI.bindChipPreview('void', 'outChipStart', 'outQty', 'outChipPreview',
      () => ({ material_id: document.getElementById('outMaterial').value }));
    const outBatchPreview = TNR_UI.bindBatchPreview('outQty', 'outBatchHint',
      () => document.getElementById('outMaterial')?.value || null, 'outbound',
      () => document.getElementById('outBatch')?.value || '');
    c.querySelector('#outMaterial')?.addEventListener('change', outToggleChip);
    c.querySelector('#outBatch')?.addEventListener('change', () => {
      if (outBatchPreview) outBatchPreview.rerun();
    });
    c.querySelector('#outSubmitBtn')?.addEventListener('click', () => this.saveMaterialOutbound(materials));
  },

  async saveMaterialOutbound(materials) {
    const mid = document.getElementById('outMaterial').value;
    const qtyRaw = document.getElementById('outQty').value;
    const qty = parseInt(qtyRaw, 10);
    const reason = document.getElementById('outReason').value;
    if (!mid) { TNR_UI.toast('请选择物料', 'warning'); return; }
    // ⚠ 数量校验必须**两条都留**：
    //   `isNaN` 挡「空 / 非数字」（前端 parseInt 会给 NaN，直接提交会拿到
    //   服务端一句「出库数量必须是数字」，而用户明明填了东西）；
    //   `qty === 0` 挡「出 0 件」—— 它既不改变库存也不改变批次，
    //   却会生成一条看得见的流水和一张单号。
    if (isNaN(qty)) { TNR_UI.toast('请填写出库数量（可填负数表示盘盈）', 'warning'); return; }
    if (qty === 0) { TNR_UI.toast('出库数量不能为0', 'warning'); return; }
    if (!reason) { TNR_UI.toast('请选择出库原因', 'warning'); return; }
    const mat = (materials || []).find(x => String(x.id) === String(mid));
    const isChip = !!(mat && mat.category === 'chip');
    const chipStart = isChip ? (document.getElementById('outChipStart')?.value || '').trim() : '';
    if (isChip && !chipStart) { TNR_UI.toast('请填写芯片起始号', 'warning'); return; }

    try {
      const res = await TNR_API.outboundMaterial({
        material_id: parseInt(mid, 10), quantity: qty, reason: reason,
        // 芯片不传批号：服务端会忽略它，但传了会让台账上的批号快照
        // 变成用户随手选的那个 —— 芯片的批号快照应由主表决定。
        ...(isChip ? {} : { batch_no: document.getElementById('outBatch')?.value || '' }),
        note: (document.getElementById('outNote')?.value || '').trim(),
        chip_start: chipStart,
      });
      if (res.success) {
        TNR_UI.toast(res.message || '出库登记成功', 'success');
        this.render_material();
      } else {
        // 服务端的拒因（余额不足 / 指定批号余量不足 / 批号不存在）
        // 必须原样显示 —— 这些正是用户在表单里能改的东西。
        TNR_UI.toast(res.message || '提交失败', 'danger');
      }
    } catch (e) {
      TNR_UI.toast(e.message || '操作失败', 'danger');
    }
  },

  /* ---------- 芯片库存（第四十六轮：按号码逐颗管理） ---------- */
  async renderMaterialChip(c) {
    c.innerHTML = '<div class="loading-state">加载中...</div>';
    try {
      const q = this._matQuery();
      // ⚠ `getChips` 走 `getData`，已解包返回 `{total, items}` —— 再取 `.data`
      // 恒为 undefined，芯片库存页会显示「暂无芯片记录」。
      const res = await TNR_API.getChips({});
      const all = (res && res.items) || [];
      const total = (res && res.total) || all.length;
      const chips = all.filter(ch => q.ok([ch.number, ch.materialName, ch.batchNo, ch.petCode].join(' ')));
      if (all.length === 0) {
        c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📡</div><div class="empty-state-text">暂无芯片记录</div><div class="empty-state-desc">芯片随物料采购或下发入库后在此显示</div></div>';
        return;
      }
      if (chips.length === 0) {
        c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🔍</div><div class="empty-state-text">没有符合筛选条件的芯片</div></div>';
        return;
      }
      const rows = chips.map(ch => ({
        number: '<span class="font-mono fs-sm">' + this.escape(ch.number || '') + '</span>',
        _status: TNR_UI.chipStatusBadge(ch.status),
        material_name: ch.materialName || '—',
        batch_no: ch.batchNo || '—',
        pet_code: ch.petCode || '—',
        _used: ch.usedAt ? this.formatDateTime(ch.usedAt) : '—',
        created_at: ch.createdAt || '',
        _op: '<button class="btn btn-outline btn-sm" data-chiptrace="' + this.escape(ch.number) + '">追溯</button>',
      }));
      let html = '';
      if (total > chips.length) {
        html += '<div class="tx-tip inf" style="margin-bottom:var(--sp-3);"><span>ℹ️</span><div>本院共有 <b>' + total + '</b> 颗芯片，当前显示前 ' + chips.length + ' 条。请用筛选条缩小范围。</div></div>';
      }
      html += '<div data-mt="hospital.material.chip"></div>';
      c.innerHTML = html;
      TNR_UI.mountTable('[data-mt="hospital.material.chip"]', { id: 'hospital.material.chip', columns: [
          { title: '芯片号', key: 'number', sortable: false },
          { title: '状态', key: '_status', sortable: false },
          { title: '所属物料', key: 'material_name' },
          { title: '批号', key: 'batch_no' },
          { title: '绑定宠物', key: 'pet_code' },
          { title: '使用时间', key: '_used', sortable: false },
          { title: '入库时间', key: 'created_at', render: r => this.formatDateTime(r.created_at) },
          { title: '操作', key: '_op', sortable: false },
        ], data: rows, emptyText: '暂无芯片记录' });
      // 行内「追溯」走事件委托（mountTable 排序/翻页会整体重建 tbody）；
      // 抽屉本体在 TNR_UI.showChipTrace —— 与捕捉点端同一份实现。
      TNR_UI.delegateClick(c, {
        'data-chiptrace': (number) => TNR_UI.showChipTrace(number)
      }, 'HospitalChipRows');
    } catch (e) {
      c.innerHTML = '<div class="card"><div class="card-body" style="text-align:center;padding:var(--sp-6);">'
        + '<div style="font-size:var(--fs-2xl);">⚠️</div>'
        + '<div style="font-weight:600;margin-top:var(--sp-3);">芯片数据加载失败</div>'
        + '<div class="fs-sm text-muted mt-2">' + this.escape(e.message || '') + '</div></div></div>';
    }
  },

  renderMaterialLedger(c, txns) {
    const q = this._matQuery();
    const data = txns.filter(t => q.inRange(t.date)
        && q.ok([t.ledger_no, t.material_name, t.type, t.operator_name].join(' ')))
      .slice().sort((a, b) => (b.date || '').localeCompare(a.date || ''));
    if (data.length === 0) {
      c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📊</div><div class="empty-state-text">暂无台账记录</div></div>';
      return;
    }
    const rows = data.map(t => {
      // ⚠ 出库的**正负号由数量本身决定**，不能按类型一刀切：
      // 正数出库是扣库存（-），负数盘盈/退回是加库存（+）。
      // 写成「outbound 一律 -」会让盘盈那行显示成扣减，台账方向整个说反。
      const qtyNum = Number(t.quantity) || 0;
      const sign = (t.type === 'consume' || t.type === 'adjustment' || (t.type === 'outbound' && qtyNum > 0)) ? '-' : '+';
      return {
        ledger_no: t.ledger_no || '',
        _type: TNR_UI.materialTxnBadge(t.type, 'long', 'hospital'),
        _typeRaw: t.type,
        material_name: t.material_name || '',
        _qty: sign + (t.quantity || 0) + ' ' + this.escape(t.unit || ''),
        batch_no: t.batch_no || '—',
        from_to: t.from_to || '',
        operator_name: t.operator_name || '',
        date: t.date,
      };
    });
    TNR_UI.mountTable(c, { id: 'hospital.material.ledger', columns: [
        { title: '单据编号', key: 'ledger_no' },
        { title: '类型', key: '_type', sortable: false },
        { title: '物料', key: 'material_name' },
        { title: '数量', key: '_qty', sortable: false },
        { title: '批号', key: 'batch_no' },
        { title: '来源/去向', key: 'from_to' },
        { title: '操作人', key: 'operator_name' },
        { title: '日期', key: 'date' },
      ], data: rows, emptyText: '暂无台账记录' });
  },

  async confirmMaterialReceive(txnId) {
    TNR_UI.confirm('确认接收该批物料？', '确认入库').then(async ok => {
      if (!ok) return;
      try {
        const res = await TNR_API.receiveMaterial(txnId);
        if (res.success) {
          TNR_UI.toast('物料入库成功', 'success');
          this.render_material();
        } else {
          TNR_UI.toast(res.message || '入库失败', 'danger');
        }
      } catch (e) {
        TNR_UI.toast(e.message || '操作失败', 'danger');
      }
    });
  },

  /* ========== 6. 单据台账 ========== */
  async render_ledger() {
    const page = document.getElementById('page-ledger');
    page.innerHTML = '<div class="loading-state">加载中...</div>';

    try {
      const [transfers, treatments, txns, euthanasia, pets] = await Promise.all([
        TNR_API.getTransfersStrict(),
        TNR_API.getTreatmentsStrict(),
        TNR_API.getMaterialTransactionsStrict(),
        TNR_API.getEuthanasiaStrict(),
        TNR_API.getHospitalPetsStrict()
      ]);

      page.innerHTML = '<div class="tabs">' +
        '<div class="tab-item ' + (this.state.ledgerTab === 'transfer' ? 'active' : '') + '" data-ltab="transfer">交接台账 (' + transfers.length + ')</div>' +
        '<div class="tab-item ' + (this.state.ledgerTab === 'treatment' ? 'active' : '') + '" data-ltab="treatment">诊疗台账 (' + treatments.length + ')</div>' +
        '<div class="tab-item ' + (this.state.ledgerTab === 'material' ? 'active' : '') + '" data-ltab="material">物料台账 (' + txns.length + ')</div>' +
        '<div class="tab-item ' + (this.state.ledgerTab === 'euthanasia' ? 'active' : '') + '" data-ltab="euthanasia">安乐死台账 (' + euthanasia.length + ')</div>' +
        '</div>' +
        // 四个标签页共用一条筛选条（关键词 + 日期区间）：字段同构，分四条只会多一次维护。
        // 位置紧跟 .tabs —— CSS 里 .tabs 钉 top:0、.filter-bar 钉 top:46px，是配套的。
        TNR_UI.renderFilterBar([
          { key: 'keyword', label: '搜索', type: 'search', placeholder: '单号 / 宠物编号 / 物料 / 操作人' },
          { key: 'start_date', label: '开始日期', type: 'date' },
          { key: 'end_date', label: '结束日期', type: 'date' }
        ], '', { id: 'ledgerFilterBar' }) +
        '<div id="ledgerTabContent"></div>';

      page.querySelectorAll('[data-ltab]').forEach(el => {
        el.addEventListener('click', () => {
          this.state.ledgerTab = el.dataset.ltab;
          page.querySelectorAll('.tab-item').forEach(t => {
            t.classList.toggle('active', t.dataset.ltab === this.state.ledgerTab);
          });
          this.renderLedgerTab(transfers, treatments, txns, euthanasia, pets);
        });
      });

      this._ledgerCache = { transfers, treatments, txns, euthanasia, pets };
      this.renderLedgerTab(transfers, treatments, txns, euthanasia, pets);
      TNR_UI.bindFilter(() => {
        const d = this._ledgerCache || {};
        this.renderLedgerTab(d.transfers, d.treatments, d.txns, d.euthanasia, d.pets);
      }, page);
    } catch (e) {
      page.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚠️</div><div class="empty-state-text">加载失败</div></div>';
    }
  },

  renderLedgerTab(transfers, treatments, txns, euthanasia, pets) {
    const c = document.getElementById('ledgerTabContent');
    if (!c) return;
    const tab = this.state.ledgerTab;
    // 一宠一档：编号 → 动物档案，台账里同时展示类别/品种/性别
    const petByCode = {};
    (pets || []).forEach(p => { petByCode[p.code] = p; });
    const attrOf = (code) => TNR_API.petAttrText(petByCode[code]);
    const attrOfMany = (codes) => {
      const seen = [];
      (codes || []).forEach(code => {
        const t = attrOf(code);
        if (t !== '—' && seen.indexOf(t) === -1) seen.push(t);
      });
      return seen.length ? seen.join('；') : '—';
    };

    /* 四个标签页共用一条筛选条，统一在这里取值。
       日期字段各页不同（created_at / date / euthanized_at），故按值传入 inRange。 */
    const bar = document.getElementById('ledgerFilterBar');
    const v = TNR_UI.getFilterValues(bar);
    // `d` 各页不同：`MaterialTransaction.date` 是 DateField（纯日期，原样返回），
    // `Transfer/Treatment.created_at` 是 DateTimeField（UTC 串，需转本地日期）。
    // `inDateRange` 内部走 `localDateStr`，两种情况都对，故不必按页区分。
    const inRange = (d) => TNR_UI.inDateRange(d, v.start_date, v.end_date);
    const hit = (hay) => !v.keyword || String(hay || '').toLowerCase().includes(v.keyword);

    if (tab === 'transfer') {
      const data = transfers.filter(t => inRange(t.created_at)
          && hit([t.ledger_no, t.from_shelter_name, t.pet_codes].join(' ')))
        .slice().sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
      if (data.length === 0) { c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📊</div><div class="empty-state-text">暂无交接台账</div></div>'; return; }
      const rows = data.map(t => {
        const codes = this.parsePetCodes(t.pet_codes);
        return {
          ledger_no: t.ledger_no || '',
          created_at: t.created_at,
          from_shelter_name: t.from_shelter_name || '',
          _count: t.pet_count || 0,
          _codes: this.escape(codes.join(', ')),
          _attr: this.escape(attrOfMany(codes)),
          _status: this.transferStatusBadge(t.status),
          received_at: t.received_at,
        };
      });
      TNR_UI.mountTable(c, { id: 'hospital.ledger.transfer', columns: [
          { title: '单据编号', key: 'ledger_no' },
          { title: '创建时间', key: 'created_at', render: r => this.formatDateTime(r.created_at) },
          { title: '来源捕捉点', key: 'from_shelter_name' },
          { title: '动物数量', key: '_count', align: 'center' },
          { title: '宠物编号', key: '_codes', sortable: false },
          { title: '类别/品种/性别', key: '_attr', sortable: false },
          { title: '状态', key: '_status', sortable: false },
          { title: '接收时间', key: 'received_at', render: r => this.formatDateTime(r.received_at) },
        ], data: rows, emptyText: '暂无交接台账' });
    } else if (tab === 'treatment') {
      const data = treatments.filter(t => inRange(t.created_at)
          && hit([t.pet_code, t.hospital_name, t.sterilization_surgeon, t.operator_name].join(' ')))
        .slice().sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
      if (data.length === 0) { c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📊</div><div class="empty-state-text">暂无诊疗台账</div></div>'; return; }
      const rows = data.map(t => {
        const items = [];
        if (t.items_sterilization) items.push('绝育');
        if (t.items_vaccine) items.push('疫苗');
        if (t.items_deworming) items.push('驱虫');
        if (t.items_chip) items.push('芯片');
        return {
          pet_code: t.pet_code || '',
          _attr: this.escape(attrOf(t.pet_code)),
          _items: items.join('、') || '—',
          hospital_name: t.hospital_name || '',
          _doctor: this.escape(t.sterilization_surgeon || t.operator_name || '—'),
          created_at: t.created_at,
          _status: this.treatmentStatusBadge(t.status),
        };
      });
      TNR_UI.mountTable(c, { id: 'hospital.ledger.treatment', columns: [
          { title: '宠物编号', key: 'pet_code' },
          { title: '类别/品种/性别', key: '_attr', sortable: false },
          { title: '诊疗项目', key: '_items', sortable: false },
          { title: '医院', key: 'hospital_name' },
          { title: '医生', key: '_doctor', sortable: false },
          { title: '日期', key: 'created_at', render: r => this.formatDateTime(r.created_at) },
          { title: '状态', key: '_status', sortable: false },
        ], data: rows, emptyText: '暂无诊疗台账' });
    } else if (tab === 'material') {
      const data = txns.filter(t => inRange(t.date)
          && hit([t.ledger_no, t.material_name, t.operator_name].join(' ')))
        .slice().sort((a, b) => (b.date || '').localeCompare(a.date || ''));
      if (data.length === 0) { c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📊</div><div class="empty-state-text">暂无物料台账</div></div>'; return; }
      const rows = data.map(t => {
        // 出库的正负号由数量本身决定：正数扣（-）、负数盘盈加（+）。
        const qtyNum = Number(t.quantity) || 0;
        const sign = (t.type === 'consume' || t.type === 'adjustment' || (t.type === 'outbound' && qtyNum > 0)) ? '-' : '+';
        return {
          ledger_no: t.ledger_no || '',
          _type: TNR_UI.materialTxnBadge(t.type, 'long', 'hospital'),
          material_name: t.material_name || '',
          _qty: sign + (t.quantity || 0) + ' ' + this.escape(t.unit || ''),
          date: t.date,
          operator_name: t.operator_name || '',
        };
      });
      TNR_UI.mountTable(c, { id: 'hospital.ledger.material', columns: [
          { title: '单据编号', key: 'ledger_no' },
          { title: '类型', key: '_type', sortable: false },
          { title: '物料', key: 'material_name' },
          { title: '数量', key: '_qty', sortable: false },
          { title: '日期', key: 'date' },
          { title: '操作人', key: 'operator_name' },
        ], data: rows, emptyText: '暂无物料台账' });
    } else if (tab === 'euthanasia') {
      const data = euthanasia.filter(e => inRange(TNR_UI.firstDate(
            e.euthanizedAt, e.euthanized_at, e.createdAt, e.created_at))
          && hit([e.ledger_no, e.pet_code, e.reason, e.operator_name].join(' ')))
        .slice().sort((a, b) => (b.euthanized_at || '').localeCompare(a.euthanized_at || ''));
      if (data.length === 0) { c.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📊</div><div class="empty-state-text">暂无安乐死台账</div></div>'; return; }
      const rows = data.map(e => ({
        ledger_no: e.ledger_no || '',
        pet_code: e.pet_code || '',
        reason: e.reason || '',
        euthanized_at: e.euthanized_at,
        _body: e.body_received ? '<span class="badge badge-success">已移交</span>' : '<span class="badge badge-warning">未移交</span>',
        operator_name: e.operator_name || '',
      }));
      TNR_UI.mountTable(c, { id: 'hospital.ledger.euthanasia', columns: [
          { title: '单据编号', key: 'ledger_no' },
          { title: '宠物编号', key: 'pet_code' },
          { title: '原因', key: 'reason' },
          { title: '处置日期', key: 'euthanized_at', render: r => this.formatDate(r.euthanized_at) },
          { title: '遗体移交', key: '_body', sortable: false },
          { title: '操作人', key: 'operator_name' },
        ], data: rows, emptyText: '暂无安乐死台账' });
    }
  },

  /* ========== 7. 个人中心 ========== */
  render_profile() {
    const u = this.currentUser();
    if (!u) return;
    const page = document.getElementById('page-profile');
    page.innerHTML =
      '<div class="form-row" style="align-items: stretch;">' +
        '<div class="card">' +
          '<div class="card-header"><div class="card-title"><span class="card-title-bar"></span>账户信息</div></div>' +
          '<div class="card-body">' +
            TNR_UI.renderDescList([
              {label: '用户名', value: this.escape(u.username || '')},
              {label: '姓名', value: this.escape(u.name || '')},
              {label: '所属机构', value: this.escape(u.institution_name || '—')},
              {label: '机构类型', value: '宠物医院'},
              {label: '角色', value: this.escape(u.role_display || TNR_UI.roleText(u.role))},
              {label: '所属区县', value: this.escape(u.district_name || '—')},
              // 执业证号只能由平台端维护：它是诊疗单/处方/绝育证明上
              // 执业人身份的唯一来源，本人可改就等于自证身份。
              {label: '执业兽医师证号', value: u.vet_license
                  ? this.escape(u.vet_license)
                  : '<span class="badge badge-danger">未登记（请联系平台管理员）</span>'},
              {label: '联系电话', value: this.escape(u.phone || '—')},
              {label: '用户ID', value: this.escape(String(u.id || ''))}
            ]) +
          '</div>' +
        '</div>' +
        '<div class="card">' +
          '<div class="card-header"><div class="card-title"><span class="card-title-bar"></span>修改密码</div></div>' +
          '<div class="card-body">' +
            '<div class="form-group"><label class="form-label">原密码</label><input type="password" class="form-input" id="pf_oldPwd" autocomplete="current-password"></div>' +
            '<div class="form-group"><label class="form-label">新密码</label><input type="password" class="form-input" id="pf_newPwd" autocomplete="new-password"></div>' +
            '<div class="form-group"><label class="form-label">确认新密码</label><input type="password" class="form-input" id="pf_confirmPwd" autocomplete="new-password"></div>' +
            '<button class="btn btn-primary" id="btnChangePwd">修改密码</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div class="card mt-4"><div class="card-header"><div class="card-title"><span class="card-title-bar"></span>系统说明</div></div><div class="card-body">' +
      '<div style="font-size:var(--fs-sm);color:var(--ink-muted);line-height:1.8;">' +
      '· 医院端负责动物接收、诊疗操作、物料管理、领养信息维护及安乐死处置。<br>' +
      '· 领养登记与出库在「出院管理」办理，领养大厅资料由本院维护。<br>' +
      '· 物料库存联动：疫苗/驱虫/芯片在诊疗时自动扣减医院库存。<br>' +
      '· 执业兽医师证号由平台管理员统一登记，如需变更请联系平台管理员。' +
      '</div></div></div>';

    document.getElementById('btnChangePwd').addEventListener('click', async (ev) => {
      const btn = ev.currentTarget;
      const oldPwd = document.getElementById('pf_oldPwd').value;
      const newPwd = document.getElementById('pf_newPwd').value;
      const confirmPwd = document.getElementById('pf_confirmPwd').value;
      if (!oldPwd || !newPwd || !confirmPwd) { TNR_UI.toast('请填写完整', 'warning'); return; }
      if (newPwd !== confirmPwd) { TNR_UI.toast('两次新密码不一致', 'warning'); return; }
      if (newPwd.length < 8) { TNR_UI.toast('新密码至少8位', 'warning'); return; }
      btn.disabled = true;
      try {
        const res = await TNR_API.post('/api/me/password/', {
          old_password: oldPwd, new_password: newPwd, confirm_password: confirmPwd
        });
        TNR_UI.toast((res && res.message) || '密码修改成功', 'success');
        document.getElementById('pf_oldPwd').value = '';
        document.getElementById('pf_newPwd').value = '';
        document.getElementById('pf_confirmPwd').value = '';
      } catch (e) {
        TNR_UI.toast('修改失败：' + (e.message || '未知错误'), 'danger');
      }
      btn.disabled = false;
    });
  }
};

/* === 启动 === */
Hospital.init();
