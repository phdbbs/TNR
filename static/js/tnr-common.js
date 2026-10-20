/* ============================================
   TNR 流浪动物管理系统 - 通用 UI 组件库
   ============================================ */

const TNR_UI = {

  // === Toast 通知 ===
  /* 短时间内的**同文案** toast 只弹一次（第三十七轮）。
   *
   * 场景：会话过期时门户首屏的多个并发请求同时拿到 401，
   * `TNR_API._handleUnauthorized()` 与各调用点的 `catch` 会各弹一次，
   * 用户看到同一句话叠 2~3 个。
   *
   * 窗口取 1 秒：足够吞掉并发重复，又不至于把「用户连续两次同样的操作失败」
   * 这种**真实**的第二次反馈吃掉。
   */
  _recentToasts: {},
  toast(message, type = 'success', duration = 3000) {
    const key = type + '|' + message;
    const now = Date.now();
    if (this._recentToasts[key] && now - this._recentToasts[key] < 1000) return;
    this._recentToasts[key] = now;
    let container = document.querySelector('.toast-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
    const icons = { success: '✓', warning: '⚠', danger: '✕', info: 'ℹ' };
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    // message 必须走 **textContent**，不能用 innerHTML 拼串。
    // 服务端大量把用户输入原样回显在 message 里，例如
    // `raise ValueError(f'去向建议「{advised}」不合法')`、
    // `f'还有 N 项未完成：{物料名}'` —— 用 innerHTML 就等于
    // 「请求体 → 界面 HTML」的反射型 XSS 通道。图标是常量，留在 HTML 里没问题。
    const icon = document.createElement('span');
    icon.className = 'toast-icon';
    icon.textContent = icons[type] || '✓';
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message == null ? '' : String(message);
    toast.append(icon, text);
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(120%)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, duration);
  },

  /* === 写接口结果断言 ===
   *
   * `TNR_API._post` / `_get` **不会抛异常** —— 它们只把响应体原样返回
   * （`_post` 是 `return res.json()`）。所以下面这种写法里的 catch 是**死代码**：
   *
   *     try {
   *       await TNR_API.createUser({...});
   *       TNR_UI.toast('账号创建成功', 'success');   // ← 服务端拒绝时照样弹
   *     } catch (e) { TNR_UI.toast(e.message, 'danger'); }
   *
   * 本项目的业务错误一律以 **HTTP 200 + `{success:false, message}`** 返回
   * （`json_fail`），越权才用 404。因此服务端每一次拒绝（用户名已存在、
   * 库存不足、无权操作、跨区县…）界面上都会弹绿色「成功」，用户以为存上了，
   * 实际什么都没写 —— 比直接报错更糟。
   *
   * 所有写操作拿到响应后必须过一遍本函数，把失败转成异常交给 catch：
   *
   *     const res = await TNR_API.createUser({...});
   *     TNR_UI.assertOk(res, '创建失败');
   *
   * 读接口（列表/详情）不适用：失败时静默降级成空列表比整页崩掉更可接受。
   */
  assertOk(res, fallback = '操作失败') {
    if (res && res.success === false) throw new Error(res.message || fallback);
    return res;
  },

  // === 确认对话框 ===
  confirm(message, title = '确认操作') {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay show';
      overlay.style.zIndex = '2000';
      overlay.innerHTML = `
        <div class="modal" style="max-width:420px;">
          <div class="modal-header">
            <div class="modal-title">${title}</div>
          </div>
          <div class="modal-body">
            <p style="font-size:14px;color:var(--ink-text);line-height:1.8;">${message}</p>
          </div>
          <div class="modal-footer">
            <button class="btn btn-secondary" data-action="cancel">取消</button>
            <button class="btn btn-primary" data-action="ok">确认</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      // 逐个绑定：querySelector 只命中第一个，同名按钮（页脚"取消"）会失效
      overlay.querySelectorAll('[data-action="cancel"]').forEach(btn => {
        btn.onclick = () => { overlay.remove(); resolve(false); };
      });
      overlay.querySelectorAll('[data-action="ok"]').forEach(btn => {
        btn.onclick = () => { overlay.remove(); resolve(true); };
      });
    });
  },

  // === 模态框 ===
  modal({ title, body, size = '', actions = null, onShow = null }) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay show';
    overlay.style.zIndex = '1500';
    const sizeClass = size === 'lg' ? 'modal-lg' : (size === 'xl' ? 'modal-xl' : '');
    overlay.innerHTML = `
      <div class="modal ${sizeClass}">
        <div class="modal-header">
          <div class="modal-title">${title}</div>
          <button class="modal-close" data-action="close">&times;</button>
        </div>
        <div class="modal-body">${typeof body === 'string' ? body : ''}</div>
        ${actions ? `<div class="modal-footer">${actions}</div>` : ''}
      </div>
    `;
    document.body.appendChild(overlay);

    let escHandler = null;
    const closeFn = () => {
      overlay.remove();
      if (escHandler) { document.removeEventListener('keydown', escHandler); escHandler = null; }
    };
    // 关键修复：弹窗头部 × 与页脚「取消」都带 data-action="close"，
    // 之前用 querySelector 只给第一个绑定，导致「取消」按钮点了没反应。
    overlay.querySelectorAll('[data-action="close"]').forEach(btn => { btn.onclick = closeFn; });
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeFn(); });
    // 键盘 Esc 关闭
    escHandler = (e) => { if (e.key === 'Escape') closeFn(); };
    document.addEventListener('keydown', escHandler);

    if (typeof body === 'function') {
      const bodyEl = overlay.querySelector('.modal-body');
      body(bodyEl, closeFn);
    }

    if (onShow) onShow(overlay);
    return { overlay, close: closeFn };
  },

  // === 抽屉 ===
  drawer({ title, body }) {
    const overlay = document.createElement('div');
    overlay.className = 'drawer-overlay show';
    // z-index 使用 CSS 默认(1000)，保证抽屉本体(1001)在遮罩之上，避免内容被遮罩模糊
    const drawerEl = document.createElement('div');
    drawerEl.className = 'drawer show';
    drawerEl.innerHTML = `
      <div class="drawer-header">
        <div class="drawer-title">${title}</div>
        <button class="modal-close" data-action="close">&times;</button>
      </div>
      <div class="drawer-body"></div>
    `;
    document.body.appendChild(overlay);
    document.body.appendChild(drawerEl);

    let escHandler = null;
    const closeFn = () => {
      overlay.remove();
      drawerEl.remove();
      if (escHandler) { document.removeEventListener('keydown', escHandler); escHandler = null; }
    };
    // 与 modal 保持一致：抽屉正文里若也放了「关闭/取消」按钮，需一并绑定，
    // 否则只有页头 × 可关闭（正文按钮点了没反应）。
    drawerEl.querySelectorAll('[data-action="close"]').forEach(btn => { btn.onclick = closeFn; });
    overlay.onclick = closeFn;
    // 键盘 Esc 关闭
    escHandler = (e) => { if (e.key === 'Escape') closeFn(); };
    document.addEventListener('keydown', escHandler);

    const bodyEl = drawerEl.querySelector('.drawer-body');
    if (typeof body === 'string') bodyEl.innerHTML = body;
    else if (typeof body === 'function') body(bodyEl, closeFn);

    return { drawerEl, close: closeFn };
  },

  // === 芯片「首号 + 数量」配套组件（捕捉点 / 医院两端共用） ===
  // 三个方法的文案与判据只有**这一份实现** —— 两端各自写一遍的话，
  // 追溯时间线的标题、状态徽章迟早漂移成两种口径。

  /* 实时预览（oninput 防抖调 `chips/preview/`）。
   * 预检与提交走**服务端同一判据**，前端只做展示、不另立判据；
   * `purpose`：purchase / dispatch / void；后两者经 `getExtra()` 动态带
   * `material_id`（物料下拉随时会换）。返回 `{rerun}` 供物料变化时手动重跑。 */
  bindChipPreview(purpose, startId, qtyId, hintId, getExtra) {
    const startEl = document.getElementById(startId);
    const qtyEl = document.getElementById(qtyId);
    const hintEl = document.getElementById(hintId);
    if (!startEl || !qtyEl || !hintEl) return null;
    let timer = null;
    let seq = 0;  // 丢弃过期响应：网络慢时前一次的结果不能盖掉最新一次
    const run = async () => {
      const first = startEl.value.trim();
      const count = parseInt(qtyEl.value, 10) || 0;
      if (!first) {
        hintEl.innerHTML = '<span class="text-muted">填写起始号后，这里会预览将要生成 / 操作的号码范围</span>';
        return;
      }
      if (count < 1) {
        hintEl.innerHTML = '<span style="color:var(--rouge);font-size:13px;">数量至少为 1</span>';
        return;
      }
      const mySeq = ++seq;
      const res = await TNR_API.previewChips(Object.assign(
        { purpose: purpose, first_number: first, count: count },
        getExtra ? getExtra() : {}));
      if (mySeq !== seq) return;
      const d = (res && res.data) || {};
      if (d.ok) {
        hintEl.innerHTML = '<span style="color:var(--bamboo-green);font-size:13px;">✔ 将处理 '
          + '<b class="font-mono">' + this.escape(d.first_number) + ' ~ ' + this.escape(d.last_number)
          + '</b>，共 ' + d.count + ' 颗</span>';
      } else {
        hintEl.innerHTML = '<span style="color:var(--rouge);font-size:13px;">✘ '
          + this.escape(d.reason || '无法处理该号码范围') + '</span>';
      }
    };
    const debounced = () => { clearTimeout(timer); timer = setTimeout(run, 300); };
    startEl.addEventListener('input', debounced);
    qtyEl.addEventListener('input', debounced);
    return { rerun: run };
  },

  /* 可用芯片的连续号段摘要：点击一段 → `onPick(段首)` 由调用方填进起始号。
   * 「看得到号段」比「逼操作员翻台账抄号码」少一半录入错误。 */
  async renderChipSegments(boxId, materialId, onPick) {
    const box = document.getElementById(boxId);
    if (!box) return;
    if (!materialId) { box.innerHTML = ''; return; }
    let res;
    try {
      res = await TNR_API.getChipSegments(materialId);
    } catch (e) {
      box.innerHTML = '<div class="fs-sm" style="color:var(--rouge);">号段加载失败：'
        + this.escape(e.message || '') + '</div>';
      return;
    }
    const segs = (res && res.segments) || [];
    if (!segs.length) {
      box.innerHTML = '<div class="fs-sm text-muted">本机构该物料暂无可用芯片</div>';
      return;
    }
    box.innerHTML = '<div class="fs-sm text-muted" style="margin-bottom:var(--sp-2);">可用号段（点击填入起始号）：</div>'
      + segs.map(s => '<button type="button" class="btn btn-outline btn-sm font-mono" style="margin:0 var(--sp-2) var(--sp-2) 0;"'
        + ' data-segstart="' + this.escape(s.start) + '" data-segend="' + this.escape(s.end) + '">'
        + this.escape(s.start) + (s.start !== s.end ? ' ~ ' + this.escape(s.end) : '')
        + '<span class="text-muted">（' + s.count + '颗）</span></button>').join('');
    box.querySelectorAll('[data-segstart]').forEach(b => {
      b.addEventListener('click', () => { if (onPick) onPick(b.dataset.segstart); });
    });
  },

  /* 芯片追溯时间线抽屉。事件顺序 = 采购 → 下发 → 签收 → 植入 → 作废，
   * 由服务端 `chips/trace/` 排好序返回，这里只负责展示。 */
  async showChipTrace(number) {
    let res;
    try {
      res = await TNR_API.traceChip(number);
    } catch (e) {
      this.toast('追溯加载失败：' + (e.message || ''), 'danger');
      return;
    }
    const chip = (res && res.chip) || {};
    const events = (res && res.events) || [];
    let html = '<div style="padding:var(--sp-4);">'
      + this.renderDescList([
        { label: '芯片号', value: '<span class="font-mono">' + this.escape(chip.number || number) + '</span>' },
        { label: '当前状态', value: TNR_UI.chipStatusText(chip.status) },
        { label: '持有机构', value: this.escape(chip.institutionName || '—') },
        // 已绑定动物时编号可点：现场最常见的追问是「这颗芯片到底打到哪只
        // 猫狗身上了、之后又去过哪」，追溯抽屉只有一行编号，看不到档案。
        { label: '绑定宠物', value: chip.petCode
            ? (chip.petId
              ? '<button type="button" class="pet-code-chip" data-pet-archive="'
                + this.escape(String(chip.petId)) + '" style="cursor:pointer;'
                + 'text-decoration:underline;color:var(--cinnabar);border:0;'
                + 'background:none;padding:0;font:inherit;">'
                + this.escape(chip.petCode) + '</button>'
              : this.escape(chip.petCode))
            : '—' }
      ]) + '</div>';
    if (!events.length) {
      html += '<div class="fs-sm text-muted" style="padding:0 var(--sp-4) var(--sp-4);">暂无流转记录</div>';
    } else {
      html += '<div style="padding:0 var(--sp-4) var(--sp-4);">';
      events.forEach(ev => {
        html += '<div style="display:flex;gap:var(--sp-3);padding:var(--sp-2) 0;">'
          + '<div style="width:8px;height:8px;border-radius:50%;background:var(--bamboo-green);margin-top:7px;flex:none;"></div>'
          + '<div><div style="font-weight:600;">' + this.escape(ev.title || '')
          + ' <span class="fs-sm text-muted">' + this.escape(ev.time || '') + '</span></div>'
          + (ev.detail ? '<div class="fs-sm text-muted">' + this.escape(ev.detail) + '</div>' : '')
          + '</div></div>';
      });
      html += '</div>';
    }
    const opened = this.drawer({ title: '芯片追溯 · ' + this.escape(number), body: html });
    const petLink = opened.drawerEl.querySelector('[data-pet-archive]');
    if (petLink) {
      petLink.addEventListener('click', () => {
        // 先关追溯再开档案：drawer() 是追加式实现，叠着开会留下两层遮罩。
        opened.close();
        this.showPetArchive(petLink.dataset.petArchive);
      });
    }
  },

  /* 一宠一档档案详情：各阶段数据 + 照片墙（点击放大）。
     数据来自 /api/business/pets/<id>/lifecycle/，与政府端「一宠一档」同源。
     放在公共层是因为芯片追溯（捕捉点/医院两端）也要从这里跳进来。 */
  async showPetArchive(petId) {
    let data = null;
    try {
      data = await TNR_API.getPetLifecycleStrict(petId);
    } catch (e) {
      this.toast('档案加载失败：' + (e.message || '未知错误'), 'danger');
      return;
    }
    const pet = (data && (data.pet_brief || data.pet)) || {};
    const photos = (data && data.photos) || [];
    const events = (data && data.events) || [];

    const items = [
      { label: '一宠一档', value: '<span class="pet-code-chip">' + this.escape(pet.code || '—') + '</span>' },
      { label: '昵称', value: this.escape(pet.name || '—') },
      { label: '类别', value: this.escape(pet.species || '—') },
      { label: '品种', value: this.escape(pet.breed || '—') },
      { label: '性别', value: this.escape(pet.gender || '—') },
      { label: '年龄', value: this.escape(pet.age || '—') },
      { label: '毛色', value: this.escape(pet.color || '—') },
      { label: '芯片号', value: this.escape(pet.chip_no || '—') },
      { label: '当前状态', value: TNR_UI.petStageBadge(pet.stage || pet.status, pet.stage_display) },
      { label: '所属区县', value: this.escape(pet.district_name || '—') },
      { label: '捕捉点', value: this.escape(pet.shelter_name || '—') },
      { label: '所在医院', value: this.escape(pet.hospital_name || '—') }
    ];

    let html = this.renderDescList(items);

    // 照片墙：捕捉 / 整体合影 / 术前 / 术后 / 诊疗
    html += '<div class="desc-item desc-full"><div class="desc-label">动物照片（点击可放大）</div><div class="desc-value">';
    html += photos.length
      ? '<div class="photo-grid" style="grid-template-columns:repeat(auto-fill,minmax(120px,1fr));">'
        + photos.map(p => '<div class="photo-item"><img src="' + p.url + '" alt="' + this.escape(p.label) + '" data-zoom="' + p.url + '"><div class="photo-item-label">' + this.escape(p.label) + '</div></div>').join('')
        + '</div>'
      : '<span class="text-light fs-sm">暂无照片</span>';
    html += '</div></div>';

    // 流转全过程时间线（捕捉 → 转运 → 诊疗 → 回收/放归/领养/死亡）
    html += '<div class="desc-item desc-full"><div class="desc-label">流转全过程（' + events.length + ' 条）</div><div class="desc-value">';
    if (!events.length) {
      html += '<span class="text-light fs-sm">暂无流转记录</span>';
    } else {
      html += '<div class="timeline">' + events.map(ev => {
        const dateStr = ev.date ? TNR_UI.formatDate(ev.date) : '—';
        let desc = '';
        if (ev.type === 'capture') {
          desc = '捕捉点：' + this.escape(ev.shelter_name || '—')
            + '<br>小区：' + this.escape(ev.community_name || '—')
            + '<br>地址：' + this.escape(ev.address || '—')
            + '<br>物业交接人：' + this.escape(ev.contact_person || '—') + (ev.contact_phone ? ' / ' + this.escape(ev.contact_phone) : '')
            + '<br>捕捉单号：' + this.escape(ev.ledger_no || '—');
        } else if (ev.type === 'transfer') {
          desc = '发出：' + this.escape(ev.from_shelter_name || '—')
            + '<br>接收：' + this.escape(ev.to_hospital_name || '—')
            + '<br>状态：' + this.escape(TNR_UI.transferStatusText(ev.status))
            + (ev.received_at ? '<br>签收日期：' + this.escape(ev.received_at) : '');
        } else if (ev.type === 'treatment') {
          desc = '医院：' + this.escape(ev.hospital_name || '—')
            + '<br>项目：' + this.escape((ev.items || []).join('、') || '—')
            + '<br>状态：' + this.escape(TNR_UI.treatmentStatusText(ev.status))
            + (ev.chip_no ? '<br>芯片号：' + this.escape(ev.chip_no) : '');
        } else if (ev.type === 'release') {
          desc = '放归小区：' + this.escape(ev.community_name || '—')
            + '<br>接收人：' + this.escape(ev.receiver_name || '—')
            + '<br>状态：' + this.escape(TNR_UI.releaseStatusText(ev.status));
        } else if (ev.type === 'adoption') {
          desc = '领养人：' + this.escape(ev.adopter_name || '—') + '<br>医院：' + this.escape(ev.hospital_name || '—');
        } else if (ev.type === 'euthanasia') {
          desc = '医院：' + this.escape(ev.hospital_name || '—') + '<br>原因：' + this.escape(ev.reason || '—');
        } else if (ev.type === 'owner_return') {
          // 回收：领回人完整信息 + 原因 + 操作人 + 签名照片
          desc = '领回单号：' + this.escape(ev.ledger_no || '—')
            + '<br>领回人：' + this.escape(ev.owner_name || '—')
            + (ev.owner_phone ? '<br>联系电话：' + this.escape(ev.owner_phone) : '')
            + (ev.owner_id_card ? '<br>身份证：' + this.escape(ev.owner_id_card) : '')
            + (ev.owner_address ? '<br>住址：' + this.escape(ev.owner_address) : '')
            + '<br>原因：' + this.escape(ev.reason || '—')
            + '<br>操作人：' + this.escape(ev.operator_name || '—');
          // 签名是 base64 data URL（png），直接当图片渲染
          if (ev.signature && ev.signature.startsWith('data:image')) {
            desc += '<div style="margin-top:8px;"><div style="font-size:12px;color:var(--ink-muted);margin-bottom:4px;">领回人电子签名</div>'
              + '<img src="' + ev.signature + '" alt="签名" data-zoom="' + ev.signature + '" '
              + 'style="max-width:260px;max-height:120px;background:#fff;border:1px solid var(--border-light);border-radius:6px;cursor:zoom-in;padding:4px;"></div>';
          }
        }
        const evPhotos = ev.photos || [];
        if (evPhotos.length) {
          desc += '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;">'
            + evPhotos.map(p => '<img src="' + p.url + '" alt="' + this.escape(p.label) + '" data-zoom="' + p.url + '" style="width:72px;height:72px;object-fit:cover;border-radius:6px;border:1px solid var(--border-light);cursor:zoom-in;">').join('')
            + '</div>';
        }
        return '<div class="timeline-item done">'
          + '<div class="timeline-time">' + this.escape(dateStr) + '</div>'
          + '<div class="timeline-title">' + this.escape(ev.type_display || ev.type) + '</div>'
          + '<div class="timeline-desc">' + desc + '</div>'
          + '</div>';
      }).join('') + '</div>';
    }
    html += '</div></div>';

    this.drawer({ title: '一宠一档档案 · ' + this.escape(pet.code || ''), body: html });
    setTimeout(() => this.bindPhotoZoom(document), 50);
  },

  // === 物料批次配套组件（B5：捕捉点 / 医院两端共用） ===
  // 与芯片组件同一原则：文案与判据只有这一份实现，两端不各写一遍。

  /* 批次明细表 HTML（FEFO 顺序，过期标红/临期标橙）。表格行内展开与
   * 医院端卡片展开共用同一份渲染 —— 两处各写一遍，红/橙/绿的口径
   * 迟早一边改了一边没改。 */
  batchDetailHtml(batches) {
    return '<table class="data-table" style="margin:0;"><thead><tr>'
      + '<th>批号</th><th>有效期至</th><th>余量</th><th>状态</th></tr></thead><tbody>'
      + batches.map(b => {
        const expiry = b.expiry_date || '无效期';
        const badge = b.expired
          ? '<span style="color:var(--rouge);font-weight:600;">已过期</span>'
          : (b.near_expiry
            ? '<span style="color:#c07800;font-weight:600;">临期</span>'
            : '<span style="color:var(--bamboo-green);">正常</span>');
        const expiryStyle = b.expired ? 'color:var(--rouge);font-weight:600;text-decoration:line-through;'
          : (b.near_expiry ? 'color:#c07800;font-weight:600;' : '');
        return '<tr><td class="font-mono">' + this.escape(b.batch_no) + '</td>'
          + '<td style="' + expiryStyle + '">' + this.escape(expiry) + '</td>'
          + '<td>' + b.quantity + '</td><td>' + badge + '</td></tr>';
      }).join('')
      + '</tbody></table>';
  },

  /* 医院端「实时库存」卡片的批次展开（卡片不是表格行，不适配
   * bindBatchExpand 的 tr 结构）：按钮点击 → fetch 一次 → toggle 卡片内
   * 的批次区。`boxId` 是卡片内预留的容器，按钮与容器由调用方渲染。 */
  async toggleCardBatches(materialId, boxEl) {
    if (!boxEl) return;
    if (boxEl.dataset.loaded) {
      boxEl.style.display = boxEl.style.display === 'none' ? '' : 'none';
      return;
    }
    boxEl.innerHTML = '<span class="fs-sm text-muted">批次加载中…</span>';
    boxEl.style.display = '';
    let res;
    try {
      res = await TNR_API.getMaterialBatches(materialId);
    } catch (err) {
      boxEl.innerHTML = '<span class="fs-sm" style="color:var(--rouge);">'
        + '批次加载失败：' + this.escape(err.message || '') + '</span>';
      return;
    }
    boxEl.dataset.loaded = '1';
    const batches = (res && res.batches) || [];
    if (!batches.length) {
      boxEl.innerHTML = '<span class="fs-sm text-muted">本机构暂无批次台账</span>';
      return;
    }
    boxEl.innerHTML = '<div class="fs-sm" style="font-weight:600;margin:var(--sp-2) 0;">批次明细'
      + '<span class="text-muted" style="font-weight:400;">（近效期优先扣减）</span></div>'
      + this.batchDetailHtml(batches);
  },

  /* 物料列表的「批次」行内展开/收起。按钮带 `data-material-id` /
   * `data-material-name`，点击后在其所在行下方插入一条批次明细子行；
   * 再次点击收起。政府端物料列表没有本机构概念，不适用（台账另有展示）。
   *
   * 为什么用事件委托签名 `bindBatchExpand(container)`：两端列表都是
   * JS 重渲染的，逐个按钮绑事件会在重渲染后失效；委托绑在容器上一次即可。 */
  bindBatchExpand(container) {
    const box = typeof container === 'string'
      ? document.getElementById(container) : container;
    if (!box || box._batchExpandBound) return;
    box._batchExpandBound = true;
    box.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-batch-toggle]');
      if (!btn) return;
      const tr = btn.closest('tr');
      if (!tr) return;
      const existing = tr.nextElementSibling;
      if (existing && existing.classList.contains('batch-detail-row')) {
        // 已展开过：只做显隐切换（数据不重取 —— 批次在当前页停留期间
        // 变化的窗口很小，重取反而让「点一下闪一下」）。
        existing.style.display = existing.style.display === 'none' ? '' : 'none';
        return;
      }
      const materialId = btn.dataset.materialId;
      const detailRow = document.createElement('tr');
      detailRow.className = 'batch-detail-row';
      detailRow.innerHTML = '<td colspan="99"><span class="fs-sm text-muted">批次加载中…</span></td>';
      tr.after(detailRow);
      let res;
      try {
        res = await TNR_API.getMaterialBatches(materialId);
      } catch (err) {
        detailRow.innerHTML = '<td colspan="99"><span class="fs-sm" style="color:var(--rouge);">'
          + '批次加载失败：' + this.escape(err.message || '') + '</span></td>';
        return;
      }
      const batches = (res && res.batches) || [];
      if (!batches.length) {
        detailRow.innerHTML = '<td colspan="99"><span class="fs-sm text-muted">'
          + '本机构暂无批次台账（该物料还没有入库 / 签收记录）</span></td>';
        return;
      }
      detailRow.innerHTML = '<td colspan="99" style="background:var(--bg-secondary,#f8f7f4);padding:var(--sp-3);">'
        + '<div class="fs-sm" style="font-weight:600;margin-bottom:var(--sp-2);">批次明细'
        + '<span class="text-muted" style="font-weight:400;">（按近效期优先排序，扣减时先扣前排）</span></div>'
        + this.batchDetailHtml(batches) + '</td>';
    });
  },

  /* 消耗 / 下发 / 异动表单的「将扣批次预览」（C1 芯片预检同款）。
   * `getMaterialId()` 动态取当前选中的物料，`txnType` 固定传给服务端 ——
   * consume 跳过过期批次、dispatch/adjustment 不跳，预览与落库同判据。 */
  bindBatchPreview(qtyId, hintId, getMaterialId, txnType, getBatchNo) {
    const qtyEl = document.getElementById(qtyId);
    const hintEl = document.getElementById(hintId);
    if (!qtyEl || !hintEl) return null;
    let timer = null;
    let seq = 0;  // 丢弃过期响应：物料切换后上一次的结果不能盖掉最新一次
    const run = async () => {
      const materialId = getMaterialId ? getMaterialId() : null;
      const qty = parseInt(qtyEl.value, 10) || 0;
      if (!materialId) {
        hintEl.innerHTML = '<span class="fs-sm text-muted">选择物料后，这里会预览将要扣减的批次</span>';
        return;
      }
      // ⚠ 出库的**负数量**是盘盈 / 退回入库，方向整个是反的 ——
      // 它不会扣任何批次，反而会往批号里加。这里必须**明确说出来**，
      // 不能让它掉进下面 qty<1 的「填写数量后…」分支：用户填了 -3，
      // 界面却像没填，只会让人以为预览坏了。
      if (qty < 0) {
        hintEl.innerHTML = '<span class="fs-sm" style="color:var(--bamboo-green);">↺ 负数量 = 盘盈 / 退回入库，将<b>增加</b>库存 '
          + Math.abs(qty) + '，不扣减任何批次</span>';
        return;
      }
      if (qty < 1) {
        hintEl.innerHTML = '<span class="fs-sm text-muted">填写数量后，这里会预览将要扣减的批次</span>';
        return;
      }
      const mySeq = ++seq;
      const batchNo = getBatchNo ? (getBatchNo() || '') : '';
      let res;
      try {
        res = await TNR_API.previewMaterialBatches(materialId, {
          quantity: qty, txn_type: txnType,
          ...(batchNo ? { batch_no: batchNo } : {}),
        });
      } catch (e) {
        if (mySeq !== seq) return;
        hintEl.innerHTML = '<span class="fs-sm text-muted">批次预览不可用</span>';
        return;
      }
      if (mySeq !== seq) return;
      const d = (res && res.data) || {};
      if (!d.has_batches) {
        hintEl.innerHTML = '<span class="fs-sm text-muted">该物料暂无批次台账'
          + (typeof d.available === 'number' ? '（当前库存 ' + d.available + '）' : '')
          + '，扣减不区分批次</span>';
        return;
      }
      if (!d.enough) {
        hintEl.innerHTML = '<span class="fs-sm" style="color:var(--rouge);">✘ '
          + this.escape(d.message || '批次库存不足') + '</span>';
        return;
      }
      const skipped = d.expired_skipped > 0
        ? '<span style="color:#c07800;">（另有 ' + d.expired_skipped + ' 已过期批次被跳过）</span>' : '';
      hintEl.innerHTML = '<span class="fs-sm" style="color:var(--bamboo-green);">✔ 将扣：</span>'
        + '<span class="fs-sm">' + this.escape(d.message) + skipped + '</span>';
    };
    const debounced = () => { clearTimeout(timer); timer = setTimeout(run, 300); };
    qtyEl.addEventListener('input', debounced);
    return { rerun: run };
  },

  // === 渲染表格（旧接口，保持兼容；标准列表请用 mountTable） ===
  renderTable({ columns, data, emptyText = '暂无数据', rowActions = null }) {
    if (!data || data.length === 0) {
      return `<div class="table-empty"><div class="table-empty-icon">📋</div><div class="table-empty-text">${emptyText}</div></div>`;
    }
    let html = '<table class="data-table"><thead><tr>';
    columns.forEach(col => {
      html += `<th style="${col.width ? 'width:' + col.width + ';' : ''}">${col.title}</th>`;
    });
    if (rowActions) html += '<th style="width:1px;">操作</th>';
    html += '</tr></thead><tbody>';
    data.forEach((row, idx) => {
      html += '<tr>';
      columns.forEach(col => {
        const val = typeof col.render === 'function' ? col.render(row, idx) : (row[col.key] ?? '');
        html += `<td>${val ?? ''}</td>`;
      });
      if (rowActions) {
        html += `<td><div class="flex gap-2">${rowActions(row, idx)}</div></td>`;
      }
      html += '</tr>';
    });
    html += '</tbody></table>';
    return html;
  },

  // ============================================
  // 标准数据表格（前端分页 / 点击排序 / 列宽拖拽记忆）
  // ============================================
  _tableState: {},   // 运行态：page / sortKey / sortDir（filter 重渲染时保留排序）
  _dtDrag: null,     // 列宽拖拽中的临时状态
  _delegatedTags: new WeakMap(),   // 容器 -> Set(委托标识)，防重复绑定

  /* 统一解析「容器」参数：支持 id 字符串 / CSS 选择器 / Element */
  _resolveEl(target) {
    if (!target) return null;
    if (typeof target !== 'string') return target;
    const isSelector = target.startsWith('#') || target.startsWith('.')
      || target.startsWith('[') || target.includes(' ');
    return isSelector ? document.querySelector(target) : document.getElementById(target);
  },

  /* 筛选控件的默认作用域。
     各 portal 的 .page-view 全部常驻 DOM（切页只切 .active），所以**不传 scope 时
     不能退回 document** —— 那会把所有页面的 [data-filter] 混在一起读，
     典型故障是「A 页输入的关键词把 B 页的表格也过滤掉了」，且不报错、很难查。
     这里优先锁到当前激活页；只有当激活页内一个筛选控件都没有时（例如筛选条被放在
     页壳层顶部而非 page-view 内）才退回 document，以兼容既有布局。 */
  _filterScope(scope) {
    const explicit = this._resolveEl(scope);
    if (explicit) return explicit;
    const active = document.querySelector('.page-view.active');
    if (active && active.querySelector('[data-filter]')) return active;
    return document;
  },

  _pref(key, value) {
    // localStorage 读写（JSON），失败静默降级为无记忆
    try {
      const fullKey = 'tnr.ui.' + key;
      if (value === undefined) {
        const raw = localStorage.getItem(fullKey);
        return raw === null ? null : JSON.parse(raw);
      }
      localStorage.setItem(fullKey, JSON.stringify(value));
    } catch (e) { /* 隐私模式等场景降级 */ }
    return value === undefined ? null : value;
  },

  _sortValue(row, key) {
    const v = row[key];
    if (v === null || v === undefined || v === '') return null;
    if (typeof v === 'number') return v;
    const s = String(v).trim();
    if (s !== '' && !isNaN(Number(s)) && /^-?\d+(\.\d+)?$/.test(s)) return Number(s);
    if (/^\d{4}-\d{2}-\d{2}/.test(s)) return Date.parse(s) || s;
    return s;
  },

  /**
   * 标准表格挂载：客户端分页（默认 10 条/页）+ 点击列头排序 + 列宽拖拽（localStorage 记忆）。
   *
   * **列表上方不再有汇总标题**：早期这里会渲染「捕捉记录（12）」这类带数量的标题头，
   * 位于表头正上方。它既与页面/外层卡片的分区标题重复，又与「表头冻结」争抢 sticky 层级，
   * 现已整块移除。需要分区标题时由调用方自备外层 `card-header`（不带数量）。
   *
   * @param {string|Element} target 容器元素或 id
   * @param {object} opts { id, columns:[{key,title,render,width,sortable,align}], data, rowActions,
   *                        emptyText, pageSize, pageSizes, wrap:'card'|'plain' }
   */
  mountTable(target, opts) {
    const el = this._resolveEl(target);
    if (!el) return;
    // 外层容器若带 table-wrapper（overflow-x:auto）会剪裁 sticky 表头，剥离之
    if (el.classList && el.classList.contains('table-wrapper')) el.classList.remove('table-wrapper');
    const id = opts.id || el.id || 'table';
    const st = this._tableState[id] = this._tableState[id] || { page: 1 };
    const prefs = this._pref('tbl.' + id) || {};
    const pageSizes = opts.pageSizes || [10, 20, 50];
    const pageSize = st.pageSize || prefs.pageSize || opts.pageSize || 10;
    const data = Array.isArray(opts.data) ? opts.data.slice() : [];

    // ---- 排序（外部重渲染保留排序条件；filter 变化时由调用方决定是否重置） ----
    let rows = data;
    if (st.sortKey) {
      const dir = st.sortDir === 'desc' ? -1 : 1;
      rows = rows.slice().sort((a, b) => {
        const va = this._sortValue(a, st.sortKey), vb = this._sortValue(b, st.sortKey);
        if (va === null && vb === null) return 0;
        if (va === null) return 1;
        if (vb === null) return -1;
        if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
        return String(va).localeCompare(String(vb), 'zh-Hans-CN') * dir;
      });
    }

    // ---- 分页 ----
    const total = rows.length;
    // 数据总量变化（筛选/刷新）时回到第 1 页；排序/翻页等内部操作不受影响
    if (st.lastTotal !== undefined && st.lastTotal !== total) st.page = 1;
    st.lastTotal = total;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (st.page > totalPages) st.page = totalPages;
    const pageRows = rows.slice((st.page - 1) * pageSize, st.page * pageSize);

    // ---- 列宽记忆 ----
    const widths = prefs.widths || null;

    const esc = this.escape.bind(this);
    const emptyText = opts.emptyText || '暂无数据';
    const actionsCol = !!opts.rowActions;

    let html = '';
    if (opts.wrap !== 'plain') {
      // 列表卡**不再渲染标题头**。此前这里是：
      //   `<div class="card-header">…${title}${total ? `（${total}）` : ''}…</div>`
      // 即「捕捉记录（12）」这类带数量的汇总文字，正好压在表头上方 —— 需求要求列表
      // 上方不出现汇总数据，且该 header 也占用了 sticky 层级（top:100px），与表头冻结冲突。
      html += '<div class="card dt-card"><div class="card-body" style="padding:0;"><div class="table-wrapper">';
    }

    if (total === 0) {
      html += `<div class="table-empty"><div class="table-empty-icon">📋</div><div class="table-empty-text">${esc(emptyText)}</div></div>`;
    } else {
      const fixed = widths ? ' table-layout:fixed;' : '';
      html += `<table class="data-table dt-table" style="${fixed}">`;
      html += '<colgroup>';
      opts.columns.forEach((col, i) => {
        const w = widths ? widths[i] : (col.width || null);
        // 移动端列压缩：`mobile:'hide'` 的列在 ≤768px 隐藏（见 CSS），
        // 其内容应通过主列的 `mobileRender` 带回，否则手机上信息直接消失。
        const hide = col.mobile === 'hide' ? ' class="dt-col-mobile-hide"' : '';
        html += `<col${hide} style="${w ? 'width:' + (typeof w === 'number' ? w + 'px' : w) + ';' : ''}">`;
      });
      if (actionsCol) html += `<col style="${widths && widths[opts.columns.length] ? 'width:' + widths[opts.columns.length] + 'px' : 'width:1px;'}">`;
      html += '</colgroup><thead><tr>';
      opts.columns.forEach((col, i) => {
        const sortable = col.sortable !== false && col.key;
        const sorted = st.sortKey === col.key;
        const arrow = sortable ? `<span class="dt-sort ${sorted ? 'on ' + (st.sortDir === 'desc' ? 'desc' : 'asc') : ''}">${sorted ? (st.sortDir === 'desc' ? '▼' : '▲') : '⇅'}</span>` : '';
        const align = col.align ? `text-align:${col.align};` : '';
        const hide = col.mobile === 'hide' ? ' dt-col-mobile-hide' : '';
        html += `<th class="${sortable ? 'dt-sortable' : ''}${hide}" data-dt-sort="${sortable ? esc(col.key) : ''}" style="${align}">${col.title}${arrow}`;
        if (col.resizable !== false) html += `<span class="dt-colresizer" data-dt-col="${i}"></span>`;
        html += '</th>';
      });
      if (actionsCol) html += `<th class="dt-actions-th">操作<span class="dt-colresizer" data-dt-col="${opts.columns.length}"></span></th>`;
      html += '</tr></thead><tbody>';
      pageRows.forEach((row, idx) => {
        html += '<tr>';
        opts.columns.forEach(col => {
          let val = typeof col.render === 'function' ? col.render(row, idx) : (row[col.key] ?? '');
          // 仅手机可见的补充内容，挂在主列单元格里；桌面端由 CSS 隐藏，
          // 不改变原有列宽与信息密度。
          if (typeof col.mobileRender === 'function') {
            val += `<div class="dt-mobile-only">${col.mobileRender(row, idx)}</div>`;
          }
          const hide = col.mobile === 'hide' ? ' class="dt-col-mobile-hide"' : '';
          html += `<td${hide}>${val ?? ''}</td>`;
        });
        if (actionsCol) html += `<td class="dt-actions-td"><div class="flex gap-2">${opts.rowActions(row, idx)}</div></td>`;
        html += '</tr>';
      });
      html += '</tbody></table>';
    }

    if (opts.wrap !== 'plain') html += '</div>';

    // ---- 分页条 ----
    if (total > 0) {
      html += '<div class="pagination dt-pagination">';
      html += `<span class="pagination-info">共 ${total} 条</span>`;
      html += `<select class="form-select dt-pagesize" data-dt-size="${esc(id)}" style="width:auto;padding:2px 6px;font-size:12px;">`;
      pageSizes.forEach(s => {
        html += `<option value="${s}" ${s === pageSize ? 'selected' : ''}>${s} 条/页</option>`;
      });
      html += '</select>';
      if (totalPages > 1) {
        html += `<button class="pagination-btn" ${st.page <= 1 ? 'disabled' : ''} data-dt-page="${st.page - 1}" data-dt-id="${esc(id)}">‹</button>`;
        for (let i = 1; i <= totalPages; i++) {
          if (i === 1 || i === totalPages || (i >= st.page - 1 && i <= st.page + 1)) {
            html += `<button class="pagination-btn ${i === st.page ? 'active' : ''}" data-dt-page="${i}" data-dt-id="${esc(id)}">${i}</button>`;
          } else if (i === st.page - 2 || i === st.page + 2) {
            html += '<button class="pagination-btn" disabled>…</button>';
          }
        }
        html += `<button class="pagination-btn" ${st.page >= totalPages ? 'disabled' : ''} data-dt-page="${st.page + 1}" data-dt-id="${esc(id)}">›</button>`;
      }
      html += '</div>';
    }
    if (opts.wrap !== 'plain') html += '</div>';

    el.innerHTML = html;

    // 让「列表内容区」成为唯一纵向滚动区（筛选条 / 表头固定）——必须在入 DOM 之后，
    // 否则量不到真实位置。窗口尺寸变化时统一重算（见 _bindTableFit）。
    // 去重：mountTable 在排序/翻页时会重入，同一个容器不能反复入列。
    if (!this._dtHosts.includes(el)) this._dtHosts.push(el);
    this._bindTableFit();
    this._fitTableHeight(el);

    // ---- 事件绑定 ----
    // 排序
    el.querySelectorAll('.dt-sortable').forEach(th => {
      th.addEventListener('click', (e) => {
        if (e.target.classList.contains('dt-colresizer')) return;
        const key = th.dataset.dtSort;
        if (!key) return;
        if (st.sortKey === key) st.sortDir = st.sortDir === 'asc' ? 'desc' : 'asc';
        else { st.sortKey = key; st.sortDir = 'asc'; }
        st.page = 1;
        this.mountTable(el, opts);   // 重渲染（保留其他状态）
      });
    });
    // 翻页 / 每页条数
    el.querySelectorAll('[data-dt-page]').forEach(btn => {
      btn.addEventListener('click', () => {
        const tid = btn.dataset.dtId;
        const s = this._tableState[tid];
        if (s) { s.page = parseInt(btn.dataset.dtPage, 10) || 1; this.mountTable(el, opts); }
      });
    });
    const sizeSel = el.querySelector('.dt-pagesize');
    if (sizeSel) {
      sizeSel.addEventListener('change', () => {
        st.pageSize = parseInt(sizeSel.value, 10);
        st.page = 1;
        this._pref('tbl.' + id, Object.assign({}, prefs, { pageSize: st.pageSize, widths: prefs.widths }));
        this.mountTable(el, opts);
      });
    }
    // 列宽拖拽
    el.querySelectorAll('.dt-colresizer').forEach(handle => {
      handle.addEventListener('mousedown', (e) => this._startColResize(e, handle, el, opts, id, prefs));
      handle.addEventListener('touchstart', (e) => this._startColResize(e, handle, el, opts, id, prefs), { passive: true });
    });
  },

  /* ============================================
     列表冻结：筛选条 + 表头固定，只有行区域滚动
     ============================================ */
  _dtHosts: [],        // 已挂载的列表容器（窗口尺寸变化时统一重算高度）
  _dtFitBound: false,  // resize 监听只绑一次

  /* 为什么需要给 `.table-wrapper` 一个 max-height：
   *
   * `thead th` 上本来就写了 `position: sticky`，但**一直没生效**，两个原因叠加：
   *   1. 同文件后段 `.data-table thead th { position: relative; }` 覆盖了它
   *      （为列宽拖拽手柄 `.dt-colresizer` 提供定位祖先而加，同选择器、后者胜）；
   *   2. 外层 `.table-wrapper` 带 `overflow-x: auto` —— 只要一个轴不是 visible，
   *      另一个轴的 visible 就会被计算成 auto，于是 wrapper 成了滚动容器。
   *      它由内容撑开、永远不滚，sticky 表头便彻底失效。
   *
   * 表现就是用户看到的那样：滚轮下滑时筛选条钉住了，表头却跟着内容上移，
   * 最后钻到筛选条底下被遮住。
   *
   * 修法：保留 wrapper 的横向滚动能力（宽表仍要能左右拖），给它一个**按视口算出来的
   * max-height**，让它真的成为纵向滚动容器，表头 sticky 随即生效。max-height 只封顶
   * 不拉伸，所以短列表的观感与改动前完全一致。
   */
  _fitTableHeight(el) {
    if (!el || !el.querySelector) return;
    const wrap = el.querySelector('.table-wrapper');
    if (!wrap) return;                                   // wrap:'plain' 无卡片，不参与
    const scroller = el.closest('.portal-content') || document.scrollingElement;
    if (!scroller) return;

    const viewportH = scroller.clientHeight || window.innerHeight;
    const pager = el.querySelector('.dt-pagination');
    const pagerH = pager ? pager.offsetHeight : 0;
    const card = el.querySelector('.dt-card') || el;

    // 页面本身不滚动 → 不存在「表头被筛选条压住」的场景，保持列表自然高度，
    // 不要为了凑公式把它压小。
    //
    // 判定必须用「**解除限制后**会不会溢出」，不能直接看当前 scrollHeight：
    // 一旦限了高，scrollHeight 就变小了，下次再判会得出「不溢出」→ 清掉限制 →
    // 又溢出 → 再限高…… 来回振荡。这里用「wrapper 内容高 − 当前高」把
    // 限高造成的差值补回去，判定就与当前是否限高无关，稳定收敛。
    const unclampDelta = Math.max(0, wrap.scrollHeight - wrap.clientHeight);
    if (scroller.scrollHeight + unclampDelta <= scroller.clientHeight + 1) {
      // 页面本身不滚动 → 不存在「表头被筛选条压住」的场景，保持列表自然高度。
      // 但要把上一步可能设过的 overflow 还原：clip 会把宽表裁掉。
      wrap.style.maxHeight = '';
      wrap.style.overflow = 'auto';
      this._setHeadTop(wrap, 0);
      return;
    }

    const GAP = 16;        // 卡片下沿与视口底部的呼吸位
    // 下限 = 表头 + **固定 10 行**（磊哥的要求：「列表高度按默认 10 行设置」）。
    // ⚠ 这 10 行**不随「条/页」控件变化**。曾误写成「取当前每页条数」，后果很隐蔽：
    // 一页渲染的行数 = 每页条数 ⇒ 「自然高」恒等于「一页的行高」，于是 MIN ≈ natural，
    // 下面的容差判断永远把它判成「内容放得下」→ **下限彻底失效**（实测 20 条/页 时
    // 列表长到 1017px，比视口还高）。更糟的是行数 > 50 的表在 50 条/页 下，
    // 下限会变成 50 行（约 2500px），列表被顶出屏幕。
    // 「每页条数」只决定一页渲染多少行，**不决定列表该有多高**。
    //
    // ⚠ 行高**不能**用「首行高 × N」估算：长文本会折行，同一张表里行高并不均匀。
    // 实测「全量台账中心 / 捕捉台账」首行 69.78px、第 5/7/10 行 76.19px，按首行估出
    // 「10 行刚好 742px」，而实际需要 767px → 第 10 行被裁掉、只完整显示 9 行，
    // 还凭空多出一条 25px 的内部滚动条（且完全不报错）。
    // 改成**直接量第 N 行的下沿**，拿到的就是前 N 行的真实累计高度，与行高是否整齐无关。
    const FLOOR_ROWS = 10;
    const tbody = el.querySelector('.dt-table tbody');
    const trows = tbody ? Array.from(tbody.querySelectorAll('tr')) : [];
    const thH = (el.querySelector('.dt-table thead') || {}).offsetHeight || 44;
    let rowsH = 0;
    if (trows.length) {
      const last = trows[Math.min(trows.length, FLOOR_ROWS) - 1];
      rowsH = last.getBoundingClientRect().bottom - tbody.getBoundingClientRect().top;
    }
    if (!(rowsH > 0)) {
      // 空态（无数据行）时退回估算，保证下限仍然成立
      const rowH = (el.querySelector('.dt-table tbody tr') || {}).offsetHeight || 44;
      rowsH = rowH * FLOOR_ROWS;
    }
    // max-height 作用在**边框盒**上（实测 maxHeight=744 → clientHeight=742），
    // 所以要把 wrapper 自身的上下边框与内边距补进来，否则内容区正好差这几像素。
    const wcs = getComputedStyle(wrap);
    const boxY = (parseFloat(wcs.borderTopWidth) || 0) + (parseFloat(wcs.borderBottomWidth) || 0)
      + (parseFloat(wcs.paddingTop) || 0) + (parseFloat(wcs.paddingBottom) || 0);
    const MIN = Math.ceil(thH + rowsH + boxY);

    // 卡片下方的留白 / 内容（容器 padding、页面里排在列表之后的其它卡片）。
    // 页面滚到底时，这些部分会占据视口底部，必须一并扣掉。
    // 注意与 max-height 无循环依赖：改 max-height 时 scrollHeight 与 cardBottom
    // 同步增减，差值（本值）恒定。
    const sRect = scroller.getBoundingClientRect();
    const cardBottom = card.getBoundingClientRect().bottom - sRect.top + (scroller.scrollTop || 0);
    const tail = Math.max(0, scroller.scrollHeight - cardBottom);

    // 固定头部（标签页 top:0 / 筛选条 top:46）永久占据视口顶部，同样扣掉。
    // 于是：页面滚到底时列表恰好铺满剩余视口 —— 表头正好停在筛选条下沿，
    // 既不会被遮住，分页条也不会被顶出屏幕。
    const reserved = this._pinnedHeight(scroller);
    const target = viewportH - reserved - pagerH - tail - GAP;
    // 下限生效（10 行）时，短列表自然高度更小，max-height 只封顶不拉伸，观感不变。
    // 容差：cap 与内容自然高只差 1~2px 时直接取自然高 —— 那是行高的取整误差，
    // 硬限会凭空给列表多出一条 1px 的滚动条（实测「捕捉台账」踩到过）。
    // 与 cap 同单位：max-height 作用在**边框盒**上，而 scrollHeight 只含「内边距 + 内容」。
    // 两者差一个上下边框（实测 2px）。单位不一致会让「cap 与自然高只差 1~2px」的容差判断
    // 失真 —— 该走「内容放得下」分支的表格会被判成放不下，从而被限高、裁掉最后一行。
    const natural = wrap.scrollHeight + boxY;
    let cap = Math.max(MIN, Math.round(target));
    if (cap >= natural - 2) cap = natural;

    if (cap >= natural) {
      // 内容放得下 → 列表内部不需要滚动。
      // 但页面可能因为「列表下方还排着别的卡片」而滚动；这时若 wrapper 仍是滚动容器，
      // 表头会被关在它里面（它自己不滚）→ 页面一滚，表头就被筛选条压住。
      // 所以这里放开 wrapper 的**纵向** overflow，让最近的滚动容器变成 .portal-content，
      // 表头便能钉在**页面级** —— 滚页面时停在筛选条下沿，而不是被它盖住。
      // 横向：确实要左右滚的宽表保留 auto（此时页面级 sticky 失效，退回原行为）；
      // 不需要的用 clip —— clip 不产生滚动容器，也不会把内容溢出到卡片外。
      const needX = wrap.scrollWidth > wrap.clientWidth + 4;
      wrap.style.maxHeight = '';
      if (needX) {
        wrap.style.overflow = 'auto';
        this._setHeadTop(wrap, 0);
      } else {
        wrap.style.overflowX = 'clip';
        wrap.style.overflowY = 'visible';
        this._setHeadTop(wrap, reserved);
      }
    } else {
      // 内容超出 → 让 wrapper 成为真正的纵向滚动容器，表头钉在它自己的顶部
      wrap.style.overflow = 'auto';
      wrap.style.maxHeight = cap + 'px';
      this._setHeadTop(wrap, 0);
    }
  },

  /* 设置该列表所有表头的 sticky top。
     0 = 钉在列表自身滚动区顶部（列表内部滚动时用）；
     >0 = 钉在页面级，值即筛选条/标签栏钉住后的底边（页面滚动时用）。 */
  _setHeadTop(wrap, px) {
    const v = px > 0 ? Math.round(px) + 'px' : '0px';
    wrap.querySelectorAll('thead th').forEach((th) => {
      if (th.style.top !== v) th.style.top = v;
    });
  },

  /* 统计页面里「钉住的头部」占用的高度：取所有 sticky 头部（标签栏 top:0、
     筛选条 top:46px、标准页头 top:0）中 `top + offsetHeight` 的**最大值**，而非累加 ——
     它们是叠着钉的，最大下沿才是真正被占掉的高度。

     ⚠ `.std-page-head` 必须一起算：政府端「数据总览大屏」把整行页头做成 sticky
     （里面的 `.filter-bar` 反而被置为 static，因为 flex 行只比它高 10px、钉不住），
     漏掉它就会把表头的 top 算成 0 —— 表头被页头**压住**，看起来又是「冻结的标题漂移」。

     注意**不要再回到「沿 wrapper 的 previousElementSibling 逐层找」的写法**：
     筛选条与列表并不总在同一层兄弟位置，那种写法会漏算 —— 实测「动物去向/回收」
     的第一个列表只算到标签栏 46px，于是表头被设成 top:46px，被 145px 高的筛选条
     压住（该页筛选条有 7 项、会折行）。
     高度为 0 的节点（隐藏的其它 .page-view）直接跳过。 */
  _pinnedHeight(root) {
    let maxEdge = 0;
    root.querySelectorAll('.tabs, .filter-bar, .std-page-head').forEach((el) => {
      if (!el.offsetHeight) return;
      const cs = getComputedStyle(el);
      if (cs.position !== 'sticky' && cs.position !== 'fixed') return;
      maxEdge = Math.max(maxEdge, (parseFloat(cs.top) || 0) + el.offsetHeight);
    });
    return maxEdge;
  },

  /* 尺寸/布局变化时重算所有列表高度（去抖 120ms）。只在第一次 mountTable 时绑一次，
     避免监听器随每次重渲染堆积。

     `childList` 观察是必需的，不是保险：列表之外的内容（表单卡、说明卡、其它列表）
     常常在 `mountTable` **之后**才渲染完，只在 mount 时算一次会拿到偏小的「卡片下方留白」，
     把列表留得过高 —— 实测在「账号权限管理」上算出 199px，而真实值只有 137px，
     页面滚到底时表头正好被筛选条压住 40px。
     只观察 childList（**不含 attributes**），所以本方法自己写 max-height 不会反过来
     触发它，不会自激。 */
  _bindTableFit() {
    if (this._dtFitBound) return;
    this._dtFitBound = true;
    let timer = null;
    const run = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        // 容器可能已被 innerHTML 覆盖（旧页面的 tableWrap 被换掉），过滤掉脱离文档的
        this._dtHosts = this._dtHosts.filter(h => h && document.contains(h));
        this._dtHosts.forEach(h => this._fitTableHeight(h));
      }, 120);
    };
    window.addEventListener('resize', run);
    window.addEventListener('orientationchange', run);
    const scope = document.querySelector('.portal-content') || document.body;
    if (scope) new MutationObserver(run).observe(scope, { childList: true, subtree: true });
  },

  _startColResize(e, handle, el, opts, id, prefs) {
    e.preventDefault();
    e.stopPropagation();
    const th = handle.closest('th');
    const table = handle.closest('table');
    const colIndex = parseInt(handle.dataset.dtCol, 10);
    const startX = (e.touches ? e.touches[0].clientX : e.clientX);
    const startW = th.offsetWidth;
    const move = (ev) => {
      const x = (ev.touches ? ev.touches[0].clientX : ev.clientX);
      const w = Math.max(48, startW + (x - startX));
      if (table.style.tableLayout !== 'fixed') {
        // 首次拖拽：把当前自然宽度固化为列宽
        table.style.tableLayout = 'fixed';
        const widths = Array.from(table.querySelectorAll('thead th')).map(t => t.offsetWidth);
        table.querySelectorAll('colgroup col').forEach((c, i) => {
          if (widths[i]) c.style.width = widths[i] + 'px';
        });
      }
      table.querySelectorAll('colgroup col')[colIndex].style.width = w + 'px';
      th.style.width = w + 'px';
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      document.removeEventListener('touchmove', move);
      document.removeEventListener('touchend', up);
      const widths = Array.from(table.querySelectorAll('colgroup col')).map(c => parseInt(c.style.width, 10) || null);
      const saved = this._pref('tbl.' + id) || {};
      saved.widths = widths;
      this._pref('tbl.' + id, saved);
      this.toast('列宽已保存', 'info', 1200);
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
    document.addEventListener('touchmove', move, { passive: false });
    document.addEventListener('touchend', up);
  },

  // === 数量步进控件（PC/移动端统一） ===
  /**
   * 给数量输入框附加显式 −/+ 按钮。
   *
   * 移动端浏览器**不显示** `<input type="number">` 的原生上下箭头（iOS/Android
   * 平台行为，CSS 无法强制显示），导致手机端只能靠键盘输入，与 PC 端「可上下选择」
   * 体验不一致。这里统一改成两端都有的显式按钮，并由 CSS 隐藏 PC 端原生箭头，
   * 避免同屏出现「原生箭头 + 自定义按钮」两套控件。
   *
   * 幂等：同一输入框重复调用只绑定一次（`data-qty-bound` 标记）。
   *
   * @param {string|Element} target 输入框元素或 id
   * @param {object} opts { min, max, onChange(nextValue) }
   *        min/max 缺省时读 input 的 min/max 属性；onChange 缺省派发 input 事件
   */
  bindQtyStepper(target, opts = {}) {
    const input = this._resolveEl(target);
    if (!input || input.dataset.qtyBound) return null;
    input.dataset.qtyBound = '1';

    const min = opts.min != null ? opts.min : (parseInt(input.min, 10) || 1);
    const max = opts.max != null ? opts.max : (parseInt(input.max, 10) || 9999);

    const wrap = document.createElement('div');
    wrap.className = 'qty-stepper';
    input.parentNode.insertBefore(wrap, input);

    const makeBtn = (text, delta, label) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-secondary btn-sm qty-step-btn';
      b.textContent = text;
      b.setAttribute('aria-label', label);
      b.addEventListener('click', () => {
        const cur = parseInt(input.value, 10);
        const next = Math.min(max, Math.max(min, (isNaN(cur) ? min : cur) + delta));
        if (next === (isNaN(cur) ? min : cur)) return;
        input.value = next;
        if (typeof opts.onChange === 'function') opts.onChange(next);
        else input.dispatchEvent(new Event('input', { bubbles: true }));
      });
      return b;
    };

    wrap.appendChild(makeBtn('−', -1, '减少数量'));
    wrap.appendChild(input);
    wrap.appendChild(makeBtn('+', 1, '增加数量'));
    return wrap;
  },

  // === 渲染分页 ===
  renderPagination({ total, current, pageSize = 10 }) {
    const totalPages = Math.ceil(total / pageSize);
    if (totalPages <= 1) return '';
    let html = '<div class="pagination"><span class="pagination-info">共 ' + total + ' 条</span>';
    html += `<button class="pagination-btn" ${current <= 1 ? 'disabled' : ''} data-page="${current - 1}">‹</button>`;
    for (let i = 1; i <= totalPages; i++) {
      if (i === 1 || i === totalPages || (i >= current - 1 && i <= current + 1)) {
        html += `<button class="pagination-btn ${i === current ? 'active' : ''}" data-page="${i}">${i}</button>`;
      } else if (i === current - 2 || i === current + 2) {
        html += `<button class="pagination-btn" disabled>...</button>`;
      }
    }
    html += `<button class="pagination-btn" ${current >= totalPages ? 'disabled' : ''} data-page="${current + 1}">›</button>`;
    html += '</div>';
    return html;
  },

  // === 描述列表 ===
  renderDescList(items) {
    let html = '<div class="desc-list">';
    items.forEach(item => {
      html += `<div class="desc-item ${item.full ? 'desc-full' : ''}">`;
      html += `<div class="desc-label">${item.label}</div>`;
      html += `<div class="desc-value">${item.value ?? '—'}</div>`;
      html += '</div>';
    });
    html += '</div>';
    return html;
  },

  // === 签名（页面只预览，手写在弹窗里）===

  /* 画布笔迹绑定。只管「这块 canvas 上画了什么」，与页面/弹窗无关。
   *
   * ⚠ 只应绑定**一次**（弹窗的 canvas 是全页单例）：鼠标抬手监听挂在 window 上，
   * 每次重开都绑一遍会不断累积监听器。
   */
  _bindSigCanvas(canvas) {
    const ctx = canvas.getContext('2d');
    let drawing = false, lastX = 0, lastY = 0, ratio = 1;

    /* 画布尺寸必须按设备像素比放大，否则高分屏上笔迹发虚、坐标也会偏。
       每次打开弹窗都重新量一次 —— 手机横竖屏切换后宽度会变。 */
    const resize = () => {
      const cssW = Math.max(canvas.clientWidth || 0, 1);
      const cssH = Math.max(canvas.clientHeight || 0, 1);
      ratio = Math.max(window.devicePixelRatio || 1, 1);
      canvas.width = Math.round(cssW * ratio);
      canvas.height = Math.round(cssH * ratio);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.scale(ratio, ratio);
      ctx.strokeStyle = '#1C1C1C';
      ctx.fillStyle = '#1C1C1C';
      ctx.lineWidth = 2;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.clearRect(0, 0, cssW, cssH);
    };

    /* 触摸/鼠标坐标 → 画布坐标。
       不能直接用 clientX - rect.left：CSS 把 canvas 拉伸过之后
       rect 与 clientWidth 不再相等，笔迹会整体偏移。 */
    const pos = (e) => {
      const rect = canvas.getBoundingClientRect();
      const src = (e.touches && e.touches[0]) || e;
      const sx = rect.width ? canvas.clientWidth / rect.width : 1;
      const sy = rect.height ? canvas.clientHeight / rect.height : 1;
      return [(src.clientX - rect.left) * sx, (src.clientY - rect.top) * sy];
    };

    const start = (e) => {
      e.preventDefault();          // 阻断后续合成的鼠标事件与页面滚动
      drawing = true;
      [lastX, lastY] = pos(e);
      // 只点一下不拖也要留痕：moveTo/lineTo 同一个点画不出东西，补一个圆点
      ctx.beginPath();
      ctx.arc(lastX, lastY, ctx.lineWidth / 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(lastX, lastY);
    };
    const move = (e) => {
      if (!drawing) return;
      e.preventDefault();
      const [x, y] = pos(e);
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(x, y);
      [lastX, lastY] = [x, y];
    };
    const end = () => { drawing = false; };

    canvas.addEventListener('mousedown', start);
    canvas.addEventListener('mousemove', move);
    canvas.addEventListener('mouseleave', end);
    // 抬手挂在 window：手指拖出画布再松开，不挂 window 会一直停在 drawing
    window.addEventListener('mouseup', end);
    canvas.addEventListener('touchstart', start, { passive: false });
    canvas.addEventListener('touchmove', move, { passive: false });
    canvas.addEventListener('touchend', end);
    canvas.addEventListener('touchcancel', end);

    /* 有没有落笔：只看 alpha 通道。签名是纯黑笔画 + 全透明底，
       alpha 非 0 就说明画过；比整张图逐字节比较快得多。 */
    const hasInk = () => {
      const d = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      for (let i = 3; i < d.length; i += 4) if (d[i] !== 0) return true;
      return false;
    };

    return {
      resize,
      clear() { ctx.clearRect(0, 0, canvas.width, canvas.height); },
      hasInk,
      isEmpty() { return !hasInk(); },
      getDataURL() { return canvas.toDataURL('image/png'); },
      /* 把一张图按「等比缩放 + 居中」铺回画布（回显已有签名）。
         ctx 上已经应用了 scale(ratio)，所以这里用 CSS 像素坐标即可。 */
      drawScaled(img) {
        const cw = canvas.clientWidth, ch = canvas.clientHeight;
        const s = Math.min(cw / img.width, ch / img.height, 1);
        const w = img.width * s, h = img.height * s;
        ctx.drawImage(img, (cw - w) / 2, (ch - h) / 2, w, h);
      },
    };
  },

  /* 全页共用一个签名弹窗：每次重开都新建 DOM 会累积事件监听。 */
  _sigModalState: null,

  _openSignatureModal({ title, initial, onCommit }) {
    let st = this._sigModalState;
    if (!st) {
      const overlay = document.createElement('div');
      overlay.className = 'sig-modal';
      overlay.innerHTML =
        '<div class="sig-modal-panel" role="dialog" aria-modal="true">' +
          '<div class="sig-modal-title"></div>' +
          '<div class="sig-modal-hint">请在下方空白处签名；点「确定」保存，点「取消」不保存。</div>' +
          '<canvas class="sig-canvas"></canvas>' +
          '<div class="sig-modal-actions">' +
            '<button type="button" class="btn btn-secondary btn-sm sig-spacer" data-sig-act="clear">重写</button>' +
            '<button type="button" class="btn btn-secondary" data-sig-act="cancel">取消</button>' +
            '<button type="button" class="btn btn-primary" data-sig-act="ok">确定</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(overlay);

      st = this._sigModalState = {
        overlay,
        titleEl: overlay.querySelector('.sig-modal-title'),
        pad: this._bindSigCanvas(overlay.querySelector('.sig-canvas')),
        onCommit: null,
        loadToken: 0,
        prevOverflow: '',
      };

      const act = (name) => {
        const s = this._sigModalState;
        if (name === 'clear') { s.pad.clear(); return; }
        if (name === 'ok') {
          // 画布空白 = 明确表示「没有签名」，提交 null（调用方据此判必填）
          this._closeSignatureModal(s.pad.isEmpty() ? null : s.pad.getDataURL());
          return;
        }
        this._closeSignatureModal(undefined);   // 取消：undefined 表示「不提交」
      };

      overlay.addEventListener('click', (e) => {
        const btn = e.target.closest ? e.target.closest('[data-sig-act]') : null;
        if (btn) { act(btn.getAttribute('data-sig-act')); return; }
        // 点遮罩 = 取消。必须判断 target 就是遮罩本身，否则点面板内部也会误关
        if (e.target === overlay) act('cancel');
      });

      /* 横竖屏切换后画布尺寸会变：重新量尺寸，并把已有笔迹按新尺寸铺回去，
         否则用户转个屏，刚写的签名就没了。 */
      st.onWinResize = () => {
        const s = this._sigModalState;
        if (!s || !s.overlay.classList.contains('show')) return;
        const keep = s.pad.isEmpty() ? null : s.pad.getDataURL();
        s.pad.resize();
        if (keep) this._drawSigImage(keep);
      };
      window.addEventListener('resize', st.onWinResize);
      window.addEventListener('orientationchange', st.onWinResize);
      document.addEventListener('keydown', (e) => {
        const s = this._sigModalState;
        if (e.key === 'Escape' && s && s.overlay.classList.contains('show')) {
          this._closeSignatureModal(undefined);
        }
      });
    }

    st.titleEl.textContent = title || '手写签名';
    st.onCommit = onCommit;
    st.overlay.classList.add('show');
    // 必须先显示再量尺寸：display:none 的元素 clientWidth 是 0，量不到宽度
    st.pad.resize();
    // 作废上一次打开时还在解码中的图片
    st.loadToken += 1;
    if (initial) this._drawSigImage(initial);
    st.prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  },

  /* 把 dataURL 铺回弹窗画布。图片解码是异步的，用 loadToken 作废过期回调 ——
     用户在图片加载完之前就关掉/重开了弹窗，回调再画上去就是错的。 */
  _drawSigImage(url) {
    const st = this._sigModalState;
    if (!st) return;
    const token = ++st.loadToken;
    const img = new Image();
    img.onload = () => {
      if (!st.overlay.classList.contains('show')) return;
      if (token !== st.loadToken) return;
      st.pad.drawScaled(img);
    };
    img.src = url;
  },

  _closeSignatureModal(result) {
    const st = this._sigModalState;
    if (!st) return;
    st.overlay.classList.remove('show');
    document.body.style.overflow = st.prevOverflow || '';
    const cb = st.onCommit;
    st.onCommit = null;
    st.pad.clear();
    // result === undefined 表示取消：回调一次都不调，页面保持原值
    if (result !== undefined && typeof cb === 'function') cb(result);
  },

  /* 把 `el` 变成「预览 + 弹窗手写」的签名字段。
   *
   * 返回值与旧 `initSignaturePad` **同名同义**（isEmpty / getDataURL / clear），
   * 所以调用点几乎不用改；另加 isDirty / setValue 供「编辑已有签名」的场景使用。
   *
   * `opts`：{ title, initial, onChange }
   */
  initSignatureField(el, opts) {
    if (!el) throw new Error('initSignatureField: 容器元素不存在');
    const o = opts || {};
    el.classList.add('sig-field');
    el.innerHTML =
      '<div class="sig-preview" data-sig-preview></div>' +
      '<div class="sig-tools">' +
        '<button type="button" class="btn btn-secondary btn-sm" data-sig-sign>签名</button>' +
        '<button type="button" class="btn btn-secondary btn-sm" data-sig-clear>清除</button>' +
      '</div>';

    const preview = el.querySelector('[data-sig-preview]');
    const btnSign = el.querySelector('[data-sig-sign]');
    const btnClear = el.querySelector('[data-sig-clear]');

    let committed = o.initial || null;   // 已确认的签名（dataURL），null = 没有
    let dirty = false;                   // 本次会话是否改过（编辑场景据此决定要不要重传）

    const render = () => {
      el.classList.toggle('has-value', !!committed);
      preview.innerHTML = committed
        ? '<img src="' + committed + '" alt="电子签名">'
        : '<span class="sig-placeholder">点击此处手写签名</span>';
      btnSign.textContent = committed ? '重新签名' : '签名';
      btnClear.hidden = !committed;
    };

    const api = {
      isEmpty() { return !committed; },
      getDataURL() { return committed; },
      isDirty() { return dirty; },
      setValue(url) { committed = url || null; dirty = false; render(); },
      clear() { if (committed) { committed = null; dirty = true; } render(); },
    };

    const open = () => {
      this._openSignatureModal({
        title: o.title || '手写签名',
        initial: committed,
        onCommit: (url) => {
          // 与打开前一模一样就不算「改过」：编辑页据此决定要不要重传文件
          dirty = url !== committed;
          committed = url;
          render();
          if (typeof o.onChange === 'function') o.onChange(committed);
        },
      });
    };

    // 预览块与「签名」按钮都能唤起弹窗；预览块本身不拦滚动手势
    preview.addEventListener('click', open);
    btnSign.addEventListener('click', open);
    btnClear.addEventListener('click', () => api.clear());
    render();
    return api;
  },

  // === 格式化日期 ===
  /** 把后端返回的日期/时间串转成**本地** `YYYY-MM-DD`。
   *
   * ⚠ 存在的理由：后端 `serialize_instance` 对 `DateTimeField` 用 `isoformat()`，
   * `USE_TZ=True` 下是 **UTC 带偏移**（`2026-09-22T06:05:50+00:00`）。直接
   * `.slice(0, 10)` 拿到的是 **UTC 日期**，拿它跟用户选的**本地日期**比较，
   * 北京时间 00:00–08:00 的记录会被算到**前一天** —— 用户选「今天」看不到它们。
   * 后端筛选口径已经是本地日期（`timezone.localdate()` / `__date` 查询），前端必须对齐。
   *
   * 纯日期串（`DateField` 产物，如 `2026-09-22`）**原样返回**：ES 规范把
   * date-only form 当 **UTC 午夜**，负时区下 `new Date('2026-09-22')` 会退到 21 日。
   * 纯日期不参与时区换算。
   *
   * 用途：**日期范围筛选**的比较键。
   */
  localDateStr(date) {
    if (!date) return '';
    const s = String(date);
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
    const d = new Date(s);
    if (isNaN(d)) return s;   // 解析不了就原样返回（与 formatDate 一致），不假装是日期
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  },

  /** 一条记录是否落在 `[start, end]` 区间内（**含两端**）。
   *
   * 全站**唯一**的起止时间判据 —— 各端列表与「数据概览」共用，避免每个调用点
   * 各写一份 `if (v.start_date && d < v.start_date) return false;` 然后漂移
   * （已经漂过一次：有的地方比 `created_at`、有的比 `date`、有的漏了 `end_date`）。
   *
   * @param {string} dateValue 行上的时间字段。可以是 `DateTimeField` 序列化出的
   *        ISO 串（`USE_TZ=True` 下是 **UTC 带偏移**），也可以是 `DateField` 的
   *        纯日期串 —— 统一交给 `localDateStr()` 换算成**本地日期**再比较，
   *        与后端 `__date` 查询同口径。**绝不能裸截断 `.slice(0,10)`**：
   *        北京时间 00:00–08:00 的记录会被算到前一天。
   * @param {string} start 起始日期 `YYYY-MM-DD`，空 = 不限
   * @param {string} end   结束日期 `YYYY-MM-DD`，空 = 不限
   *
   * 语义要点：
   *   - **未设范围时恒为 true** —— 不设筛选时不改变任何既有行为；
   *   - 设了范围而该行**没有时间**时返回 false：用户既然圈了区间，
   *     一条没有时间的记录混在结果里就是错的（宁可少、不可错）。
   *
   * ⚠ 上面那条「没有时间就排除」只有在**每行都拿得到时间**时才是对的。
   * 很多业务时间字段是**可空**的，因为事件还没发生：`Adoption.adopted_at`
   * （待领出时为空）、`Transfer.received_at`（待签收时为空）、
   * `Euthanasia.euthanized_at`、`Release.released_at` 都是这样。
   * 直接拿它们当唯一判据的后果不是报错，而是**用户圈一个「全部」区间，
   * 这些行反而消失**（GUI 实测：`Adoption` 5 行圈 2000~2099 后只剩 3 行）。
   * 所以调用点必须用 `firstDate()` 补上创建时间兜底。
   */
  inDateRange(dateValue, start, end) {
    if (!start && !end) return true;
    const d = this.localDateStr(dateValue);
    if (!d) return false;
    if (start && d < start) return false;
    if (end && d > end) return false;
    return true;
  },

  /** 取第一个有值的时间，用于「业务发生时间 → 记录创建时间」兜底。
   *
   * 一行记录的「时间」= 它最后一次业务活动发生的时间；活动还没发生，
   * 就退到它被创建的时间。**任何情况下都不该因为「没有时间」被丢掉。**
   *
   * 用法（把驼峰与蛇形两种别名都列上，因为 `serialize_instance` 两个都给）：
   *   `TNR_UI.firstDate(r.adoptedAt, r.adopted_at, r.createdAt, r.created_at)`
   */
  firstDate(...values) {
    for (let i = 0; i < values.length; i++) {
      if (values[i]) return values[i];
    }
    return '';
  },

  formatDate(date) {
    if (!date) return '—';
    const s = String(date);
    // 纯日期串直接返回：date-only 在 ES 里按 **UTC 午夜** 解析，负时区会退一天。
    // （UTC+8 下与旧行为完全一致，这里只是把这条隐含前提写死。）
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
    const d = new Date(s);
    if (isNaN(d)) return date;
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  },

  formatDateTime(date) {
    if (!date) return '—';
    const s = String(date);
    // 纯日期串（DateField 产物）**只返回日期**，不补 `08:00`。
    // 旧实现把它当 UTC 午夜再按本地取小时 → UTC+8 下渲染成 `2026-09-22 08:00`，
    // 一个日期字段凭空长出「08:00」。gov 台账有多处 `formatDateTime(r.date)`。
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
    const d = new Date(s);
    if (isNaN(d)) return date;
    return this.formatDate(date) + ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  },

  /* 手机端列压缩的行内补充内容（见 mountTable 的 `mobileRender`）。
     标签是页面写死的文案，值由调用方负责转义 —— 这里不做二次处理。 */
  mobileLine(label, val) {
    return '<div class="dt-mobile-line"><span class="dt-mobile-label">' + label
      + '</span><span class="dt-mobile-value">' + val + '</span></div>';
  },

  mobileMore(inner) {
    return '<details class="dt-mobile-more"><summary>更多信息</summary>' + inner + '</details>';
  },

  /** 今天（本地）YYYY-MM-DD，用作表单日期默认值。
   *
   * **不要用 `new Date().toISOString().substr(0,10)`** —— toISOString() 返回
   * **UTC**，北京时间 00:00–08:00 会退到前一天，表单默认日期就是错的。
   * 与后端 `generate_pet_codes` 曾用 `timezone.now()` 是同一类坑。
   */
  todayStr() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  },

  /** 当前本地日期时间 `YYYY-MM-DDTHH:mm`，用于 `<input type="datetime-local">` 默认值。
   *  同样不能用 toISOString()（UTC，会差 8 小时且可能跨天）。
   */
  nowLocalStr() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return this.todayStr() + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  },

  // === 生成单据编号 ===
  genLedgerNo(prefix) {
    return TNR_DB._genLedgerNo(prefix);
  },

  // === 状态徽章 ===
  statusBadge(text, type = 'default') {
    return `<span class="badge badge-${type}">${text}</span>`;
  },

  /* 把状态真源转成筛选条选项（顺序 = 真源定义顺序）。
     筛选条逐条手写等于多一处「后端加一档时必定漏掉」的副本；
     生成式选项的覆盖性由「真源 ↔ 后端 choices」的判据负责。 */
  statusOptions(table) {
    return Object.keys(TNR_UI[table]).map(value => ({
      value, label: TNR_UI[table][value].text,
    }));
  },

  /* === 宠物状态的唯一显示真源（第五十二轮电脑端巡检）===
     同一个 `Pet.status` 此前有**三份**显示表，而且已经抄漂移：
       - `tnr-api.js` 的原取值函数：`in_treatment` → 「待诊疗/诊疗中」、`pending_claim` → cinnabar；
       - `gov.js` 的本地 `petStatus`：`in_treatment` → 「待诊疗」、warning；
       - `shelter.js` 的通用 `statusBadge`：`in_treatment` → 「诊疗中」、`owner_returned` → 「已领回」。
     后果不是难看：同一只动物在捕捉点端叫「诊疗中」、监管端叫「待诊疗」、
     医院端叫「待诊疗/诊疗中」，而**纸质单据与监管报表**用的是后端 choices 的
     「待诊疗」——三端对账时会被当成三种不同的流转阶段。
     文案以 `business/models.py` 的 `Pet.STATUS_CHOICES` 为准（闸门逐条比相等），
     配色取此前覆盖面最广的那一份（`TNR_API` 的徽章表，五端都在用）。
     ⚠ 这里是**展示**真源，不改变状态机本身：合法取值仍由后端 choices 决定。 */
  PET_STATUS: {
    in_transit:     { text: '在途',          badge: 'warning' },
    in_treatment:   { text: '待诊疗',        badge: 'info' },
    pending_adopt:  { text: '待领养',        badge: 'cinnabar' },
    return_pending: { text: '待运回捕捉点',  badge: 'info' },
    pending_release:{ text: '待放归',        badge: 'warning' },
    pending_claim:  { text: '待领出',        badge: 'cinnabar' },
    adopted:        { text: '已领养',        badge: 'success' },
    released:       { text: '已放归',        badge: 'success' },
    euthanized:     { text: '已安乐死',      badge: 'danger' },
    dead:           { text: '已死亡/无害化', badge: 'danger' },
    owner_returned: { text: '主人领回',      badge: 'default' },
  },

  /* 宠物状态的取值函数（与 RELEASE_STATUS 等同一套写法）。
     徽章整段返回，调用方不要再自己拼 `<span class="badge …">`，
     更不要用 `.replace('badge-', '')` 把类名拆回来 —— 那等于把
     「配色」和「展示」拆成两步，改真源时总有一处漏掉。 */
  petStatusText(status) {
    const m = TNR_UI.PET_STATUS[status];
    return m ? m.text : (status || '—');
  },

  petStatusBadge(status) {
    const m = TNR_UI.PET_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.petStatusText(status)) + '</span>';
  },

  /* 一宠一档的「当前阶段」：`pet.stage` 由 `pet_lifecycle` 按业务单据派生，
     可能是 `Pet.STATUS_CHOICES` 之外的中间态（如 `pending_discharge`）。
     已知状态码仍走 `PET_STATUS` 真源；派生态直接用后端给的文案，
     前端不再维护第二张状态表。 */
  petStageBadge(stage, label) {
    if (stage && TNR_UI.PET_STATUS[stage]) return TNR_UI.petStatusBadge(stage);
    return `<span class="badge badge-warning">` +
      TNR_UI.escape(label || stage || '—') + '</span>';
  },

  /* === 放归单状态的唯一显示真源（第五十二轮，做法与 PET_STATUS 一致）===
     `Release.status` 同样被抄了三份，中间态 `transferred`（已回小区待放归）
     在两份里**根本不存在**：
       - `hospital.js` 出院看板：三段齐全（唯一写全的一处）；
       - `gov.js` 放归监管：`released ? 已放归 : 待放归` 的二值兜底 ——
         动物已经回到小区、只差物业签字，监管端仍显示「待放归」，
         两段流程走到哪一段完全看不出来；
       - `shelter.js` 放归页签：`filter(status === 'pending')` ——
         `transferred` 的单子既不进待放归也不进已放归，**整张单从界面上消失**，
         办理人也再也点不到「确认放归」，单据永远停在 transferred。
     文案以 `business/models.py` 的 `Release.STATUS_CHOICES` 为准（闸门逐条比相等）。
     ⚠ 本表只管展示，**不要拿它的键当「未办结」判据**：列表筛选请用
     `OPEN_RELEASE_STATUS`（口径见下面那条）。 */
  RELEASE_STATUS: {
    pending:     { text: '待转运回小区',   badge: 'warning' },
    transferred: { text: '已回小区待放归', badge: 'info' },
    released:    { text: '已放归',         badge: 'success' },
  },

  /* 「待放归」= 尚未办结的放归单 = `pending` ∪ `transferred`。
     这条口径的**唯一定义处**是后端 `release_confirm`
     （`business/views_release.py`：`if release.status not in ('pending', 'transferred')`），
     第四十八轮把放归拆成两段之前只有 `pending`，前端如果继续只筛 `pending`，
     拆段的中间态就成了界面盲区。前端三处列表/判据都必须引用本数组，
     不要再各自写 `=== 'pending'`。 */
  OPEN_RELEASE_STATUS: ['pending', 'transferred'],

  /* === 运回单状态的唯一显示真源（第五十四轮）===
     `ReturnOrder.status`：医院开单后待捕捉点确认，捕捉点可接收/驳回，
     医院在确认前可撤回。文案与后端 `ReturnOrder.STATUS_CHOICES` 保持一致。 */
  RETURN_ORDER_STATUS: {
    pending:   { text: '待确认',     badge: 'warning' },
    received:  { text: '已确认出库', badge: 'success' },
    rejected:  { text: '已驳回',     badge: 'danger' },
    withdrawn: { text: '已撤回',     badge: 'default' },
  },

  returnOrderStatusText(status) {
    const m = TNR_UI.RETURN_ORDER_STATUS[status];
    return m ? m.text : (status || '—');
  },

  returnOrderStatusBadge(status) {
    const m = TNR_UI.RETURN_ORDER_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.returnOrderStatusText(status)) + '</span>';
  },

  /* 放归状态的纯文案（时间线、抽屉等不带徽章的地方用）。
     真源查不到时显示原始枚举码，不猜成某个具体业务状态 ——
     「released 之外一律叫待放归」会把「已回小区、只差签字」也压成待放归。 */
  releaseStatusText(status) {
    const m = TNR_UI.RELEASE_STATUS[status];
    return m ? m.text : (status || '—');
  },

  /* 放归状态徽章：真源查不到时显示原始枚举码（不猜成某个具体业务状态）。
     与 `petStatusText` 同一套兜底口径。
     `title` 可选，用作悬停说明（例如把转运时间挂在徽章上，省掉一列）。 */
  releaseStatusBadge(status, title) {
    const m = TNR_UI.RELEASE_STATUS[status];
    const tip = title ? ' title="' + TNR_UI.escape(title) + '"' : '';
    return `<span class="badge badge-${m ? m.badge : 'default'}"${tip}>` +
      TNR_UI.escape(TNR_UI.releaseStatusText(status)) + '</span>';
  },

  /* === 回访打卡审核状态的唯一显示真源（做法与 PET_STATUS / RELEASE_STATUS 一致）===
     `CheckIn.status` 三个取值此前散在四处：
       - `shelter.js` 回访页：徽章 + 筛选下拉 + 详情抽屉，写的是「未通过」；
       - `adopter.js`：`getCheckinStatusBadge()` 自带一份，写的是「已驳回」；
       - `gov.js` 领养业务详情的打卡时间线：`c.status === 'approved' ? '已审核'
         : '待审核'` —— 二值兜底把**已驳回**的打卡显示成「待审核」，
         监管端看不出这条回访其实已经判过不合格，也就不会触发后面的收回。
     后端 `CheckIn.STATUS_CHOICES`（`business/models.py`）的 label 是
     待审核 / 已通过 / 已驳回，文案以它为准（闸门逐条比相等）。 */
  CHECKIN_STATUS: {
    pending:  { text: '待审核', badge: 'warning' },
    approved: { text: '已通过', badge: 'success' },
    rejected: { text: '已驳回', badge: 'danger' },
  },

  checkinStatusText(status) {
    const m = TNR_UI.CHECKIN_STATUS[status];
    return m ? m.text : (status || '—');
  },

  checkinStatusBadge(status) {
    const m = TNR_UI.CHECKIN_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.checkinStatusText(status)) + '</span>';
  },

  /* === 捕捉单状态的唯一显示真源 ===
     `Capture.status` 由后端按「本单宠物的转运情况」推导
     （待转运 / 部分转运 / 已完成 / 已作废），不许人工填。
     此前监管端两张表（全业务列表 + 台账列表）、监管端详情抽屉、捕捉点端
     `Shelter.captureStatusBadge` 各写一份 —— 四份文案恰好一致才没出事，
     后端加一档必然漏一处。 */
  CAPTURE_STATUS: {
    pending:   { text: '待转运',   badge: 'warning' },
    partial:   { text: '部分转运', badge: 'info' },
    completed: { text: '已完成',   badge: 'success' },
    void:      { text: '已作废',   badge: 'default' },
  },

  captureStatusText(status) {
    const m = TNR_UI.CAPTURE_STATUS[status];
    return m ? m.text : (status || '—');
  },

  captureStatusBadge(status) {
    const m = TNR_UI.CAPTURE_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.captureStatusText(status)) + '</span>';
  },

  captureStatusOptions() {
    return TNR_UI.statusOptions('CAPTURE_STATUS');
  },

  /* === 转运单状态的唯一显示真源 ===
     ⚠ 两个坑：
       1) `pending` 后端叫**待签收**、`received` 叫**已签收**，监管端三处
          都写成「待接收 / 已接收」—— 与医院端、捕捉点端对不上账；
       2) 监管端三处**都漏了 `partial`（部分签收）**，而兜底写的是
          `m[r.status] || { t: r.status }` / `|| '待签收'`：
          一半动物已签收的转运单，在监管端状态列直接印出英文码 `partial`，
          在领养人端的生命周期时间线里又被兜底成「待签收」——
          两种显示都看不出「已经签收了一部分」这件事。 */
  TRANSFER_STATUS: {
    pending:  { text: '待签收',   badge: 'warning' },
    partial:  { text: '部分签收', badge: 'info' },
    received: { text: '已签收',   badge: 'success' },
    rejected: { text: '已驳回',   badge: 'danger' },
    void:     { text: '已撤回',   badge: 'default' },
  },

  /* 「待签收」= 还没签完 = `pending` ∪ `partial`。
     口径的唯一定义处是后端 `Transfer.OPEN_STATUSES`（签收与驳回两处门禁都读它），
     闸门逐条比对两边取值。医院可以先签一部分，剩余动物仍在待办里；只认 `pending`
     会让部分签收的单子从待办列表消失、行内没有「去签收」按钮，
     而捕捉点仍把这几只动物算作在途占用 —— 谁都不会再去看这张单。 */
  OPEN_TRANSFER_STATUS: ['pending', 'partial'],

  transferStatusText(status) {
    const m = TNR_UI.TRANSFER_STATUS[status];
    return m ? m.text : (status || '—');
  },

  transferStatusBadge(status) {
    const m = TNR_UI.TRANSFER_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.transferStatusText(status)) + '</span>';
  },

  transferStatusOptions() {
    return TNR_UI.statusOptions('TRANSFER_STATUS');
  },

  isOpenTransferStatus(status) {
    return TNR_UI.OPEN_TRANSFER_STATUS.indexOf(status) !== -1;
  },

  /* === 诊疗单状态的唯一显示真源 ===
     `Treatment.status` 只有进行中 / 已完成两档。监管端两张表一份写「诊疗中」、
     一份写「进行中」，配色还一黄一蓝 —— 「诊疗中」其实是**阶段**
     （`Treatment.STAGE_CHOICES`：待接诊 / 诊疗中 / 诊疗完毕）的文案被抄来当了状态名。
     阶段与状态是两个字段，取值也不同，不许互相冒名（阶段的显示另有医院端流程标签）。 */
  TREATMENT_STATUS: {
    in_progress: { text: '进行中', badge: 'info' },
    completed:   { text: '已完成', badge: 'success' },
  },

  treatmentStatusText(status) {
    const m = TNR_UI.TREATMENT_STATUS[status];
    return m ? m.text : (status || '—');
  },

  treatmentStatusBadge(status) {
    const m = TNR_UI.TREATMENT_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.treatmentStatusText(status)) + '</span>';
  },

  /* === 诊疗「阶段」的唯一显示真源（三态 + 看板五态两张表）===
     阶段有两个粒度，都是**后端派生值**（不存列）：
       - `Treatment.stage`：checked_at/done_at 推三档，文案 = `STAGE_CHOICES` 的 label；
       - 医院看板的 `board_stage`：再多看 diagnosed_at/disposed_at 推五档，
         判据在后端 `_board_stage()`，前端只许展示。
     本轮之前五档的文案只活在 `hospital.js` 的函数体里，同一个界面还各写一遍：
     `txStageBadge()` 一张 `[文案, badge]` 表、`renderBoard()` 的页签又一张、
     `txStageText()` 第三张（三档）。改一个页签名要翻三个方法，于是同一个 code
     `checking` 在页签叫「待检」、在详情的「当前阶段」叫「待接诊」——
     同一只动物、同一时刻，看板和抽屉两种说法。现在 `checking` 两张表都叫「待接诊」，
     `pending_diagnose` 叫「待诊断」（旧称「待诊」和「待接诊」只差一个字，扫一眼分不清）。
     阶段与状态是两个字段，取值不同，不许互相冒名（`TREATMENT_STATUS` 只管 进行中/已完成）。 */
  TREATMENT_STAGE: {
    checking: { text: '待接诊', badge: 'warning' },
    treating: { text: '诊疗中', badge: 'info' },
    done:     { text: '诊疗完毕', badge: 'success' },
  },

  treatmentStageText(stage) {
    const m = TNR_UI.TREATMENT_STAGE[stage];
    return m ? m.text : (stage || '—');
  },

  /* 看板五态的**流程顺序**，与后端 `Treatment.BOARD_STAGES` 逐位相同。
     页签、操作按钮、筛选都按它迭代，取值增减只改这一处 + 后端一处。 */
  BOARD_STAGE_ORDER: ['checking', 'pending_diagnose', 'treating',
                      'pending_discharge', 'discharged'],

  BOARD_STAGE: {
    checking:          { text: '待接诊',  badge: 'warning' },
    pending_diagnose:  { text: '待诊断',  badge: 'info' },
    treating:          { text: '诊疗中',  badge: 'info' },
    pending_discharge: { text: '待出院',  badge: 'warning' },
    discharged:        { text: '已出院',  badge: 'success' },
  },

  boardStageText(stage) {
    const m = TNR_UI.BOARD_STAGE[stage];
    return m ? m.text : (stage || '—');
  },

  boardStageBadge(stage) {
    const m = TNR_UI.BOARD_STAGE[stage];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.boardStageText(stage)) + '</span>';
  },

  /* === 「启用 / 停用」的唯一显示真源（区县 / 机构 / 账号三张表共用）===
     `District.status`、`Institution.status`、`User.status` 都是
     `CharField(default='active')`、**没有 choices**：取值域由后端常量
     `core.models.STATUS_VALUES` 界定，平台端 6 个写入点（三个开关 + 三个编辑口）
     与停用守卫读取侧都读它。

     本轮之前这一件事在界面上有 8 份写法：徽章表 1 份（里面还多写了一个后端
     **永远不会产出**的 `disabled` —— 幽灵态，「停用」有两种来源，出问题时分不清
     是哪个号真被停过）、状态筛选下拉 3 份、行内「停用/启用」按钮 3 份、
     捕捉点端「个人中心 · 账号状态」直接**写死绿色「启用」**（账号真被停用也照显示启用）。 */
  ENTITY_STATUS: {
    active:   { text: '启用', badge: 'success' },
    inactive: { text: '停用', badge: 'danger' },
  },

  entityStatusText(status) {
    const m = TNR_UI.ENTITY_STATUS[status];
    return m ? m.text : (status || '—');
  },

  entityStatusBadge(status) {
    const m = TNR_UI.ENTITY_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.entityStatusText(status)) + '</span>';
  },

  entityStatusOptions() {
    return Object.keys(TNR_UI.ENTITY_STATUS).map(k => ({
      value: k, label: TNR_UI.ENTITY_STATUS[k].text }));
  },

  /* 行内切换按钮的动作词 = **点下去之后变成的状态**，不是当前状态。
     以前三个列表各写一遍 `r.status === 'active' ? '停用' : '启用'`。 */
  entityToggleLabel(status) {
    return TNR_UI.entityStatusText(status === 'active' ? 'inactive' : 'active');
  },

  /* 同上：按钮配色也跟着**目标**状态的徽章色（停用=红、启用=绿），
     以前三份列表各自又写了一遍 `'danger' : 'success'`。 */
  entityToggleBtnClass(status) {
    const m = TNR_UI.ENTITY_STATUS[status === 'active' ? 'inactive' : 'active'];
    return m ? m.badge : 'default';
  },

  /* === 角色显示真源（文案 = `User.ROLE_CHOICES` 的 label）===
     平台端原来有五份：名称表、徽章表、账号筛选下拉、建号表单下拉、
     角色权限说明卡。名称表还与后端**对不上账** —— 后端 label 是
     「市级政府管理员 / 区级政府管理员」，平台端简写成「市级管理员 / 区级管理员」，
     而接口同时带出的 `role_display` 是全称：同一个账号，管理列表里一个职务名、
     个人中心里另一个。角色名是要进公文与授权说明的，两种叫法不能并存。 */
  ROLE_ORDER: ['platform_admin', 'gov_city', 'gov_district',
               'shelter', 'hospital', 'adopter'],

  ROLE: {
    platform_admin: { text: '平台管理员',     badge: 'ink' },
    gov_city:       { text: '市级政府管理员', badge: 'ink' },
    gov_district:   { text: '区级政府管理员', badge: 'info' },
    shelter:        { text: '捕捉点操作员',   badge: 'cinnabar' },
    hospital:       { text: '医院操作员',     badge: 'success' },
    adopter:        { text: '领养人',         badge: 'warning' },
  },

  roleText(role) {
    const m = TNR_UI.ROLE[role];
    return m ? m.text : (role || '—');
  },

  roleBadge(role) {
    const m = TNR_UI.ROLE[role];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.roleText(role)) + '</span>';
  },

  /* 建号/筛选的下拉选项。`exclude` 传角色数组：平台端不建领养人
     （由捕捉点在领养登记时自动创建），这一条业务规则留在调用点，
     文案与顺序仍取自真源。 */
  roleOptions(exclude) {
    const skip = exclude || [];
    return TNR_UI.ROLE_ORDER.filter(k => skip.indexOf(k) === -1)
      .map(k => ({ value: k, label: TNR_UI.ROLE[k].text }));
  },

  /* === 机构类型真源（文案 = `Institution.TYPE_CHOICES` 的 label）=== */
  INST_TYPE: {
    shelter:   { text: '捕捉点', badge: 'cinnabar' },
    hospital:  { text: '医院',   badge: 'success' },
    community: { text: '小区',   badge: 'info' },
  },

  instTypeText(type) {
    const m = TNR_UI.INST_TYPE[type];
    return m ? m.text : (type || '—');
  },

  instTypeBadge(type) {
    const m = TNR_UI.INST_TYPE[type];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.instTypeText(type)) + '</span>';
  },

  /* === 芯片四态真源（文案 = `Chip.STATUS_CHOICES` 的 label）===
     此前四份：捕捉点芯片列表、医院端选芯片、本文件「芯片追溯」抽屉，
     以及宠物状态表顺手复用了一次 `in_transit` 文案。其中两份还把**元组顺序写反**
     （一份 `[文案, badge]`、一份 `[badge, 文案]`），抄表时最容易错的形式。
     后端 label 是「已使用 / 已作废」，前端三份都写「已植入 / 作废」——
     芯片存在的意义就是逐号追溯，打印单据（读后端 label）与界面两种说法，
     对账时得多问一句「已植入是不是就是已使用」。 */
  CHIP_STATUS: {
    available:  { text: '可用',   badge: 'success' },
    in_transit: { text: '在途',   badge: 'warning' },
    used:       { text: '已使用', badge: 'cinnabar' },
    void:       { text: '已作废', badge: 'default' },
  },

  chipStatusText(status) {
    const m = TNR_UI.CHIP_STATUS[status];
    return m ? m.text : (status || '—');
  },

  chipStatusBadge(status) {
    const m = TNR_UI.CHIP_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.chipStatusText(status)) + '</span>';
  },

  /* === 领养单状态的唯一显示真源（做法与 PET_STATUS / RELEASE_STATUS 一致）===
     `Adoption.status` 三个取值此前有四份写法，同一个 code 四种叫法：
       - 后端 label：待领出 / 已完成 / 已取消；
       - `shelter.js` 筛选下拉：与后端一致（唯一对的一处）；
       - `shelter.js` 徽章兜底表：`cancelled` 叫「已撤销」；
       - `hospital.js` 出院看板：`completed` 叫「已领出」、`cancelled` 叫「已撤销」；
       - `adopter.js` 我的领养卡片：**硬编码「已领养」**，压根不看 status ——
         `pending_claim`（还没去医院领动物）与 `cancelled`（已取消）的单据
         也一律显示绿色「已领养」，领养人据此以为手续已办完。
     ⚠ 与 `PET_STATUS.pending_claim/adopted` 是两回事：那是**动物**的状态，
     这是**领养单**的状态；单据办结与动物出栏虽同义，字段不同、取值也不同。 */
  ADOPTION_STATUS: {
    pending_claim: { text: '待领出', badge: 'cinnabar' },
    completed:     { text: '已完成', badge: 'success' },
    cancelled:     { text: '已取消', badge: 'default' },
  },

  adoptionStatusText(status) {
    const m = TNR_UI.ADOPTION_STATUS[status];
    return m ? m.text : (status || '—');
  },

  adoptionStatusBadge(status) {
    const m = TNR_UI.ADOPTION_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.adoptionStatusText(status)) + '</span>';
  },

  adoptionStatusOptions() {
    return TNR_UI.statusOptions('ADOPTION_STATUS');
  },

  /* === 在线领养申请单状态的唯一显示真源 ===
     `AdoptionApplication.status` 只有领养人端在渲染，此前在本文件里手写了
     两张表（`getApplyStatusText` / `getApplyStatusBadge`），同一状态两处各排各的序。
     `cancelled`（领养人自己撤回，见 `applications/cancel/`）是第四档，
     机构端与监管端的筛选/徽章都得上它，不能再各写一份。 */
  APPLY_STATUS: {
    pending:  { text: '待审核', badge: 'warning' },
    approved: { text: '已通过', badge: 'success' },
    rejected: { text: '已拒绝', badge: 'danger' },
    cancelled: { text: '已取消', badge: 'default' },
  },

  applyStatusText(status) {
    const m = TNR_UI.APPLY_STATUS[status];
    return m ? m.text : (status || '—');
  },

  applyStatusBadge(status) {
    const m = TNR_UI.APPLY_STATUS[status];
    return `<span class="badge badge-${m ? m.badge : 'default'}">` +
      TNR_UI.escape(TNR_UI.applyStatusText(status)) + '</span>';
  },

  /* 把「可能为空的几个字段」拼成一行小字（物种 · 品种 · 性别 这类）。
     以前各处写 `a + ' · ' + b`，字段一空就留下「猫 · 」这种半截分隔符，
     名字为空时更是直接以「 · 」开头。空值跳过、全空给占位符。
     ⚠ 本函数**内部已转义**，调用方不要再套一次 escape。 */
  joinInfo(parts, sep = ' · ') {
    const list = (parts || []).filter(p => p !== null && p !== undefined && String(p).trim() !== '');
    return list.length ? list.map(p => TNR_UI.escape(String(p))).join(sep) : '—';
  },

  /* === 物料流水类型的唯一显示真源（第五十二轮 重构 B）===
     同样六个取值此前在捕捉点 / 医院 / 监管三端各抄一份，一共 7 张表，
     而且**已经抄漂移了**：`adjustment` 在捕捉点叫「库存异动」、监管端叫「调整」，
     `consume` 一处 `warning` 色、另一处 `cinnabar` 色 —— 同一笔流水在三端
     显示成三种样子，监管对账时会被当成三种不同的业务。
     后端 `MaterialTransaction.TYPE_CHOICES` 新增取值时，只改这里一行，
     三端的表格、抽屉、筛选下拉同时跟上。
     `long` 用在台账与筛选（要说清是哪一类动作），`short` 用在紧凑列与详情抽屉。 */
  MATERIAL_TXN_TYPES: {
    purchase:   { long: '采购入库', short: '采购', badge: 'success' },
    dispatch:   { long: '下发出库', short: '下发', badge: 'info' },
    receive:    { long: '签收入库', short: '签收', badge: 'cinnabar' },
    consume:    { long: '诊疗消耗', short: '消耗', badge: 'warning' },
    adjustment: { long: '库存异动', short: '异动', badge: 'danger' },
    outbound:   { long: '物料出库', short: '出库', badge: 'cinnabar' },
  },

  /* 视角差异**不是**漂移：同一笔 `dispatch`，捕捉点/监管端是「我发出去」
     （下发出库），医院端是「别人发给我」（捕捉点下发）。只显式标注真正
     因视角而变的这一处，别让它退化成「各端爱怎么写怎么写」。 */
  MATERIAL_TXN_VIEW_LABELS: {
    hospital: { dispatch: { long: '捕捉点下发', short: '捕捉点下发' } },
  },

  materialTxn(type, form = 'long', view = '') {
    const base = this.MATERIAL_TXN_TYPES[type];
    if (!base) return null;
    const over = (this.MATERIAL_TXN_VIEW_LABELS[view] || {})[type] || {};
    const text = over[form] || base[form];
    /* `form` 拼错（`'short'` 打成 `'shot'`）时必须返回 null，不能返回
       `{ text: undefined }` —— 那会在表格里渲染成字面量 "undefined"，
       而「这一列是 undefined」和「这一列漏了映射」在满屏数据里一样不显眼。
       退回 null 让调用方显示原始枚举码：错得看得见，才有机会被发现。 */
    return text ? { text, badge: base.badge } : null;
  },

  /* 未知类型显示原始枚举码（跟旧版逐字一致），且**必须转义** ——
     它是接口回来的字符串，不是这里写死的文案。 */
  materialTxnBadge(type, form = 'long', view = '') {
    const m = this.materialTxn(type, form, view);
    return this.statusBadge(m ? m.text : this.escape(String(type)),
                            m ? m.badge : 'default');
  },

  materialTxnOptions(form = 'long', view = '') {
    return Object.keys(this.MATERIAL_TXN_TYPES).map(t => {
      const m = this.materialTxn(t, form, view);
      return { value: t, label: m ? m.text : t };
    });
  },

  // === 渲染侧边栏 ===
  renderSidebar({ brand, brandSub, navGroups, user, activeId }) {
    let html = '<aside class="sidebar" id="tnrSidebar">';
    html += `<div class="sidebar-brand"><div class="sidebar-brand-icon">${brand.icon || '🐾'}</div>`;
    html += `<div><div class="sidebar-brand-text">${brand.name}</div>`;
    if (brandSub) html += `<div class="sidebar-brand-sub">${brandSub}</div>`;
    html += '</div></div>';
    html += '<nav class="sidebar-nav">';
    navGroups.forEach(group => {
      if (group.title) html += `<div class="nav-group-title">${group.title}</div>`;
      group.items.forEach(item => {
        const badge = item.badge ? `<span class="nav-item-badge">${item.badge}</span>` : '';
        html += `<a class="nav-item ${activeId === item.id ? 'active' : ''}" data-nav="${item.id}" href="javascript:void(0)">`;
        html += `<span class="nav-item-icon">${item.icon}</span><span class="nav-item-label">${item.label}</span>${badge}</a>`;
      });
    });
    html += '</nav>';
    html += '<div class="sidebar-footer"><div class="sidebar-user">';
    html += `<div class="sidebar-user-avatar">${user.avatar || '管'}</div>`;
    html += `<div class="sidebar-user-info"><div class="sidebar-user-name">${user.name}</div><div class="sidebar-user-role">${user.role}</div></div>`;
    html += '</div></div></aside>';
    html += '<div class="sidebar-overlay" id="sidebarOverlay"></div>';
    return html;
  },

  // === 绑定侧边栏导航 ===
  bindSidebar(onNavigate) {
    document.querySelectorAll('.nav-item[data-nav]').forEach(item => {
      item.addEventListener('click', () => {
        const navId = item.dataset.nav;
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        item.classList.add('active');
        // 切换页面
        document.querySelectorAll('.page-view').forEach(p => p.classList.remove('active'));
        const page = document.getElementById('page-' + navId);
        if (page) page.classList.add('active');
        if (onNavigate) onNavigate(navId);
        // 移动端关闭侧边栏
        if (window.innerWidth <= 768) {
          document.getElementById('tnrSidebar').classList.remove('show');
          document.getElementById('sidebarOverlay').classList.remove('show');
        }
      });
    });

    // 移动端侧边栏开关
    const toggle = document.getElementById('sidebarToggle');
    const sidebar = document.getElementById('tnrSidebar');
    const overlay = document.getElementById('sidebarOverlay');
    if (toggle) {
      toggle.addEventListener('click', () => {
        sidebar.classList.toggle('show');
        overlay.classList.toggle('show');
      });
    }
    if (overlay) {
      overlay.addEventListener('click', () => {
        sidebar.classList.remove('show');
        overlay.classList.remove('show');
      });
    }
  },

  // === 渲染搜索筛选栏（自动附带标准「搜索/重置」按钮，配合 bindFilter 生效） ===

  /* 所有列表筛选条统一追加的两个「起止时间」条件。
   *
   * key 固定为 `start_date` / `end_date` —— 与后端 `parse_date_param()` 的参数名
   * 同源（政府端台账 `/api/supervision/ledger/` 就是直接透传这两个 key 给服务端）。
   *
   * ⚠ 抽成常量、由 `renderFilterBar` 统一追加，而不是在 31 个调用点各写一遍：
   * 逐处手写必然漏，而漏掉的表现是「这一页没有起止时间」—— **不报错、只是少个控件**，
   * 正是本项目最防的那种静默缺陷。
   */
  DATE_RANGE_FILTERS: [
    { key: 'start_date', label: '开始日期', type: 'date' },
    { key: 'end_date', label: '结束日期', type: 'date' }
  ],

  renderFilterBar(filters, actions = '', opts = {}) {
    /* 起止时间**默认自动追加**（要求：每个列表的搜索条件都要有）。
     *
     * 幂等：筛选条自己已经写了 `start_date` 的（医院端交接/诊疗/物料/安乐死/台账、
     * 捕捉端转运明细/采购/出库/回收、政府端物料流水/台账中心）**不重复追加** ——
     * 否则同一页会出现两组同名 `data-filter`，`getFilterValues` 后者覆盖前者，
     * 用户在上面那组输入的值会被下面那组**静默抹掉**。
     *
     * 显式关掉传 `{ noDateRange: true }`（目前只有「实体本身没有任何时间字段」才需要）。
     *
     * `opts.id`：把 id **直接写在 `.filter-bar` 上**。
     * ⚠⚠ 千万不要在调用处再套一层 `<div id="xxxFilterBar">` 把返回值包起来
     * （本项目曾在 18 处这么写）。那一层与 `.filter-bar` **同高**，
     * 于是 `position: sticky` 的包含块就是筛选条自己 —— **一点可移动空间都没有**，
     * 筛选条根本不冻结、跟着内容一起滚走；而表头是钉住的，视觉上就是
     * 「冻结的筛选条被表头撞飞」。用 `opts.id` 让筛选条自己带 id，
     * `getFilterValues(bar)` / `bindFilter(fn, bar)` 照旧按 id 取，零改动。
     */
    const list = Array.isArray(filters) ? filters : [];
    const hasDateRange = list.some(f => f && f.key === 'start_date');
    const all = (opts.noDateRange || hasDateRange)
      ? list : list.concat(this.DATE_RANGE_FILTERS);

    const itemHtml = (f) => {
      let h = '<div class="filter-item">';
      h += `<div class="filter-item-label">${f.label}</div>`;
      if (f.type === 'select') {
        h += `<select class="form-select" data-filter="${f.key}">`;
        h += '<option value="">全部</option>';
        f.options.forEach(opt => {
          h += `<option value="${opt.value}">${opt.label}</option>`;
        });
        h += '</select>';
      } else if (f.type === 'search') {
        h += `<div class="search-input"><span class="search-input-icon">🔍</span><input type="text" class="form-input" data-filter="${f.key}" placeholder="${f.placeholder || '搜索...'}"></div>`;
      } else {
        h += `<input type="${f.type || 'text'}" class="form-input" data-filter="${f.key}" placeholder="${f.placeholder || ''}">`;
      }
      return h + '</div>';
    };
    let html = `<div class="filter-bar"${opts.id ? ` id="${this.escape(opts.id)}"` : ''}>`;
    /* 起止日期成对包一层：手机端两列并排、桌面端仍在同一行。
       分开做两个整行控件时，仅日期就占掉 ~170px 屏高，列表首屏被筛选条挤没。 */
    for (let i = 0; i < all.length; i++) {
      const f = all[i];
      if (f && f.type === 'date' && all[i + 1] && all[i + 1].type === 'date') {
        html += '<div class="filter-date-pair">' + itemHtml(f) + itemHtml(all[i + 1]) + '</div>';
        i += 1;
      } else if (f) {
        html += itemHtml(f);
      }
    }
    // 搜索/重置紧跟筛选条件且样式一致；自定义操作（新增/导出等）单独分组、样式区分
    const stdButtons = opts.noButtons ? '' :
      `<button type="button" class="btn btn-secondary btn-sm" data-fb="search">搜索</button>` +
      `<button type="button" class="btn btn-secondary btn-sm" data-fb="reset">重置</button>`;
    if (actions || stdButtons) {
      html += `<div class="filter-actions">${stdButtons}${actions ? `<span class="filter-actions-extra">${actions}</span>` : ''}</div>`;
    }
    html += '</div>';
    return html;
  },

  // === 实时搜索过滤（输入防抖 200ms；回车立即提交） ===
  /* scope 可选：限定到某个容器。
     不传时退回 document，保持与既有调用兼容。
     传容器可以避免「A 页面的筛选条件被 B 页面的表格读到」——各 portal 的
     .page-view 都常驻 DOM，全局查询会把所有页面的筛选值混在一起。 */
  bindFilter(tableRender, scope) {
    const root = this._filterScope(scope);
    // 标准「搜索 / 重置」按钮
    root.querySelectorAll('[data-fb="search"]').forEach(btn => {
      const fresh = btn.cloneNode(true);
      btn.parentNode.replaceChild(fresh, btn);
      fresh.addEventListener('click', (e) => { e.preventDefault(); tableRender(); });
    });
    root.querySelectorAll('[data-fb="reset"]').forEach(btn => {
      const fresh = btn.cloneNode(true);
      btn.parentNode.replaceChild(fresh, btn);
      fresh.addEventListener('click', (e) => {
        e.preventDefault();
        root.querySelectorAll('[data-filter]').forEach(i => { i.value = ''; });
        tableRender();
      });
    });
    root.querySelectorAll('[data-filter]').forEach(input => {
      let timer = null;
      const isText = (input.type || '') === 'text' || input.tagName === 'INPUT' && input.type !== 'checkbox';
      input.addEventListener('input', () => {
        if (!isText) { tableRender(); return; }
        clearTimeout(timer);
        timer = setTimeout(tableRender, 200);
      });
      input.addEventListener('change', () => {
        clearTimeout(timer);
        tableRender();
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          clearTimeout(timer);
          tableRender();
        }
      });
    });
  },

  getFilterValues(scope) {
    const root = this._filterScope(scope);
    const vals = {};
    root.querySelectorAll('[data-filter]').forEach(input => {
      vals[input.dataset.filter] = input.value.trim().toLowerCase();
    });
    return vals;
  },

  // ============================================
  // 门户壳层框架：侧栏折叠（桌面）/ 抽屉自动隐藏（移动）
  // ============================================
  /**
   * 对 portal_*_base 布局启用标准框架行为。
   * - 桌面（>900px）：顶栏 ☰ 切换「图标窄栏」模式，状态记忆 localStorage；
 *   窄栏下悬停自动展开预览，导航项以 title 提示。
   * - 移动（≤900px）：侧栏默认隐藏（抽屉），☰ 呼出，点遮罩/导航后自动收起；
   *   窗口尺寸切换时自动复位。
   */
  initPortalShell() {
    const sidebar = document.getElementById('portalSidebar');
    const overlay = document.getElementById('portalSidebarOverlay');
    const toggle = document.getElementById('portalSidebarToggle');
    if (!sidebar) return;

    const isMobile = () => window.matchMedia('(max-width: 768px)').matches;
    const mq = window.matchMedia('(max-width: 768px)');

    // 恢复桌面折叠状态
    if (!isMobile() && this._pref('sideCollapsed') === true) {
      document.body.classList.add('tnr-side-collapsed');
    }

    const setMobile = (open) => {
      sidebar.classList.toggle('show', open);
      if (overlay) overlay.classList.toggle('show', open);
    };

    if (toggle) {
      // 替换 base 内联脚本可能已绑定的行为：先克隆去监听
      const fresh = toggle.cloneNode(true);
      toggle.parentNode.replaceChild(fresh, toggle);
      fresh.addEventListener('click', () => {
        if (isMobile()) {
          setMobile(!sidebar.classList.contains('show'));
        } else {
          const collapsed = document.body.classList.toggle('tnr-side-collapsed');
          this._pref('sideCollapsed', collapsed);
        }
      });
    }
    if (overlay) overlay.addEventListener('click', () => setMobile(false));

    // 导航点击后收起移动端抽屉
    sidebar.querySelectorAll('.nav-item').forEach(a => {
      a.addEventListener('click', () => { if (isMobile()) setMobile(false); });
    });

    // 折叠窄栏时给导航项补 title 提示
    const applyTips = () => {
      sidebar.querySelectorAll('.nav-item').forEach(a => {
        const label = a.querySelector('.nav-item-label');
        a.title = document.body.classList.contains('tnr-side-collapsed') && !isMobile()
          ? (label ? label.textContent.trim() : '') : '';
      });
      const icon = sidebar.querySelector('.sidebar-collapse-btn .collapse-icon');
      if (icon) icon.textContent = (document.body.classList.contains('tnr-side-collapsed') && !isMobile()) ? '»' : '«';
    };
    applyTips();
    new MutationObserver(applyTips).observe(document.body, { attributes: true, attributeFilter: ['class'] });

    // === 侧栏底部收起/展开按钮（桌面折叠窄栏 / 移动端收起抽屉） ===
    const userFooter = sidebar.querySelector('.portal-sidebar-user, .sidebar-user');
    const collapseBtn = document.createElement('button');
    collapseBtn.type = 'button';
    collapseBtn.className = 'sidebar-collapse-btn';
    collapseBtn.title = '收起/展开导航栏';
    collapseBtn.innerHTML = '<span class="collapse-icon">«</span>';
    if (userFooter) userFooter.parentNode.insertBefore(collapseBtn, userFooter);
    else sidebar.appendChild(collapseBtn);
    collapseBtn.addEventListener('click', () => {
      if (isMobile()) { setMobile(false); return; }
      const collapsed = document.body.classList.toggle('tnr-side-collapsed');
      this._pref('sideCollapsed', collapsed);
    });

    // 头部锁定偏移由固定高度的 CSS 规则实现（tabs 46px / filter 54px），无需动态测量

    // 跨断点切换时复位
    mq.addEventListener('change', () => {
      setMobile(false);
      document.body.classList.remove('tnr-side-collapsed');
      if (!mq.matches) document.body.classList.remove('tnr-side-collapsed');
      applyTips();
    });
  },

  // === 标准页头 ===
  pageHeader({ title, desc = '', actions = '' }) {
    return `<div class="std-page-head"><div><div class="std-page-title">${title}</div>${desc ? `<div class="std-page-desc">${desc}</div>` : ''}</div>${actions ? `<div class="std-page-actions">${actions}</div>` : ''}</div>`;
  },

  // === 创建空HTML骨架 ===
  createPageStructure(sidebarHTML, topbarTitle, contentHTML) {
    return `
      <div class="admin-layout">
        ${sidebarHTML}
        <div class="admin-main">
          <div class="topbar">
            <div class="topbar-left">
              <button class="topbar-toggle" id="sidebarToggle">☰</button>
              <div class="topbar-title">${topbarTitle}</div>
            </div>
            <div class="topbar-right">
              <div class="topbar-time" id="topbarTime"></div>
            </div>
          </div>
          <div class="admin-content">
            ${contentHTML}
          </div>
        </div>
      </div>
    `;
  },

  // === 启动时钟 ===
  startClock() {
    const update = () => {
      const el = document.getElementById('topbarTime');
      if (el) {
        const now = new Date();
        el.textContent = now.getFullYear() + '-' +
          String(now.getMonth() + 1).padStart(2, '0') + '-' +
          String(now.getDate()).padStart(2, '0') + ' ' +
          String(now.getHours()).padStart(2, '0') + ':' +
          String(now.getMinutes()).padStart(2, '0') + ':' +
          String(now.getSeconds()).padStart(2, '0');
      }
    };
    update();
    setInterval(update, 1000);
  },

  // === HTML 转义 ===
  escape(str) {
    if (str == null) return '';
    return String(str).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  },

  /* 上传前压缩图片（手机原相机照片必过这一关）。

   * 为什么必须在**客户端**压：手机 `capture="environment"` 拍出的原图常见
   * 3~5MB，一张「新增捕捉登记」可以带 1 张合影 + 最多 100 张单只照片。
   * 不压的话有两道坎，而且**都表现为「点了提交没反应」**：
   *   1. 服务端 `DATA_UPLOAD_MAX_MEMORY_SIZE`（Django 默认 2.5MB）——
   *      已在 `parse_json_body` 侧放过 multipart，但请求体仍被整体读入内存；
   *   2. nginx `client_max_body_size`（本部署 20M）—— 超了直接 413，
   *      而 413 返回的是 HTML，`res.json()` 会**抛错**，提交处理器里没人接。
   * 移动网络下传 5MB 也要好几秒，用户会以为卡死。
   *
   * 做法：等比缩到长边 `maxEdge`，再编码为 JPEG。
   *
   * ⚠ 手机竖拍靠 **EXIF Orientation** 记录方向，canvas 绘制不会自动应用 ——
   *   不处理就会出现「照片躺倒」。这里用 `<img>` 承载，现代浏览器的
   *   `image-orientation` 初始值就是 `from-image`，绘制到 canvas 时会按
   *   EXIF 摆正；再显式设一次以防个别实现默认值不同。
   *
   * ⚠ 任何一步失败都**原样返回原文件** —— 宁可传一张大图，也不能因为
   *   压缩失败让用户传不上去。
   *
   * @param {File} file 用户选择的原始文件
   * @param {{maxEdge?: number, quality?: number}} [opts]
   * @returns {Promise<File>} 压缩后的 File（失败/无需压缩时为原文件）
   */
  async compressImage(file, opts = {}) {
    const maxEdge = opts.maxEdge || 1600;
    const quality = opts.quality || 0.82;
    if (!file || !/^image\//.test(file.type || '')) return file;
    // GIF 走 canvas 会只剩第一帧；SVG 不是位图。都原样返回。
    if (file.type === 'image/gif' || file.type === 'image/svg+xml') return file;
    try {
      const img = await new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const el = new Image();
        el.style.imageOrientation = 'from-image';
        el.onload = () => { URL.revokeObjectURL(url); resolve(el); };
        el.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片解码失败')); };
        el.src = url;
      });
      const srcW = img.naturalWidth || img.width;
      const srcH = img.naturalHeight || img.height;
      if (!srcW || !srcH) return file;
      const scale = Math.min(1, maxEdge / Math.max(srcW, srcH));
      // 尺寸已经够小且体积不大：不值得再编码一次（重编码反而可能更糊）
      if (scale >= 1 && file.size <= 512 * 1024) return file;
      const w = Math.max(1, Math.round(srcW * scale));
      const h = Math.max(1, Math.round(srcH * scale));
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      canvas.getContext('2d').drawImage(img, 0, 0, w, h);
      const blob = await new Promise((resolve) =>
        canvas.toBlob(resolve, 'image/jpeg', quality));
      if (!blob) return file;
      // 压完反而更大（本来就是高压缩比的小图）→ 用原文件
      if (blob.size >= file.size) return file;
      const name = (file.name || 'photo').replace(/\.[^.]+$/, '') + '.jpg';
      return new File([blob], name, { type: 'image/jpeg' });
    } catch (e) {
      return file;
    }
  },

  /* 照片放大查看：点击图片弹出全屏遮罩，点击遮罩关闭。
     各端共用（一宠一档档案里的捕捉/术前/术后/诊疗照片都走它）。 */
  photoZoom(url) {
    if (!url) return;
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9999;display:flex;align-items:center;justify-content:center;cursor:zoom-out;';
    overlay.innerHTML = '<img src="' + url + '" style="max-width:92vw;max-height:92vh;object-fit:contain;border-radius:8px;">';
    overlay.addEventListener('click', () => overlay.remove());
    document.body.appendChild(overlay);
  },

  // ============================================
  // 事件委托：行内按钮 / 图片点击的统一入口
  // ============================================
  /* mountTable 在「点表头排序 / 翻页 / 改每页条数」时会整体重建 tbody
     （内部再次调用 mountTable → el.innerHTML = html），此前直接绑在行元素上的
     监听器随旧节点一起被丢弃 —— 表现为「点单号/编号/按钮没反应，且控制台不报错」。
     因此行内可点元素一律「绑在稳定容器上 + 事件委托」，容器不随重渲染消失。
     @param {string|Element} container 稳定容器（通常是 mountTable 的 target）
     @param {Object} handlers { 'data-act': (value, el, e) => {} }，key 为属性名
     @param {string} [tag] 同一容器挂多组委托时用于区分（省略则按属性名组合）
     @returns {Element|null} 解析后的容器 */
  delegateClick(container, handlers, tag) {
    const el = this._resolveEl(container);
    if (!el || typeof el.addEventListener !== 'function') return null;
    const keys = Object.keys(handlers || {});
    if (!keys.length) return el;
    const key = tag || keys.join('|');
    let tags = this._delegatedTags.get(el);
    if (!tags) { tags = new Set(); this._delegatedTags.set(el, tags); }
    if (tags.has(key)) return el;   // 同一容器同一组委托只绑一次
    tags.add(key);
    el.addEventListener('click', (e) => {
      const t = e.target;
      if (!t || typeof t.closest !== 'function') return;
      for (const attr of keys) {
        const node = t.closest('[' + attr + ']');
        if (node && el.contains(node)) {
          // 命中即消费：不再匹配本组的其它属性，并中断冒泡，
          // 避免外层容器上的委托对同一次点击再触发一遍动作。
          e.stopPropagation();
          handlers[attr](node.getAttribute(attr), node, e);
          return;
        }
      }
    });
    return el;
  },

  /* 给容器内所有 [data-zoom] 图片绑定「点击放大」。
     走事件委托，因此重渲染出来的新图片无需重新绑定。 */
  bindPhotoZoom(container) {
    return this.delegateClick(container || document, {
      'data-zoom': (url) => this.photoZoom(url)
    }, 'PhotoZoom');
  },

  /* ============ 单据打印（第四十六轮：全局单据化） ============
   *
   * 「打印单据」按钮 → 新窗口打开该单据的 A4 打印页
   * （`/print/<doc>/<pk>/`，页面自带「打印 / 另存 PDF」）。
   *
   * ⚠ 打印页**不在 `/api/` 下** —— 它返回整页 HTML，而 `/api/` 的契约是
   * JSON 信封（`core/tests.py::AllApiRoutesContractTest` 会枚举每条 `/api/`
   * 路由并断言 `Content-Type: application/json`）。见 `tnr_system/urls.py`。
   *
   * `doc` 取值必须与后端 `business/views_print.py::DOC_SPECS` 的键一致：
   * capture / transfer / owner_return / release / adoption /
   * euthanasia / treatment / material。
   * 传错键后端会回 404 页面（`print/doc_missing.html`），不会静默失败。
   */
  printDocBtn(doc, id, label) {
    if (!doc || id === null || id === undefined || id === '') return '';
    this._bindPrintDoc();
    /* URL 用**模板字符串**写（与 `tnr-api.js` 一致），不要用 `'a' + x + 'b'` 拼接：
       `core/tests_frontend_consistency.py::ApiRouteCoverageTest` 会把两种写法都归一化成
       「路径 + 标记符」，但拼接式在归一化后**只在第一个动态段处截断**，前缀是否被判为
       合法路由全靠巧合。
       ⚠ 路径**不在 `/api/` 下**：打印页是整页 HTML，而 `/api/` 的契约是 JSON 信封
       （详见 `tnr_system/urls.py` 里 `print_doc` 那一段的注释）。 */
    const url = `/print/${doc}/${id}/`;
    return '<div class="print-doc-row">'
      + '<button class="btn btn-primary btn-sm" data-print-doc="' + url + '">🖨 '
      + this.escape(label || '打印单据') + '</button></div>';
  },

  /* 抽屉是 append 到 `document.body` 的，容器级委托接不到它，
     所以打印委托必须绑在 **document** 上，且只绑一次（防重复弹窗）。 */
  _bindPrintDoc() {
    if (this.__printDocBound) return;
    this.__printDocBound = true;
    document.addEventListener('click', (e) => {
      const btn = (e.target && e.target.closest)
        ? e.target.closest('[data-print-doc]') : null;
      if (!btn) return;
      e.stopPropagation();   // 别让外层委托把同一次点击再当成别的动作
      window.open(btn.getAttribute('data-print-doc'), '_blank', 'noopener');
    });
  }
};
