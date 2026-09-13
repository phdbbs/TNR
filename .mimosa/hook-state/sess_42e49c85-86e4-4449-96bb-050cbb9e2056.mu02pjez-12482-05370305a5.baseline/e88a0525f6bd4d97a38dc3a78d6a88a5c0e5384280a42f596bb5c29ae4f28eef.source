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
      overlay.querySelector('[data-action="cancel"]').onclick = () => { overlay.remove(); resolve(false); };
      overlay.querySelector('[data-action="ok"]').onclick = () => { overlay.remove(); resolve(true); };
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

    const closeFn = () => overlay.remove();
    overlay.querySelector('[data-action="close"]').onclick = closeFn;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeFn(); });

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
    overlay.style.zIndex = '1500';
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

    const closeFn = () => { overlay.remove(); drawerEl.remove(); };
    drawerEl.querySelector('[data-action="close"]').onclick = closeFn;
    overlay.onclick = closeFn;

    const bodyEl = drawerEl.querySelector('.drawer-body');
    if (typeof body === 'string') bodyEl.innerHTML = body;
    else if (typeof body === 'function') body(bodyEl, closeFn);

    return { drawerEl, close: closeFn };
  },

  // === 渲染表格 ===
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

  // === 渲染搜索筛选栏 ===
  renderFilterBar(filters, actions = '') {
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
    if (actions) {
      html += `<div class="filter-actions">${actions}</div>`;
    }
    html += '</div>';
    return html;
  },

  // === 实时搜索过滤 ===
  bindFilter(tableRender) {
    document.querySelectorAll('[data-filter]').forEach(input => {
      input.addEventListener('input', () => tableRender());
      input.addEventListener('change', () => tableRender());
    });
  },

  getFilterValues() {
    const vals = {};
    document.querySelectorAll('[data-filter]').forEach(input => {
      vals[input.dataset.filter] = input.value.trim().toLowerCase();
    });
    return vals;
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
  }
};
