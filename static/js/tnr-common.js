/* ============================================
   TNR 流浪动物管理系统 - 通用 UI 组件库
   ============================================ */

const TNR_UI = {

  // === Toast 通知 ===
  toast(message, type = 'success', duration = 3000) {
    let container = document.querySelector('.toast-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container';
      document.body.appendChild(container);
    }
    const icons = { success: '✓', warning: '⚠', danger: '✕', info: 'ℹ' };
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<span class="toast-icon">${icons[type] || '✓'}</span><span class="toast-text">${message}</span>`;
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
   * @param {string|Element} target 容器元素或 id
   * @param {object} opts { id, columns:[{key,title,render,width,sortable,align}], data, rowActions,
   *                        emptyText, title, pageSize, pageSizes, wrap:'card'|'plain', actions }
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
      html += '<div class="card dt-card"><div class="card-body" style="padding:0;">';
      if (opts.title) {
        html += `<div class="card-header"><div class="card-title"><span class="card-title-bar"></span>${esc(opts.title)}${total ? `（${total}）` : ''}</div>${opts.actions ? `<div class="std-card-actions">${opts.actions}</div>` : ''}</div>`;
      }
      html += '<div class="table-wrapper">';
    }

    if (total === 0) {
      html += `<div class="table-empty"><div class="table-empty-icon">📋</div><div class="table-empty-text">${esc(emptyText)}</div></div>`;
    } else {
      const fixed = widths ? ' table-layout:fixed;' : '';
      html += `<table class="data-table dt-table" style="${fixed}">`;
      html += '<colgroup>';
      opts.columns.forEach((col, i) => {
        const w = widths ? widths[i] : (col.width || null);
        html += `<col style="${w ? 'width:' + (typeof w === 'number' ? w + 'px' : w) + ';' : ''}">`;
      });
      if (actionsCol) html += `<col style="${widths && widths[opts.columns.length] ? 'width:' + widths[opts.columns.length] + 'px' : 'width:1px;'}">`;
      html += '</colgroup><thead><tr>';
      opts.columns.forEach((col, i) => {
        const sortable = col.sortable !== false && col.key;
        const sorted = st.sortKey === col.key;
        const arrow = sortable ? `<span class="dt-sort ${sorted ? 'on ' + (st.sortDir === 'desc' ? 'desc' : 'asc') : ''}">${sorted ? (st.sortDir === 'desc' ? '▼' : '▲') : '⇅'}</span>` : '';
        const align = col.align ? `text-align:${col.align};` : '';
        html += `<th class="${sortable ? 'dt-sortable' : ''}" data-dt-sort="${sortable ? esc(col.key) : ''}" style="${align}">${col.title}${arrow}`;
        if (col.resizable !== false) html += `<span class="dt-colresizer" data-dt-col="${i}"></span>`;
        html += '</th>';
      });
      if (actionsCol) html += `<th class="dt-actions-th">操作<span class="dt-colresizer" data-dt-col="${opts.columns.length}"></span></th>`;
      html += '</tr></thead><tbody>';
      pageRows.forEach((row, idx) => {
        html += '<tr>';
        opts.columns.forEach(col => {
          const val = typeof col.render === 'function' ? col.render(row, idx) : (row[col.key] ?? '');
          html += `<td>${val ?? ''}</td>`;
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

  // === 签名板 ===
  initSignaturePad(canvas) {
    const ctx = canvas.getContext('2d');
    let isDrawing = false;
    let lastX = 0, lastY = 0;

    const ratio = Math.max(window.devicePixelRatio || 1, 1);
    canvas.width = canvas.offsetWidth * ratio;
    canvas.height = 200 * ratio;
    ctx.scale(ratio, ratio);
    ctx.strokeStyle = '#1C1C1C';
    ctx.lineWidth = 2;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    const getPos = (e) => {
      const rect = canvas.getBoundingClientRect();
      const touch = e.touches && e.touches[0];
      return [
        (touch ? touch.clientX : e.clientX) - rect.left,
        (touch ? touch.clientY : e.clientY) - rect.top
      ];
    };

    const start = (e) => { isDrawing = true; [lastX, lastY] = getPos(e); };
    const draw = (e) => {
      if (!isDrawing) return;
      e.preventDefault();
      const [x, y] = getPos(e);
      ctx.beginPath();
      ctx.moveTo(lastX, lastY);
      ctx.lineTo(x, y);
      ctx.stroke();
      [lastX, lastY] = [x, y];
    };
    const stop = () => { isDrawing = false; };

    canvas.addEventListener('mousedown', start);
    canvas.addEventListener('mousemove', draw);
    canvas.addEventListener('mouseup', stop);
    canvas.addEventListener('mouseout', stop);
    canvas.addEventListener('touchstart', start);
    canvas.addEventListener('touchmove', draw);
    canvas.addEventListener('touchend', stop);

    return {
      clear() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
      },
      isEmpty() {
        const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        return data.every(val => val === 0);
      },
      getDataURL() {
        return canvas.toDataURL();
      }
    };
  },

  // === 格式化日期 ===
  formatDate(date) {
    if (!date) return '—';
    const d = new Date(date);
    if (isNaN(d)) return date;
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  },

  formatDateTime(date) {
    if (!date) return '—';
    const d = new Date(date);
    if (isNaN(d)) return date;
    return this.formatDate(date) + ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
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
  renderFilterBar(filters, actions = '', opts = {}) {
    let html = '<div class="filter-bar">';
    filters.forEach(f => {
      html += '<div class="filter-item">';
      html += `<div class="filter-item-label">${f.label}</div>`;
      if (f.type === 'select') {
        html += `<select class="form-select" data-filter="${f.key}">`;
        html += '<option value="">全部</option>';
        f.options.forEach(opt => {
          html += `<option value="${opt.value}">${opt.label}</option>`;
        });
        html += '</select>';
      } else if (f.type === 'search') {
        html += `<div class="search-input"><span class="search-input-icon">🔍</span><input type="text" class="form-input" data-filter="${f.key}" placeholder="${f.placeholder || '搜索...'}"></div>`;
      } else {
        html += `<input type="${f.type || 'text'}" class="form-input" data-filter="${f.key}" placeholder="${f.placeholder || ''}">`;
      }
      html += '</div>';
    });
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
    const root = this._resolveEl(scope) || document;
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
    const root = this._resolveEl(scope) || document;
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
  }
};
