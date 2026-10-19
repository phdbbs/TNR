/* ============================================
   平台管理端 - SPA 应用逻辑

   职责边界（第四十二轮）：全局设置在这里，业务查看在政府端（/gov/）。
   ------------------------------------------------
   机构基础管理 / 账号权限管理 / 系统配置  ← 本端
   数据总览大屏 / 全业务监管 / 物料全局监管 / 全局台账中心 ← 政府端
   ============================================ */

const PlatformPortal = {
  currentUser: window.TNR_USER || null,
  instState: { tab: 'district' },
  settingsState: { tab: 'prefix' },

  /* ⚠ 键必须与 `platform_base.html` 侧边栏的标签文本一一对应。
     点击是按 label 文本找 render 方法的，两边漂移就是「点了没反应」。 */
  titles: {
    'institutions': '机构基础管理',
    'accounts': '账号权限管理',
    'settings': '系统配置'
  },

  /* ========== 初始化 ========== */
  init() {
    this.bindSidebarNav();
    TNR_UI.initPortalShell();
    this.navigate('institutions');
  },

  /* 绑定 platform_base 侧边栏导航 → SPA 路由 */
  bindSidebarNav() {
    const navMap = {
      '机构基础管理': 'institutions',
      '账号权限管理': 'accounts',
      '系统配置': 'settings'
    };
    document.querySelectorAll('.portal-sidebar-nav .nav-item').forEach(a => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
        const label = a.querySelector('.nav-item-label')?.textContent.trim();
        const page = navMap[label];
        if (page) {
          document.querySelectorAll('.portal-sidebar-nav .nav-item').forEach(x => x.classList.remove('active'));
          a.classList.add('active');
          this.navigate(page);
        }
      });
    });
  },

  navigate(pageId) {
    document.querySelectorAll('.page-view').forEach(p => p.classList.remove('active'));
    const page = document.getElementById('page-' + pageId);
    if (page) page.classList.add('active');
    const titleEl = document.querySelector('.portal-topbar-title');
    if (titleEl) titleEl.textContent = this.titles[pageId] || '';
    const renderer = this['render_' + pageId];
    if (renderer) renderer.call(this);
  },

  /* ========== 辅助方法 ==========
   *
   * ⚠ `isGlobalScope()` **不是**政府端 `isCityLevel()` 的同义词，尽管它们在这一页
   * 控制同一批「新增 / 编辑 / 停用」按钮。平台管理员（`platform_admin`）是
   * **独立于区县**的全局角色（见 `core/scope.py` 的 `GLOBAL_SCOPE_ROLES`），
   * 账号上没有区县归属。这里若照抄成 `role === 'gov_city'`，平台端**根本没有**
   * gov_city 账号，结果是所有管理按钮静默消失、整页退化成只读 ——
   * 不报错、不空表，只是「点不动」。所以判据必须按平台端自己的角色写。 */
  isGlobalScope() { return this.currentUser && this.currentUser.role === 'platform_admin'; },

  /* 机构管理页的三个 tab。文案不再本地重写：医院/捕捉点取
   * `TNR_UI.INST_TYPE`（= `Institution.TYPE_CHOICES` 的 label），
   * 「区县」是平台端独有的管理对象、类型枚举里没有，只有它保留本地写法。 */
  INST_TABS: ['district', 'hospital', 'shelter'],
  instTabLabel(tab) {
    return tab === 'district' ? '区县' : TNR_UI.instTypeText(tab);
  },
  canCreateRole(role) {
    // 领养人不在这里创建（由捕捉点在领养登记时自动创建）
    if (role === 'adopter') return false;
    // 平台端可建全部后台角色，含另一个平台管理员
    if (this.isGlobalScope()) return ['platform_admin', 'gov_city', 'gov_district', 'shelter', 'hospital'].includes(role);
    return false;
  },
  getDistrictName(district, fallbackId) {
    if (!district && !fallbackId) return '全市';
    if (district && district.name) return district.name;
    return fallbackId || '—';
  },

  /* 列表加载失败态：标题 + 服务端消息 + 重试。
     平台端这几页是「建机构 / 建账号 / 改配置」的入口，读失败若渲染成
     「暂无数据」，管理员会以为库里真的空了，甚至照着空列表重复新建。 */
  showLoadError(el, title, err, retry) {
    if (!el) return;
    el.innerHTML = '<div class="table-empty"><div class="table-empty-icon">⚠️</div>'
      + '<div class="table-empty-text">' + TNR_UI.escape(title) + '</div>'
      + '<div class="form-hint mt-2">' + TNR_UI.escape((err && err.message) || '请稍后重试') + '</div>'
      + '<button type="button" class="btn btn-secondary mt-4" data-reload>重新加载</button></div>';
    const btn = el.querySelector('[data-reload]');
    if (btn) btn.onclick = retry;
  },

  /* ========== 2. 机构基础管理 ========== */
  async render_institutions() {
    const el = document.getElementById('page-institutions');
    // 小区不再单独设置（手填即可），改为区县设置 + 医院管理 + 捕捉点管理
    el.innerHTML = `
      <div class="tabs">
        ${this.INST_TABS.map(t => `
          <div class="tab-item ${this.instState.tab === t ? 'active' : ''}" data-inst-tab="${t}">${this.instTabLabel(t)}管理</div>
        `).join('')}
      </div>
      <div id="instContent"><div class="table-empty"><div class="table-empty-icon">⏳</div><div class="table-empty-text">加载中...</div></div></div>
    `;
    el.querySelectorAll('[data-inst-tab]').forEach(tab => {
      tab.addEventListener('click', () => { this.instState.tab = tab.dataset.instTab; this.render_institutions(); });
    });
    await this.renderInstitutionsContent();
  },

  async renderInstitutionsContent() {
    const contentEl = document.getElementById('instContent');
    let districts;
    try {
      districts = (await TNR_API.getDistrictsStrict()) || [];
    } catch (e) {
      this.showLoadError(contentEl, '区县数据加载失败', e, () => this.renderInstitutionsContent());
      return;
    }
    this._districts = districts;
    const activeDistricts = districts.filter(d => d.status === 'active');

    if (this.instState.tab === 'district') {
      // 区县管理 - 只读列表 + 仅平台管理员可增编停
      const canManage = this.isGlobalScope();
      contentEl.innerHTML = TNR_UI.renderFilterBar([
        { key: 'status', label: '状态', type: 'select', options: TNR_UI.entityStatusOptions() },
        { key: 'search', label: '搜索', type: 'search', placeholder: '名称/代码' }
      ], canManage ? `<button class="btn btn-primary btn-sm" id="btnAddDist">+ 新增区县</button>` : '')
        + '<div class="table-wrapper" id="distTableWrapper"></div>';
      if (canManage) document.getElementById('btnAddDist').addEventListener('click', () => this.showDistrictModal());
      this.renderDistrictsTable();
      TNR_UI.bindFilter(() => this.renderDistrictsTable(), contentEl);
      return;
    }

    // 医院/捕捉点管理
    let institutions;
    try {
      institutions = (await TNR_API.getInstitutionsStrict(this.instState.tab)) || [];
    } catch (e) {
      contentEl.innerHTML = TNR_UI.renderFilterBar([
        { key: 'district', label: '区县', type: 'select', options: activeDistricts.filter(d => !d.is_city).map(d => ({ value: d.id, label: d.name })) },
        { key: 'status', label: '状态', type: 'select', options: TNR_UI.entityStatusOptions() },
        { key: 'search', label: '搜索', type: 'search', placeholder: '名称/地址/联系人' }
      ], `<button class="btn btn-primary btn-sm" id="btnAddInst">+ 新增${this.instTabLabel(this.instState.tab)}</button>`)
        + '<div class="table-wrapper" id="instTableWrapper"></div>';
      document.getElementById('btnAddInst').addEventListener('click', () => this.showInstitutionModal());
      this.showLoadError(document.getElementById('instTableWrapper'), '机构数据加载失败', e, () => this.renderInstitutionsContent());
      return;
    }
    this._institutions = institutions;
    contentEl.innerHTML = TNR_UI.renderFilterBar([
      { key: 'district', label: '区县', type: 'select', options: activeDistricts.filter(d => !d.is_city).map(d => ({ value: d.id, label: d.name })) },
      { key: 'status', label: '状态', type: 'select', options: TNR_UI.entityStatusOptions() },
      { key: 'search', label: '搜索', type: 'search', placeholder: '名称/地址/联系人' }
    ], `<button class="btn btn-primary btn-sm" id="btnAddInst">+ 新增${this.instTabLabel(this.instState.tab)}</button>`)
      + '<div class="table-wrapper" id="instTableWrapper"></div>';
    document.getElementById('btnAddInst').addEventListener('click', () => this.showInstitutionModal());
    this.renderInstitutionsTable();
    TNR_UI.bindFilter(() => this.renderInstitutionsTable(), contentEl);
  },

  renderDistrictsTable() {
    const filters = TNR_UI.getFilterValues(document.getElementById('instContent'));
    let data = (this._districts || []).slice();
    // 区县创建时间是 DateField（`District.created_at`）—— `inDateRange` 对纯日期串原样返回。
    data = data.filter(d =>
      TNR_UI.inDateRange(d.createdAt || d.created_at, filters.start_date, filters.end_date));
    if (filters.status) data = data.filter(d => d.status === filters.status);
    if (filters.search) {
      const s = filters.search.toLowerCase();
      data = data.filter(d => (d.name && d.name.toLowerCase().includes(s)) || (d.code && d.code.toLowerCase().includes(s)));
    }
    const canManage = this.isGlobalScope();
    const columns = [
      { key: 'id', title: '编号', width: '90px' },
      { key: 'name', title: '区县名称', render: r => TNR_UI.escape(r.name) + (r.is_city ? ' <span class="badge badge-info">市级</span>' : '') },
      { key: 'code', title: '区县代码', render: r => TNR_UI.escape(r.code || '—') },
      { key: 'is_city', title: '级别', render: r => r.is_city ? '<span class="badge badge-ink">市级</span>' : '<span class="badge badge-default">区级</span>' },
      { key: 'created_at', title: '创建时间', render: r => TNR_UI.formatDate(r.createdAt || r.created_at) },
      { key: 'status', title: '状态', render: r => TNR_UI.entityStatusBadge(r.status) }
    ];
    const rowActions = canManage ? (r => `
      <button class="btn-link" data-edit-dist="${r.id}">编辑</button>
      <button class="btn-link ${TNR_UI.entityToggleBtnClass(r.status)}" data-toggle-dist="${r.id}">${TNR_UI.entityToggleLabel(r.status)}</button>
      <button class="btn-link danger" data-del-dist="${r.id}">删除</button>
    `) : (() => '');
    TNR_UI.mountTable('distTableWrapper', { id: 'platform.districts', columns, data, rowActions, emptyText: '暂无区县数据' });
    TNR_UI.delegateClick('distTableWrapper', {
      'data-edit-dist': (id) => this.showDistrictModal(id),
      'data-toggle-dist': (id) => this.toggleDistrictStatus(id),
      'data-del-dist': (id) => this.deleteDistrict(id)
    });
  },

  async deleteDistrict(id) {
    const dist = (this._districts || []).find(d => String(d.id) === String(id));
    const ok = await TNR_UI.confirm(
      '确定要删除区县 <b>' + TNR_UI.escape(dist ? dist.name : id) + '</b> 吗？<br>若该区县已有关联业务数据将无法删除（建议停用）。', '删除确认'
    );
    if (!ok) return;
    try {
      const res = await TNR_API.deleteDistrict(id);
      TNR_UI.assertOk(res, '区县删除失败');
      TNR_UI.toast(res.message || '区县删除成功', 'success');
      await this.renderInstitutionsContent();
    } catch (e) {
      TNR_UI.toast(e.message || '删除失败', 'danger');
    }
  },

  async showDistrictModal(id) {
    let dist = null;
    if (id) {
      dist = (this._districts || []).find(d => String(d.id) === String(id));
      if (!dist) {
        // 缓存里找不到（列表被刷新/删除）时再严格读一次；**不能**当成新增，
        // 否则「编辑」会把一条已有区县再建一遍。
        try {
          this._districts = (await TNR_API.getDistrictsStrict()) || [];
        } catch (e) {
          TNR_UI.toast('区县数据加载失败：' + (e.message || '请稍后重试'), 'danger');
          return;
        }
        dist = this._districts.find(d => String(d.id) === String(id));
      }
      if (!dist) { TNR_UI.toast('区县不存在或已被删除，请刷新列表', 'warning'); return; }
    }
    const body = `
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">区县名称<span class="req">*</span></label>
          <input type="text" class="form-input" id="dist-name" value="${dist ? TNR_UI.escape(dist.name) : ''}" placeholder="如：朝阳区">
        </div>
        <div class="form-group">
          <label class="form-label">区县代码<span class="req">*</span></label>
          <input type="text" class="form-input" id="dist-code" value="${dist ? TNR_UI.escape(dist.code) : ''}" placeholder="如：CY">
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">级别</label>
        <select class="form-select" id="dist-is-city">
          <option value="0" ${!dist || !dist.is_city ? 'selected' : ''}>区级（具体区县）</option>
          <option value="1" ${dist && dist.is_city ? 'selected' : ''}>市级（市级政府管理员/捕捉点归属）</option>
        </select>
      </div>
      <p class="form-hint">说明：市级区县用于市级政府管理员/捕捉点操作员归属；区级区县用于区级政府管理员/医院归属。</p>
      ${dist ? `<p class="form-hint" style="color:var(--rouge);">该区县下已有账号或业务数据挂靠时，级别不可修改 —— 级别决定其下所有账号的可见范围（市级 = 可见全部区县）。</p>` : ''}
    `;
    const m = TNR_UI.modal({
      title: dist ? '编辑区县' : '新增区县', body, size: 'md',
      actions: `<button class="btn btn-secondary" data-action="cancel">取消</button><button class="btn btn-primary" id="distSave">保存</button>`
    });
    m.overlay.querySelector('[data-action="cancel"]').addEventListener('click', m.close);
    m.overlay.querySelector('#distSave').addEventListener('click', async () => {
      const name = document.getElementById('dist-name').value.trim();
      const code = document.getElementById('dist-code').value.trim();
      const is_city = document.getElementById('dist-is-city').value === '1';
      if (!name) { TNR_UI.toast('请输入区县名称', 'warning'); return; }
      if (!code) { TNR_UI.toast('请输入区县代码', 'warning'); return; }
      const payload = { name, code, is_city };
      try {
        if (dist) {
          TNR_UI.assertOk(await TNR_API.editDistrict(dist.id, payload), '区县更新失败');
          TNR_UI.toast('区县更新成功', 'success');
        } else {
          TNR_UI.assertOk(await TNR_API.createDistrict(payload), '区县创建失败');
          TNR_UI.toast('区县创建成功', 'success');
        }
        m.close();
        await this.renderInstitutionsContent();
      } catch (e) {
        TNR_UI.toast(e.message || '操作失败', 'danger');
      }
    });
  },

  async toggleDistrictStatus(id) {
    try {
      const res = await TNR_API.toggleDistrict(id);
      TNR_UI.assertOk(res, '操作失败');
      TNR_UI.toast(res.message || '操作成功', 'success');
      await this.renderInstitutionsContent();
    } catch (e) {
      TNR_UI.toast(e.message || '操作失败', 'danger');
    }
  },

  renderInstitutionsTable() {
    const filters = TNR_UI.getFilterValues(document.getElementById('instContent'));
    let data = (this._institutions || []).slice();
    // 机构创建时间（`Institution.created_at`，第三十九轮补的字段）——
    // 少了它，这一页的起止时间就是个「设了没反应」的空壳。
    data = data.filter(i =>
      TNR_UI.inDateRange(i.createdAt || i.created_at, filters.start_date, filters.end_date));
    if (!this.isGlobalScope() && this.currentUser.district_id) {
      data = data.filter(i => i.district_id === this.currentUser.district_id);
    }
    if (filters.district) data = data.filter(i => String(i.district_id) === String(filters.district));
    if (filters.status) data = data.filter(i => i.status === filters.status);
    if (filters.search) {
      const s = filters.search.toLowerCase();
      data = data.filter(i => (i.name && i.name.toLowerCase().includes(s)) || (i.address && i.address.toLowerCase().includes(s)) || (i.contact && i.contact.toLowerCase().includes(s)));
    }
    const districtMap = {};
    (this._districts || []).forEach(d => { districtMap[d.id] = d.name; });

    const columns = [
      { key: 'id', title: '编号', width: '90px' },
      { key: 'name', title: '名称', render: r => TNR_UI.escape(r.name) },
      // 机构编号（I001/C001…）是**稳定业务键**：去重、引用、种子脚本幂等匹配都靠它，
      // 机构名只是展示字段（会被人在界面上改）。与区县列表一致地把它显示出来。
      { key: 'code', title: '机构编号', render: r => TNR_UI.escape(r.code || '—') },
      { key: 'district_id', title: '区县', render: r => TNR_UI.escape(districtMap[r.district_id] || r.district_name || '—') },
      { key: 'address', title: '地址', render: r => TNR_UI.escape(r.address || '—') },
      { key: 'contact', title: '联系人', render: r => TNR_UI.escape(r.contact || '—') },
      { key: 'phone', title: '电话', render: r => TNR_UI.escape(r.phone || '—') },
      { key: 'created_at', title: '创建时间', render: r => TNR_UI.formatDate(r.createdAt || r.created_at) },
      { key: 'status', title: '状态', render: r => TNR_UI.entityStatusBadge(r.status) }
    ];
    const rowActions = r => `
      <button class="btn-link" data-edit="${r.id}">编辑</button>
      <button class="btn-link ${TNR_UI.entityToggleBtnClass(r.status)}" data-toggle="${r.id}">${TNR_UI.entityToggleLabel(r.status)}</button>
    `;
    TNR_UI.mountTable('instTableWrapper', { id: 'platform.institutions', columns, data, rowActions, emptyText: '暂无机构数据' });
    TNR_UI.delegateClick('instTableWrapper', {
      'data-edit': (id) => this.showInstitutionModal(id),
      'data-toggle': (id) => this.toggleInstitutionStatus(id)
    });
  },

  async showInstitutionModal(id) {
    let districts;
    try {
      districts = (await TNR_API.getDistrictsStrict()) || [];
    } catch (e) {
      TNR_UI.toast('区县数据加载失败：' + (e.message || '请稍后重试'), 'danger');
      return;
    }
    /* 区县可选项：**捕捉点可以挂市级，医院不行**。
     *
     * 第四十五轮引入「市级捕捉点」（可采购药品、可下发给全市任何捕捉点/医院、
     * 捕捉的动物可转运到全市任何医院），服务端 `institution_create` 的判据就是
     * `if inst_type == 'hospital' and district.is_city: fail` —— 捕捉点从来是放行的。
     * 但前端一律排除市级，于是**根本建不出市级捕捉点**：下拉里没有这个选项、
     * 界面也不报错，用户只会以为「系统不支持」，只能靠改种子数据或直接改库。
     * 这就是「前端收窄选项而服务端不校验」的**反向**形态 —— 服务端允许、
     * 前端藏起来了，比多给选项更难发现（多给会撞 400，少给是**静默做不到**）。
     */
    const isShelter = this.instState.tab === 'shelter';
    let selectable = (districts || []).filter(d => d.status === 'active' && (isShelter || !d.is_city));
    // 区级政府管理员只能在本区县建机构，下拉就必须只列本区县 ——
    // 否则随手选一个他区会撞上「无权在其他区县创建机构」，白填一遍。
    // 服务端 `_district_out_of_scope()` 拦的是同一件事（前端只是别让人白填）。
    if (!this.isGlobalScope() && this.currentUser && this.currentUser.district_id) {
      selectable = selectable.filter(d => d.id === this.currentUser.district_id);
    }
    let inst = null;
    if (id) {
      let all = this._institutions;
      if (!all) {
        try {
          all = (await TNR_API.getInstitutionsStrict(this.instState.tab)) || [];
        } catch (e) {
          TNR_UI.toast('机构数据加载失败：' + (e.message || '请稍后重试'), 'danger');
          return;
        }
      }
      inst = all.find(i => String(i.id) === String(id));
      // 读失败或列表已变化时**不能**退化成「新增」——那会把一次编辑
      // 变成再建一家同名机构。宁可让用户刷新列表重来。
      if (!inst) { TNR_UI.toast('机构不存在或已被删除，请刷新列表', 'warning'); return; }
    }
    // 编辑时把机构**当前**所属区县兜底加回选项。上面的列表排除了市级，而挂市级的
    // 捕捉点确实存在（现场两个捕捉点就挂在「全市（市级）」下）—— 少了这一步，
    // <select> 会退化成选中第一项，于是**不改区县直接点保存**就会把机构
    // 静默搬到某个具体区县（还会连带搬走它下面的操作员）。
    if (inst && inst.district_id && !selectable.some(d => d.id === inst.district_id)) {
      const cur = (districts || []).find(d => d.id === inst.district_id);
      if (cur) selectable = [cur].concat(selectable);
    }
    const activeDistricts = selectable;

    const body = `
      <div class="form-group">
        <label class="form-label">名称<span class="req">*</span></label>
        <input type="text" class="form-input" id="inst-name" value="${inst ? TNR_UI.escape(inst.name) : ''}" placeholder="请输入${this.instTabLabel(this.instState.tab)}名称">
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">所属区县<span class="req">*</span></label>
          <select class="form-select" id="inst-district">
            ${activeDistricts.map(d => `<option value="${d.id}" ${inst && inst.district_id === d.id ? 'selected' : ''}>${TNR_UI.escape(d.name)}</option>`).join('')}
          </select>
          <div class="fs-sm text-muted mt-2" id="inst-district-hint"></div>
        </div>
        <div class="form-group">
          <label class="form-label">状态</label>
          <select class="form-select" id="inst-status">
            ${TNR_UI.entityStatusOptions().map(o => `<option value="${o.value}" ${((inst && inst.status) || 'active') === o.value ? 'selected' : ''}>${TNR_UI.escape(o.label)}</option>`).join('')}
          </select>
        </div>
      </div>
      <div class="form-group">
        <label class="form-label">地址</label>
        <input type="text" class="form-input" id="inst-address" value="${inst ? TNR_UI.escape(inst.address || '') : ''}" placeholder="请输入详细地址">
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">联系人</label>
          <input type="text" class="form-input" id="inst-contact" value="${inst ? TNR_UI.escape(inst.contact || '') : ''}" placeholder="请输入联系人">
        </div>
        <div class="form-group">
          <label class="form-label">联系电话</label>
          <input type="text" class="form-input" id="inst-phone" value="${inst ? TNR_UI.escape(inst.phone || '') : ''}" placeholder="请输入电话">
        </div>
      </div>
    `;

    const m = TNR_UI.modal({
      title: inst ? `编辑${this.instTabLabel(this.instState.tab)}` : `新增${this.instTabLabel(this.instState.tab)}`,
      body, size: 'lg',
      actions: `<button class="btn btn-secondary" data-action="cancel">取消</button><button class="btn btn-primary" id="instSave">保存</button>`
    });
    m.overlay.querySelector('[data-action="cancel"]').addEventListener('click', m.close);

    /* 「所属区县」选到市级时把**后果**写出来（第四十五轮）。
     *
     * 市级捕捉点不是「捕捉点挂个市级标签」那么轻 —— 它换掉的是三条业务规则：
     * 能自行采购、能下发给全市任何机构、能跨区县转运动物。
     * 不写清楚，运维会以为建出来和普通捕捉点没区别，然后在区县看到
     * 「明明有医院却发不出去」的单子（因为那个捕捉点是区县级的）。
     */
    const cityIds = new Set((districts || []).filter(d => d.is_city).map(d => String(d.id)));
    const updateDistrictHint = () => {
      const hint = document.getElementById('inst-district-hint');
      const sel = document.getElementById('inst-district');
      if (!hint || !sel) return;
      if (this.instState.tab === 'hospital') {
        hint.textContent = '医院必须挂具体区县：只能接收本区县捕捉点的下发。';
        return;
      }
      hint.textContent = cityIds.has(String(sel.value))
        ? '市级捕捉点：可自行采购药品、可下发给全市任何捕捉点或医院、捕捉的动物可转运到全市任何医院。'
        : '区县级捕捉点：只能下发给本区县内的医院或捕捉点（跨区县流动需经市级捕捉点中转）。';
    };
    document.getElementById('inst-district').addEventListener('change', updateDistrictHint);
    updateDistrictHint();

    m.overlay.querySelector('#instSave').addEventListener('click', async () => {
      const name = document.getElementById('inst-name').value.trim();
      if (!name) { TNR_UI.toast('请输入名称', 'warning'); return; }
      const phone = document.getElementById('inst-phone').value.trim();
      if (phone && !phone.replace(/[-+]/g, '').match(/^\d+$/)) { TNR_UI.toast('联系电话格式不正确', 'warning'); return; }
      const payload = {
        name, type: this.instState.tab,
        district_id: document.getElementById('inst-district').value,
        address: document.getElementById('inst-address').value.trim(),
        contact: document.getElementById('inst-contact').value.trim(),
        phone: phone,
      };
      try {
        if (inst) {
          const res = TNR_UI.assertOk(await TNR_API.editInstitution(inst.id, payload), '机构更新失败');
          // 用服务端消息：机构换区县时会把挂在它下面的操作员一起搬过去，
          // 这件事必须让操作者看到（「1 个操作员的所属区县已同步调整」）。
          TNR_UI.toast(res.message || '机构更新成功', 'success');
        } else {
          TNR_UI.assertOk(await TNR_API.createInstitution(payload), '机构创建失败');
          TNR_UI.toast('机构创建成功', 'success');
        }
        m.close();
        await this.renderInstitutionsContent();
      } catch (e) {
        TNR_UI.toast(e.message || '操作失败', 'danger');
      }
    });
  },

  async toggleInstitutionStatus(id) {
    try {
      const res = await TNR_API.toggleInstitution(id);
      TNR_UI.assertOk(res, '操作失败');
      TNR_UI.toast(res.message || '操作成功', 'success');
      await this.renderInstitutionsContent();
    } catch (e) {
      TNR_UI.toast(e.message || '操作失败', 'danger');
    }
  },

  /* ========== 3. 账号权限管理 ========== */
  async render_accounts() {
    const el = document.getElementById('page-accounts');
    el.innerHTML = TNR_UI.renderFilterBar([
      // 领养人由捕捉点在领养登记时自动创建，不在平台端账号体系里筛
      { key: 'role', label: '角色', type: 'select', options: TNR_UI.roleOptions(['adopter']) },
      { key: 'status', label: '状态', type: 'select', options: TNR_UI.entityStatusOptions() },
      { key: 'search', label: '搜索', type: 'search', placeholder: '用户名/姓名/电话' }
    ], `<button class="btn btn-primary btn-sm" id="btnAddAcct">+ 新增账号</button>`)
      + '<div class="table-wrapper" id="acctTableWrapper"><div class="table-empty"><div class="table-empty-icon">⏳</div><div class="table-empty-text">加载中...</div></div></div>'
      + '<div class="card mt-5"><div class="card-header"><div class="card-title"><span class="card-title-bar"></span>角色权限说明</div></div><div class="card-body"><div class="role-perm-grid" id="rolePermGrid"></div></div></div>';

    document.getElementById('btnAddAcct').addEventListener('click', () => this.showAccountModal());
    this.renderRolePermissions();
    await this.refreshAccountsTable();
    TNR_UI.bindFilter(() => this.renderAccountsTable(), el);
  },

  async refreshAccountsTable() {
    try {
      this._users = (await TNR_API.getUsersStrict()) || [];
    } catch (e) {
      // 置空而不是保留上一次的列表：筛选框在失败后仍然绑着
      // `renderAccountsTable()`，留着旧数据会让用户一敲键盘就把错误态
      // 换成一份过期名单 —— 看起来加载成功了，其实一条新数据也没有。
      this._users = null;
      this.showLoadError(document.getElementById('acctTableWrapper'), '账号数据加载失败', e,
        () => this.refreshAccountsTable());
      return;
    }
    this.renderAccountsTable();
  },

  renderAccountsTable() {
    if (!this._users) return;
    const filters = TNR_UI.getFilterValues(document.getElementById('page-accounts'));
    let data = this._users.filter(u => u.role !== 'adopter');
    // 账号的开户时间 = `date_joined`（`user_list` 返回的字段），不是 `User.created_at` ——
    // 两者对由 seed 建的账号可能不同天，界面上「开户时间」列用的是前者，保持同口径。
    data = data.filter(u =>
      TNR_UI.inDateRange(u.dateJoined || u.date_joined, filters.start_date, filters.end_date));
    if (filters.role) data = data.filter(u => u.role === filters.role);
    if (filters.status) data = data.filter(u => u.status === filters.status);
    if (filters.search) {
      const s = filters.search.toLowerCase();
      data = data.filter(u => (u.username && u.username.toLowerCase().includes(s)) || (u.name && u.name.toLowerCase().includes(s)) || (u.phone && u.phone.includes(s)));
    }
    // 单元格渲染集中定义：桌面列与手机主列共用同一份，避免两端各写一套后
    // 「电脑上写未登记、手机上空着」这类口径漂移。
    const cell = {
      name: r => TNR_UI.escape(r.name || '—'),
      role: r => TNR_UI.roleBadge(r.role),
      district: r => TNR_UI.escape(r.district_name || '—'),
      institution: r => TNR_UI.escape(r.institution_name || '—'),
      // 医院操作员的证号空着 = 他签的诊疗单无法追溯到执业人。这一列要看得见，
      // 否则平台端会以为「都登记过了」，而巡检也只能报已经建完的号。
      vetLicense: r => r.role !== 'hospital'
        ? '<span style="color:var(--ink-light);">—</span>'
        : (r.vet_license ? TNR_UI.escape(r.vet_license)
            : '<span class="badge badge-danger">未登记</span>'),
      phone: r => TNR_UI.escape(r.phone || '—'),
      joined: r => TNR_UI.formatDateTime(r.dateJoined || r.date_joined),
      status: r => TNR_UI.entityStatusBadge(r.status)
    };
    // 手机端：身份与状态常显，区县 / 机构 / 证号 / 电话 / 开户时间收进
    // 「更多信息」—— 10 行账号若每行都摊开 8 行字，翻页成本比横滑更高。
    const mobileCard = r => TNR_UI.mobileLine('姓名', cell.name(r))
      + TNR_UI.mobileLine('角色', cell.role(r))
      + TNR_UI.mobileLine('状态', cell.status(r))
      + TNR_UI.mobileMore(
        TNR_UI.mobileLine('区县', cell.district(r))
        + TNR_UI.mobileLine('所属机构', cell.institution(r))
        + (r.role === 'hospital' ? TNR_UI.mobileLine('执业证号', cell.vetLicense(r)) : '')
        + TNR_UI.mobileLine('电话', cell.phone(r))
        + TNR_UI.mobileLine('开户时间', cell.joined(r)));
    const columns = [
      { key: 'username', title: '用户名', mobileRender: mobileCard },
      { key: 'name', title: '姓名', render: cell.name, mobile: 'hide' },
      { key: 'role', title: '角色', render: cell.role, mobile: 'hide' },
      { key: 'district_name', title: '区县', render: cell.district, mobile: 'hide' },
      { key: 'institution_name', title: '所属机构', render: cell.institution, mobile: 'hide' },
      { key: 'vet_license', title: '执业证号', render: cell.vetLicense, mobile: 'hide' },
      { key: 'phone', title: '电话', render: cell.phone, mobile: 'hide' },
      { key: 'date_joined', title: '开户时间', render: cell.joined, mobile: 'hide' },
      { key: 'status', title: '状态', render: cell.status, mobile: 'hide' }
    ];
    const selfId = window.TNR_USER && window.TNR_USER.id;
    const rowActions = r => r.id === selfId
      ? '<span style="font-size:var(--fs-xs);color:var(--ink-light);">当前账号</span>'
      : `<button class="btn-link" data-edit="${r.id}">编辑</button>`
        + `<button class="btn-link ${TNR_UI.entityToggleBtnClass(r.status)}" data-toggle="${r.id}">${TNR_UI.entityToggleLabel(r.status)}</button>`;
    TNR_UI.mountTable('acctTableWrapper', { id: 'platform.accounts', columns, data, rowActions, emptyText: '暂无账号' });
    TNR_UI.delegateClick('acctTableWrapper', {
      'data-toggle': (id) => this.toggleAccountStatus(id),
      'data-edit': (id) => this.showAccountEditModal(data.find(u => u.id === Number(id)))
    });
  },

  /* 角色权限说明卡：只写「能干什么」，角色名一律取 `TNR_UI.ROLE`（= 后端
   * `User.ROLE_CHOICES` 的 label）。以前这里第 5 次抄了一遍角色名，
   * 抄成了「市级管理员 / 区级管理员」，与同页账号列表、个人中心、打印单据
   * 用的全称对不上 —— 授权说明是要给监管看的，职务名不能两套。 */
  renderRolePermissions() {
    const perms = [
      { role: 'platform_admin', desc: '全局设置：区县与机构管理、账号权限管理、编号规则、公告发布、操作日志。可见范围：全部区县，不受区县约束。' },
      { role: 'gov_city', desc: '全市数据查看、机构管理、账号管理、区县管理、系统配置、全业务监管。所属区域：市级。' },
      { role: 'gov_district', desc: '本区县数据查看、本区县机构管理、本区县账号管理（限捕捉点/医院）。所属区域：具体区县。' },
      { role: 'shelter', desc: '捕捉登记、转运派单、物料采购下发、放归管理、领养审核、回访管控。所属区域：市级捕捉点挂「全市（市级）」、区县捕捉点挂对应区县，须关联捕捉点且与机构同区县。' },
      { role: 'hospital', desc: '动物接收、诊疗绝育、物料消耗、领养资料维护、安乐死处置。所属区域：具体区县，须关联医院。' }
    ];
    document.getElementById('rolePermGrid').innerHTML = perms.map(p => `
      <div class="role-perm-card">
        <h4>${TNR_UI.roleBadge(p.role)}</h4>
        <p>${TNR_UI.escape(p.desc)}</p>
      </div>
    `).join('');
  },

  async showAccountModal() {
    let districts;
    try {
      districts = (await TNR_API.getDistrictsStrict()) || [];
    } catch (e) {
      TNR_UI.toast('区县数据加载失败：' + (e.message || '请稍后重试'), 'danger');
      return;
    }
    const activeDistricts = districts.filter(d => d.status === 'active');
    const cityDistricts = activeDistricts.filter(d => d.is_city);     // 市级
    // 区级政府管理员只能管本区县，下拉就必须只列本区县 ——
    // 否则「所属区县」列出全部 4 个、而「关联机构」只列本区县的，
    // 随手选一个他区就会撞上「账号区县与机构区县不一致」。
    // 服务端 `validate_user_manage_scope()` 拦的是同一件事（前端只是别让人白填）。
    let realDistricts = activeDistricts.filter(d => !d.is_city);      // 区级
    if (!this.isGlobalScope() && this.currentUser && this.currentUser.district_id) {
      realDistricts = realDistricts.filter(d => d.id === this.currentUser.district_id);
    }
    const allRoles = TNR_UI.roleOptions(['adopter']);
    const availableRoles = allRoles.filter(r => this.canCreateRole(r.value));

    const body = `
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">用户名<span class="req">*</span></label>
          <input type="text" class="form-input" id="acct-username" placeholder="登录用户名">
        </div>
        <div class="form-group">
          <label class="form-label">初始密码<span class="req">*</span></label>
          <input type="password" class="form-input" id="acct-password" placeholder="至少 8 位，不能是纯数字" autocomplete="new-password">
          <p class="form-hint">由管理员当面设置并转交本人，首次登录后请立即修改；系统不再使用统一默认口令。</p>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label class="form-label">姓名<span class="req">*</span></label>
          <input type="text" class="form-input" id="acct-name" placeholder="请输入姓名">
        </div>
        <div class="form-group">
          <label class="form-label">角色<span class="req">*</span></label>
          <select class="form-select" id="acct-role">
            ${availableRoles.map(r => `<option value="${r.value}">${r.label}</option>`).join('')}
          </select>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group" id="acct-district-group">
          <label class="form-label">所属区县<span class="req">*</span></label>
          <select class="form-select" id="acct-district">
            <option value="">请选择</option>
          </select>
          <p class="form-hint" id="acct-district-hint">请先选择角色</p>
        </div>
        <div class="form-group">
          <label class="form-label">联系电话</label>
          <input type="text" class="form-input" id="acct-phone" placeholder="请输入电话">
        </div>
      </div>
      <div class="form-group" id="acct-institution-group" style="display:none;">
        <label class="form-label">关联机构<span class="req">*</span></label>
        <select class="form-select" id="acct-institution">
          <option value="">请选择</option>
        </select>
        <p class="form-hint" id="acct-institution-hint"></p>
      </div>
      <div class="form-group" id="acct-license-group" style="display:none;">
        <label class="form-label">执业兽医师证号<span class="req">*</span></label>
        <input type="text" class="form-input" id="acct-vet-license" placeholder="按证件原文填写，如：鄂兽医执字第 20210356 号" maxlength="100">
        <p class="form-hint">用于验证操作人的执业身份：这张单子的诊疗单、处方与绝育证明上都会印这串号，
          并<strong>按开单当时快照</strong>保存 —— 以后换证、改号不会回头改历史单据。</p>
      </div>
      <p class="form-hint">说明：领养人不在平台端创建，由捕捉点在领养登记时自动生成。</p>
    `;

    const m = TNR_UI.modal({
      title: '新增账号', body, size: 'lg',
      actions: `<button class="btn btn-secondary" data-action="cancel">取消</button><button class="btn btn-primary" id="acctSave">创建</button>`
    });
    m.overlay.querySelector('[data-action="cancel"]').addEventListener('click', m.close);

    // 角色变更时联动区县选项 + 机构选项
    const roleSelect = m.overlay.querySelector('#acct-role');
    const districtSelect = m.overlay.querySelector('#acct-district');
    const districtHint = m.overlay.querySelector('#acct-district-hint');
    const instGroup = m.overlay.querySelector('#acct-institution-group');
    const instSelect = m.overlay.querySelector('#acct-institution');
    const instHint = m.overlay.querySelector('#acct-institution-hint');
    const districtGroup = m.overlay.querySelector('#acct-district-group');
    const licenseGroup = m.overlay.querySelector('#acct-license-group');

    /* 机构清单在模态框生命周期内不变，按角色懒加载一次并缓存 ——
     * 区县每切一次都重新打接口会产生竞态（后发的请求先返回，
     * 用旧区县的清单覆盖新区县的），而且切换快时白等网络。
     */
    const instCache = { shelter: null, hospital: null };
    const loadInstitutions = async (type) => {
      if (!instCache[type]) {
        try {
          const list = await TNR_API.getInstitutionsStrict(type);
          instCache[type] = (list || []).filter(i => i.status === 'active');
        } catch (e) {
          instCache[type] = null;
          throw e;
        }
      }
      return instCache[type];
    };

    /* 关联机构按「所选区县」逐级筛选 —— 本表单最容易出错的一处。
     *
     * 服务端 `validate_operator_district()` 要求「账号所属区县」与「机构所在区县」
     * 自洽：医院必须**完全相等**；捕捉点挂具体区县时也必须相等（挂市级另说，见下）。
     * 前端若把全部机构不分区县地列出来，用户会按机构名去选、区县留在默认值，
     * 提交时撞 400「账号所属区县（襄城区）与机构所在区县（樊城区）不一致」——
     * 白填一遍，而且提示里的两个区县名跟他在下拉里看到的值对不上，无从下手。
     *
     * 反过来，「选了市级却还能选区县捕捉点」更隐蔽：所属区县是市级 ⇒
     * `has_global_district_scope()` 为 True ⇒ 读取范围是**全市**。
     * 这样的账号会「看到全市档案、以某个区县捕捉点的身份操作」，
     * 不报错、不 500，数据隔离已经破了。所以市级只列市级捕捉点。
     */
    const refreshInstitutionOptions = async () => {
      const role = roleSelect.value;
      instSelect.innerHTML = '<option value="">请选择</option>';
      if (role !== 'shelter' && role !== 'hospital') { instHint.textContent = ''; return; }

      const type = role === 'shelter' ? 'shelter' : 'hospital';
      const label = TNR_UI.instTypeText(type);
      let all;
      try {
        all = await loadInstitutions(type);
      } catch (e) {
        instHint.textContent = '机构数据加载失败：' + (e.message || '请稍后重试');
        return;
      }
      const districtId = districtSelect.value;
      const matched = districtId
        ? all.filter(i => String(i.district_id) === String(districtId))
        : [];

      matched.forEach(i => {
        const opt = document.createElement('option');
        opt.value = i.id;
        opt.textContent = i.name + '（' + (i.district_name || '') + '）';
        instSelect.appendChild(opt);
      });

      if (!districtId) {
        instHint.textContent = '请先选择所属区县，机构会按区县筛选';
      } else if (!matched.length) {
        // 空下拉**不能静默**：用户会以为「系统里没有机构」，
        // 实际是该区县没有启用的捕捉点/医院 —— 两种情况的处置完全不同。
        instHint.textContent = '所选区县暂无启用的' + label
          + '，请先在「机构管理」中添加，或改选其他区县';
      } else {
        instHint.textContent = '只列出与「所属区县」一致的' + label
          + '（共 ' + matched.length + ' 个）';
      }
    };

    const updateDistrictOptions = async () => {
      const role = roleSelect.value;
      districtSelect.innerHTML = '<option value="">请选择</option>';
      instGroup.style.display = 'none';
      instSelect.innerHTML = '<option value="">请选择</option>';
      districtGroup.style.display = 'block';
      // 执业证号只有医院操作员该填（`services.validate_vet_license`：非医院角色
      // 填了会被服务端拒）。这里跟着角色收起，是为了不让人把捕捉点操作员
      // 的姓名抄进证号框，而不是为了让界面看起来干净。
      licenseGroup.style.display = role === 'hospital' ? 'block' : 'none';

      if (role === 'platform_admin') {
        // 平台管理员是全局角色：可见范围由 `GLOBAL_SCOPE_ROLES` 决定，与区县无关，
        // 所以**没有**区县可填。这里若照搬「必须选区县」，在没有市级区县的库上
        // 会直接建不出平台账号（下拉为空 → 校验拦下 → 永远提交不了）。
        districtGroup.style.display = 'none';
        districtHint.textContent = '平台管理员为全局角色，不受区县约束';
      } else if (role === 'gov_city') {
        // 市级政府管理员 → 必须选市级区县
        cityDistricts.forEach(d => {
          const opt = document.createElement('option');
          // 区县名本身可能已经带「市级」字样（现场数据叫「全市（市级）」），
          // 无条件拼后缀会渲染成「全市（市级）（市级）」。
          opt.value = d.id;
          opt.textContent = /市级|全市/.test(d.name) ? d.name : d.name + '（市级）';
          districtSelect.appendChild(opt);
        });
        districtHint.textContent = TNR_UI.roleText('gov_city') + '必须选市级区县';
      } else if (role === 'shelter') {
        /* 捕捉点操作员：服务端允许挂**市级或具体区县**（`user_create` 第 668 行
         * 「shelter 可挂市级或区县，不做限制」）。原先这里只列 `cityDistricts`，
         * 于是「区县级捕捉点操作员」在界面上**根本建不出来** ——
         * 服务端允许、前端藏起来，属于「静默做不到」，比撞 400 更难发现。
         */
        if (this.isGlobalScope()) {
          // 只有平台管理员能建「挂市级捕捉点」的账号；区级政府管理员
          // 建市级会被 `validate_user_manage_scope()` 判「无权管理其他区县的账号」，
          // 所以非全局角色不列市级。
          cityDistricts.forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.id;
            opt.textContent = /市级|全市/.test(d.name) ? d.name : d.name + '（市级）';
            districtSelect.appendChild(opt);
          });
        }
        realDistricts.forEach(d => {
          const opt = document.createElement('option');
          opt.value = d.id; opt.textContent = d.name;
          districtSelect.appendChild(opt);
        });
        districtHint.textContent = '市级捕捉点选「全市（市级）」，区县捕捉点选对应区县；关联机构会按区县筛选';
      } else if (role === 'gov_district' || role === 'hospital') {
        // 区级角色 → 必须选具体区县，不能选市级
        realDistricts.forEach(d => {
          const opt = document.createElement('option');
          opt.value = d.id; opt.textContent = d.name;
          districtSelect.appendChild(opt);
        });
        districtHint.textContent = role === 'hospital'
          ? '医院操作员必须选具体区县；关联机构会按所选区县筛选'
          : TNR_UI.roleText('gov_district') + '必须选具体区县，不能选市级';
      } else {
        districtHint.textContent = '请先选择角色';
      }

      // shelter/hospital 需要关联机构
      if (role === 'shelter' || role === 'hospital') {
        instGroup.style.display = 'block';
        await refreshInstitutionOptions();
      }
    };

    roleSelect.addEventListener('change', updateDistrictOptions);
    // 区县变了必须重刷机构：不重刷就会留下「区县=樊城区、机构=爱心宠物医院」
    // 这种自相矛盾的组合，提交时被服务端拦下。
    districtSelect.addEventListener('change', refreshInstitutionOptions);
    // 初始化一次
    await updateDistrictOptions();

    m.overlay.querySelector('#acctSave').addEventListener('click', async () => {
      const username = document.getElementById('acct-username').value.trim();
      const name = document.getElementById('acct-name').value.trim();
      const role = document.getElementById('acct-role').value;
      if (!username) { TNR_UI.toast('请输入用户名', 'warning'); return; }
      if (!name) { TNR_UI.toast('请输入姓名', 'warning'); return; }

      const password = document.getElementById('acct-password').value.trim();
      if (!password) { TNR_UI.toast('请设置初始密码', 'warning'); return; }
      if (password.length < 8) { TNR_UI.toast('密码长度不能少于8位', 'warning'); return; }

      const districtId = document.getElementById('acct-district').value;
      // 平台管理员无区县（见 updateDistrictOptions 里的说明）
      if (role !== 'platform_admin' && !districtId) { TNR_UI.toast('请选择所属区县', 'warning'); return; }

      const phone = document.getElementById('acct-phone').value.trim();
      if (phone && !phone.replace(/[-+]/g, '').match(/^\d+$/)) { TNR_UI.toast('联系电话格式不正确', 'warning'); return; }

      const institutionId = document.getElementById('acct-institution').value || null;
      if ((role === 'shelter' || role === 'hospital') && !institutionId) {
        TNR_UI.toast('请选择关联机构', 'warning'); return;
      }

      const vetLicense = document.getElementById('acct-vet-license').value.trim();
      // 医院操作员的证号是诊疗单签名归属的唯一来源，前端这里只是别让人白跑一趟；
      // 真判据在服务端 `validate_vet_license`（直接 POST 也会拒）。
      if (role === 'hospital' && !vetLicense) {
        TNR_UI.toast('请输入执业兽医师证号', 'warning'); return;
      }

      try {
        TNR_UI.assertOk(await TNR_API.createUser({
          username, password, name, role,
          district_id: districtId, institution_id: institutionId, phone,
          vet_license: vetLicense
        }), '账号创建失败');
        TNR_UI.toast('账号创建成功', 'success');
        m.close();
        await this.refreshAccountsTable();
      } catch (e) {
        TNR_UI.toast(e.message || '创建失败', 'danger');
      }
    });
  },

  /* 账号纠错模态框：只开放「抄错」的三类字段。
   * 角色 / 区县 / 机构 / 口令 / 启停**不在这个入口** —— 它们改的是账号能看什么、
   * 能代表谁，与服务端 `user_update` 的收窄范围必须一致；这里多给一个输入框，
   * 就等于把一个越权面包装成「编辑」按钮。
   */
  showAccountEditModal(row) {
    const body = `
      <div class="form-group">
        <label class="form-label">姓名<span class="req">*</span></label>
        <input type="text" class="form-input" id="acctEdit-name" value="${TNR_UI.escape(row.name || '')}">
      </div>
      <div class="form-group">
        <label class="form-label">联系电话</label>
        <input type="text" class="form-input" id="acctEdit-phone" value="${TNR_UI.escape(row.phone || '')}">
      </div>
      ${row.role === 'hospital' ? `
      <div class="form-group">
        <label class="form-label">执业兽医师证号<span class="req">*</span></label>
        <input type="text" class="form-input" id="acctEdit-license" maxlength="100"
               value="${TNR_UI.escape(row.vet_license || '')}">
        <p class="form-hint">改证号只影响之后的新单据：已开出的诊疗单上仍是当时快照的旧号，
          这是监管要求的（否则历史单据将无法证明当时由哪位执业兽医师签字）。</p>
      </div>` : ''}
      <p class="form-hint">角色、所属区县、所属机构、密码与启停不在本入口修改。</p>
    `;
    const m = TNR_UI.modal({
      title: `编辑账号 · ${TNR_UI.escape(row.username)}`, body, size: 'md',
      actions: `<button class="btn btn-secondary" data-action="cancel">取消</button><button class="btn btn-primary" id="acctEditSave">保存</button>`
    });
    m.overlay.querySelector('[data-action="cancel"]').addEventListener('click', m.close);
    m.overlay.querySelector('#acctEditSave').addEventListener('click', async () => {
      const name = m.overlay.querySelector('#acctEdit-name').value.trim();
      if (!name) { TNR_UI.toast('请输入姓名', 'warning'); return; }
      const phone = m.overlay.querySelector('#acctEdit-phone').value.trim();
      if (phone && !phone.replace(/[-+]/g, '').match(/^\d+$/)) { TNR_UI.toast('联系电话格式不正确', 'warning'); return; }
      const payload = { name, phone };
      if (row.role === 'hospital') {
        payload.vet_license = m.overlay.querySelector('#acctEdit-license').value.trim();
        if (!payload.vet_license) { TNR_UI.toast('请输入执业兽医师证号', 'warning'); return; }
      }
      try {
        TNR_UI.assertOk(await TNR_API.updateUser(row.id, payload), '保存失败');
        TNR_UI.toast('已保存', 'success');
        m.close();
        await this.refreshAccountsTable();
      } catch (e) {
        TNR_UI.toast(e.message || '保存失败', 'danger');
      }
    });
  },

  async toggleAccountStatus(id) {
    try {
      const res = await TNR_API.toggleUser(id);
      TNR_UI.assertOk(res, '操作失败');
      TNR_UI.toast(res.message || '操作成功', 'success');
      await this.refreshAccountsTable();
    } catch (e) {
      TNR_UI.toast(e.message || '操作失败', 'danger');
    }
  },
  /* ========== 7. 系统配置 ========== */
  async render_settings() {
    const el = document.getElementById('page-settings');
    const tabs = [
      { id: 'prefix', label: '编号规则配置' },
      { id: 'logs', label: '操作日志' },
      { id: 'params', label: '系统参数' },
      { id: 'notice', label: '公告发布' }
    ];
    el.innerHTML = `
      <div class="tabs">
        ${tabs.map(t => `<div class="tab-item ${this.settingsState.tab === t.id ? 'active' : ''}" data-settings-tab="${t.id}">${t.label}</div>`).join('')}
      </div>
      <div id="settingsContent"></div>
    `;
    el.querySelectorAll('[data-settings-tab]').forEach(tab => {
      tab.addEventListener('click', () => { this.settingsState.tab = tab.dataset.settingsTab; this.render_settings(); });
    });
    await this.renderSettingsContent();
  },

  async renderSettingsContent() {
    const el = document.getElementById('settingsContent');
    const tab = this.settingsState.tab;

    if (tab === 'prefix') {
      // 编号规则：市级可改，区级只读。读/写权限在后端也是分开的
      // （GET 允许两级、POST 仅市级），此处门禁必须与服务端一致。
      const canEdit = this.isGlobalScope();
      let config = null;
      try {
        // 必须用 TNR_API.get（失败会抛错）而不是 getSystemConfig ——
        // 后者经 `_get()` 在非 2xx 时静默返回 []，会把真实的 CAP/TRF/…
        // 前缀渲染成空串，看上去像「编号规则没配置」（兜底谎报）。
        const json = await TNR_API.get('/api/supervision/config/');
        config = json.data || {};
      } catch (e) {
        this.showLoadError(el, '编号规则加载失败', e, () => this.renderSettingsContent());
        return;
      }
      const prefixes = [
        { type: '捕捉单', key: 'capture_prefix', example: 'CAP260001' },
        { type: '转运单', key: 'transfer_prefix', example: 'TRF260001' },
        { type: '领回单', key: 'owner_return_prefix', example: 'RET260001' },
        { type: '放归单', key: 'release_prefix', example: 'REL260001' },
        { type: '领养单', key: 'adoption_prefix', example: 'ADP260001' },
        { type: '安乐死单', key: 'euthanasia_prefix', example: 'EUT260001' },
        { type: '诊疗单', key: 'treatment_prefix', example: 'TRE260001' },
        { type: '物料采购入库单', key: 'purchase_prefix', example: 'PUR260001' },
        { type: '物料下发单', key: 'dispatch_prefix', example: 'DIS260001' },
        { type: '物料接收单', key: 'receive_prefix', example: 'RCV260001' },
        { type: '物料异动单', key: 'adjustment_prefix', example: 'ADJ260001' },
        { type: '物料消耗记录', key: 'consume_prefix', example: 'CON260001' }
      ];
      el.innerHTML = `
        <div class="card">
          ${canEdit ? `<div class="card-header" style="justify-content:flex-end;">
            <button class="btn btn-primary btn-sm" id="btnSavePrefixes">保存配置</button>
          </div>` : ''}
          <div class="card-body card-body-flush">
            <div class="table-wrapper" style="border:none;">
              <table class="data-table">
                <thead><tr><th>业务类型</th><th>前缀</th><th>示例</th></tr></thead>
                <tbody>
                  ${prefixes.map(p => `
                    <tr>
                      <td>${p.type}</td>
                      <td><input type="text" class="form-input" style="width:120px;" value="${TNR_UI.escape(config[p.key] || '')}" data-prefix-key="${p.key}"${canEdit ? '' : ' readonly'}></td>
                      <td class="text-muted">${p.example}</td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>
        </div>
        ${canEdit ? '' : '<p class="form-hint mt-3">说明：仅平台管理员可修改编号规则配置。</p>'}
      `;
      if (canEdit) document.getElementById('btnSavePrefixes').addEventListener('click', async () => {
        const payload = {};
        document.querySelectorAll('[data-prefix-key]').forEach(input => {
          payload[input.dataset.prefixKey] = input.value.trim();
        });
        try {
          TNR_UI.assertOk(await TNR_API.updateSystemConfig(payload), '保存失败');
          TNR_UI.toast('编号规则已保存', 'success');
        } catch (e) {
          TNR_UI.toast(e.message || '保存失败', 'danger');
        }
      });
    } else if (tab === 'logs') {
      el.innerHTML = '<div class="table-empty"><div class="table-empty-icon">⏳</div><div class="table-empty-text">加载中...</div></div>';
      try {
        const logs = await TNR_API.getOperationLogsStrict();
        /* 原先这里是**手写**的筛选条（只有一个关键词输入），所以它拿不到
           `renderFilterBar` 统一追加的起止时间。改用公共渲染器，顺带把
           「搜索/重置」按钮也补齐（手写版没有按钮，只能靠防抖触发）。 */
        el.innerHTML = `
          ${TNR_UI.renderFilterBar([
            { key: 'logsearch', label: '搜索', type: 'search', placeholder: '操作/详情/操作人' }
          ])}
          <div class="card">
            <div class="card-body card-body-flush" id="logList">
              ${this.renderLogList(logs || [])}
            </div>
          </div>
        `;
        TNR_UI.bindFilter(() => {
          const f = TNR_UI.getFilterValues(el);
          // 日志的时间口径 = `action_time`（后端 `with_camel_keys` 同时给了 `actionTime`）。
          let filtered = (logs || []).filter(l =>
            TNR_UI.inDateRange(l.actionTime || l.action_time, f.start_date, f.end_date));
          if (f.logsearch) filtered = filtered.filter(l =>
            this.logSearchFields(l).some(v => v.includes(f.logsearch))
          );
          document.getElementById('logList').innerHTML = this.renderLogList(filtered);
        }, el);
      } catch (e) {
        this.showLoadError(el, '操作日志加载失败', e, () => this.renderSettingsContent());
      }
    } else if (tab === 'params') {
      el.innerHTML = `
        <div class="card">
          <div class="card-header">
            <div class="card-title"><span class="card-title-bar"></span>系统参数</div>
          </div>
          <div class="card-body">
            ${TNR_UI.renderDescList([
              { label: '系统名称', value: 'TNR流浪动物管理系统' },
              { label: '当前版本', value: 'v1.0.0' },
              { label: '部署环境', value: 'Django 5.0 生产环境' },
              { label: '数据存储', value: 'PostgreSQL / SQLite' },
              { label: '当前用户', value: this.currentUser.name || this.currentUser.username },
              { label: '用户角色', value: this.currentUser.role_display || TNR_UI.roleText(this.currentUser.role) },
              { label: '管辖范围', value: '全部区县（平台管理员不受区县约束）' }
            ])}
          </div>
        </div>
      `;
    } else if (tab === 'notice') {
      /* 公告发布（第三十七轮）。
       *
       * ⚠ 读历史列表走 `getNotices()`（失败**抛错**）而不是 `_get()`：
       * 加载失败若被渲染成「还没发过公告」，发布方会以为公告没发出去、
       * 于是**重复广播** —— 而广播是不可撤回的。
       */
      let notices = [];
      let districts = [];
      try {
        notices = (await TNR_API.getNotices()) || [];
        /* ⚠ 区县下拉走 `getDistrictsStrict()`（失败**抛错**），不是 `getDistricts()`：
         * 后者经 `_get()` 在非 2xx 时**静默返回 `[]`** —— 下拉只剩「全市」一项，
         * 界面上看不出少了什么，但「发区县公告」这条路径就整条不可达了。
         * （`district_id` 是本轮新加的显式参数：平台管理员不挂区县，
         *   旧口径下它只能发全市公告 —— 见 `notice_publish` 的注释。） */
        districts = (await TNR_API.getDistrictsStrict()) || [];
      } catch (e) {
        this.showLoadError(el, '公告数据加载失败', e, () => this.renderSettingsContent());
        return;
      }
      /* 「市级」区划在服务端等价于「全市」（`notice_publish` 里 `is_city` 归一成
         `None`），所以这里把它排除掉、只留一个「全市」选项 —— 否则下拉里会出现
         两个语义相同的项，而选「市级」和选「全市」写出来的公告归属不一样。 */
      const targetDistricts = (districts || [])
        .filter(d => d.status === 'active' && !d.is_city);
      el.innerHTML = `
        <div class="card">
          <div class="card-header"><div class="card-title"><span class="card-title-bar"></span>发布公告</div></div>
          <div class="card-body">
            <div class="form-group">
              <label class="form-label">标题<span class="req">*</span></label>
              <input class="form-input" id="noticeTitle" maxlength="100" placeholder="如：关于加强秋季防疫工作的通知">
            </div>
            <div class="form-group">
              <label class="form-label">内容<span class="req">*</span></label>
              <textarea class="form-textarea" id="noticeContent" maxlength="2000" rows="5" placeholder="请填写公告正文"></textarea>
            </div>
            <div class="form-group">
              <label class="form-label">接收对象</label>
              <select class="form-select" id="noticeTarget">
                <option value="adopter">全部领养人</option>
              </select>
            </div>
            <div class="form-group">
              <label class="form-label">发布范围</label>
              <select class="form-select" id="noticeDistrict">
                <option value="">全市（全部区县）</option>
                ${targetDistricts.map(d => `<option value="${d.id}">${TNR_UI.escape(d.name)}</option>`).join('')}
              </select>
            </div>
            <div class="form-hint">按「发布范围」确定受众：选「全市」发给所有领养人，选具体区县则只发给该区县有领养记录的用户。</div>
            <div style="display:flex;justify-content:flex-end;margin-top:var(--sp-4);">
              <button class="btn btn-primary" id="btnPublishNotice">发布公告</button>
            </div>
          </div>
        </div>
        <div class="card mt-5">
          <div class="card-header"><div class="card-title"><span class="card-title-bar"></span>已发布公告</div></div>
          <div class="card-body card-body-flush" id="noticeListArea">
            ${TNR_UI.renderFilterBar([
              { key: 'nkw', label: '搜索', type: 'search', placeholder: '公告标题 / 内容' }
            ])}
            <div id="noticeTable"></div>
          </div>
        </div>`;
      /* 「已发布公告」原先**没有筛选条**（是本次统一补的），所以这里必须自己
         建一个局部渲染函数 —— 与其它列表一样把过滤后的数组交给 `mountTable`。
         时间口径取 `last_at`（最后一次广播时间；后端 `with_camel_keys` 同时给
         `lastAt`），语义上等同于「这条公告是什么时候发出去的」。
         作用域限定在 `#noticeListArea`：本页的筛选条 key 是 `nkw`，与别页不冲突，
         但**限定作用域仍然必要** —— 各 `.page-view` 常驻 DOM，全局裸查会读到别页残留值。 */
      const noticeArea = document.getElementById('noticeListArea');
      /* 发布范围一列：后端按 `title + content + district_id` 聚合，同名同文但发给
         不同区县的是**两条**公告 —— 列表必须把范围显示出来，否则运营看到两行
         一模一样的内容，会以为是重复渲染。`null` = 全市。 */
      const noticeDistrictMap = {};
      (districts || []).forEach(d => { noticeDistrictMap[d.id] = d.name; });
      const renderNoticeTable = () => {
        const f = TNR_UI.getFilterValues(noticeArea);
        const rows = (notices || []).filter(r =>
          TNR_UI.inDateRange(r.lastAt || r.last_at, f.start_date, f.end_date)
          && (!f.nkw || [r.title, r.content]
            .some(v => String(v || '').toLowerCase().includes(f.nkw)))
        );
        TNR_UI.mountTable('noticeTable', {
          id: 'platform.notice.list',
          columns: [
            { title: '标题', key: 'title' },
            { title: '内容', render: r => {
              const c = String(r.content || '');
              return TNR_UI.escape(c.slice(0, 40)) + (c.length > 40 ? '…' : '');
            }},
            { title: '发布范围', render: r => {
              const did = r.districtId ?? r.district_id;
              return did ? TNR_UI.escape(noticeDistrictMap[did] || '—') : '全市';
            }},
            { title: '触达人数', render: r => (r.sentCount ?? r.sent_count ?? 0) + ' 人' },
            { title: '发布时间', render: r => String(r.lastAt || r.last_at || '').replace('T', ' ').slice(0, 16) }
          ],
          data: rows, emptyText: notices.length ? '当前条件下暂无公告' : '暂无公告'
        });
      };
      renderNoticeTable();
      TNR_UI.bindFilter(renderNoticeTable, noticeArea);
      el.querySelector('#btnPublishNotice')
        ?.addEventListener('click', () => this.publishNotice());
    }
  },

  /* 发布公告。
   *
   * 服务端会做**全部**校验（长度 / 角色白名单 / 区县范围），前端只拦
   * 「空字段」——避免用户白填一次表单，但**不重复实现业务判据**：
   * 两处各写一份长度上限，迟早会漂移成「前端放行、后端拒绝」。
   */
  async publishNotice() {
    const title = (document.getElementById('noticeTitle')?.value || '').trim();
    const content = (document.getElementById('noticeContent')?.value || '').trim();
    const targetRole = document.getElementById('noticeTarget')?.value || 'adopter';
    const districtSel = document.getElementById('noticeDistrict');
    const districtId = districtSel ? districtSel.value : '';
    /* 发布范围必须写进确认框：广播**不可撤回**，而「全市」与「某个区县」
       在按钮上长得一模一样 —— 少了这一行，一次误选就会发给所有人。 */
    const scopeText = districtId
      ? TNR_UI.escape(districtSel.options[districtSel.selectedIndex]?.text || '指定区县')
      : '全市（全部区县）';
    if (!title) { TNR_UI.toast('请填写公告标题', 'warning'); return; }
    if (!content) { TNR_UI.toast('请填写公告内容', 'warning'); return; }

    const ok = await TNR_UI.confirm(
      '确定发布公告 <b>' + TNR_UI.escape(title) + '</b> 吗？<br>'
      + '发布范围：<b>' + scopeText + '</b><br>'
      + '<span style="color:var(--ink-muted);font-size:13px;">'
      + '公告一经发布<strong>无法撤回</strong>，接收人会立即在消息中心看到。</span>',
      '发布确认');
    if (!ok) return;

    try {
      const payload = { title: title, content: content, target_role: targetRole };
      // 空串 = 全市（不传 `district_id`）。**不要**传 `0` / `null` ——
      // 服务端把「非空但非法」的值当 400 处理（防误广播），只有缺省才是全市。
      if (districtId) payload.district_id = Number(districtId);
      const res = await TNR_API.publishNotice(payload);
      if (res && res.success) {
        TNR_UI.toast(res.message || '公告已发布', 'success');
        this.render_settings();
      } else {
        TNR_UI.toast((res && res.message) || '发布失败', 'danger');
      }
    } catch (e) {
      TNR_UI.toast(e.message || '发布失败', 'danger');
    }
  },

  renderLogList(logs) {
    if (!logs || logs.length === 0) return '<div class="table-empty"><div class="table-empty-icon">📋</div><div class="table-empty-text">暂无操作日志</div></div>';
    const flagMap = { 1: '新增', 2: '修改', 3: '删除' };
    return logs.map(log => {
      const action = log.actionLabel || flagMap[log.actionFlag] || '操作';
      const module = log.module ? `[${log.module}] ` : '';
      // summary 已由后端拼成「模块·动作 对象标识」，再单独拼一遍 objectRepr
      // 会显示成「审计演示123 黑名单·新增 审计演示123」。只有摘要里没带上
      // 对象标识时（例如区县删除这种自定义摘要）才补在后面。
      const summary = log.summary || '';
      const detail = (log.objectRepr && !summary.includes(log.objectRepr))
        ? `${summary} ${log.objectRepr}`.trim()
        : summary || log.objectRepr || '';
      const failTag = log.success === false ? ' <span class="log-fail">失败</span>' : '';
      const who = [log.userName, log.districtName].filter(Boolean).join(' · ');
      return `
      <div class="log-entry">
        <span class="log-time">${TNR_UI.formatDateTime(log.actionTime)}</span>
        <span class="log-action">${TNR_UI.escape(module + action)}</span>
        <span class="log-detail">${TNR_UI.escape(detail)}${failTag}</span>
        <span class="text-muted fs-sm">${TNR_UI.escape(who)}</span>
      </div>
    `;
    }).join('');
  },
  // 日志搜索的可检索字段。后端返回的字段名是 actionLabel / module / objectRepr …
  // 早期这里按 `l.action` 过滤，而后端**从未返回过 `action`** —— 搜索框提示写着
  // 「操作/详情/操作人」，但「操作」永远搜不到（空结果，且不报错）。
  logSearchFields(log) {
    return [log.actionLabel, log.module, log.objectRepr, log.summary,
            log.detail, log.userName, log.username, log.districtName]
      .map(v => (v || '').toLowerCase());
  }
};

/* ============================================
   启动
   ============================================ */
document.addEventListener('DOMContentLoaded', () => {
  PlatformPortal.init();
});
