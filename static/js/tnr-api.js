const TNR_API = {
  BASE: '/api/business/',
  _user: null,
  KEYS: {
    pets: 'adoptions/hall/',
    adoptions: 'portal/adoptions/',
    checkIns: 'checkins/',
    messages: 'portal/messages/',
    institutions: 'institutions/',
    captures: 'captures/',
    transfers: 'transfers/',
    treatments: 'treatments/',
    users: 'users/',
  },
  getCurrentUser() {
    if (this._user) return this._user;
    if (window.TNR_USER) { this._user = window.TNR_USER; return this._user; }
    return null;
  },
  getDistrictFilter() {
    const u = this.getCurrentUser();
    if (!u || u.role === 'gov_city') return null;
    return u.district_id;
  },
  /* 静默封装：任何非 2xx（含 403 / 500）都返回 `[]`，调用方拿不到失败信号。
   *
   * 列表页用它是可以的（空表格 ≈ 没有数据）。但**凡是「值必须如实显示」的
   * 场景都不能用它** —— 403 会变成「配置为空」、500 会变成「暂无数据」，
   * 用户看到的是错误结论而不是错误提示。这类场景一律用 `get()`（失败抛错）。
   * 第十九轮政府端「系统配置」页就是栽在这里：区级拿 403 后前缀全渲染成空串。
   *
   * ⚠ **401 是唯一的例外**（第三十七轮）：它不代表「这条数据取不到」，
   * 而代表**整个会话已经失效**。此时返回 `[]` 会让界面上每一个列表都变成
   * 空表、每一个统计都变成 0，用户看到「系统里什么都没有了」而不是
   * 「请重新登录」。所以 401 一律交给 `_handleUnauthorized()` 统一处理。
   */
  async _get(url) {
    const res = await fetch(url, { credentials: 'same-origin' });
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (res.status === 401) { this._handleUnauthorized(data.message); return []; }
    return data.success ? data.data : (Array.isArray(data.data) ? data.data : []);
  },
  /* === 会话失效（401）统一处理：**先提示原因，再跳登录页** ===
   *
   * 为什么必须「先提示」：直接跳转的话，用户只看到自己回到了登录页，
   * **无法判断**是会话过期、还是自己点错了、还是服务端出问题 —— 也就不知道
   * 该不该重新登录。提示文案直接用**服务端返回的 message**
   * （`accounts.decorators.api_unauthorized` 固定返回「请先登录」），
   * 而不是前端另编一句，保证前后端口径同源。
   *
   * ⚠⚠ **必须防重入**。门户首屏会**并发**发出多个请求（5~9 个），会话过期时
   * 它们**同时**拿到 401。不设闸门就会连弹 N 个提示、触发 N 次跳转，
   * 而 `next` 参数还可能互相覆盖成最后一个请求的地址。
   * 用 `_redirecting` 保证「只提示一次、只跳一次」。
   */
  _redirecting: false,
  _handleUnauthorized(message) {
    if (this._redirecting) return;
    this._redirecting = true;
    const msg = message || '登录已过期，请重新登录';
    const go = () => {
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = '/login/?next=' + next;
    };
    if (window.TNR_UI && typeof window.TNR_UI.toast === 'function') {
      // 留 1.8 秒让用户看清原因，再跳 —— 太快等于没提示
      window.TNR_UI.toast(msg + '，正在跳转登录页…', 'warning', 1800);
      setTimeout(go, 1800);
    } else {
      alert(msg);
      go();
    }
  },
  async _post(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': this._getCSRF()},
      body: JSON.stringify(body)
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      this._handleUnauthorized(data.message);
      return {success: false, message: data.message || '请先登录'};
    }
    return data;
  },
  async _postForm(url, formData) {
    const res = await fetch(url, {
      method: 'POST',
      headers: {'X-CSRFToken': this._getCSRF()},
      body: formData
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      this._handleUnauthorized(data.message);
      return {success: false, message: data.message || '请先登录'};
    }
    return data;
  },
  _getCSRF() {
    return document.querySelector('[name=csrfmiddlewaretoken]')?.value ||
           document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
  },
  // === 通用泛型方法（按 KEYS 映射，返回完整 JSON 响应） ===
  async get(url, params) {
    let qs = '';
    if (params) {
      const sp = new URLSearchParams();
      Object.keys(params).forEach(k => {
        if (params[k] !== undefined && params[k] !== null && params[k] !== '') sp.append(k, params[k]);
      });
      qs = '?' + sp.toString();
    }
    const fullUrl = url.startsWith('http') || url.startsWith('/') ? url : this.BASE + url;
    const res = await fetch(fullUrl + qs, { credentials: 'same-origin', headers: { 'X-CSRFToken': this._getCSRF() } });
    return this._handle(res);
  },
  async post(url, data) {
    const fullUrl = url.startsWith('http') || url.startsWith('/') ? url : this.BASE + url;
    const isForm = data instanceof FormData;
    const headers = { 'X-CSRFToken': this._getCSRF() };
    if (!isForm) headers['Content-Type'] = 'application/json';
    const res = await fetch(fullUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers,
      body: isForm ? data : JSON.stringify(data || {}),
    });
    return this._handle(res);
  },
  async _handle(res) {
    const text = await res.text();
    let json;
    try { json = text ? JSON.parse(text) : {}; }
    catch (e) { throw new Error('服务器返回格式错误'); }
    // 401 先于「通用失败」判断 —— 它需要触发跳转，不只是抛个错
    if (res.status === 401) {
      this._handleUnauthorized(json.message);
      throw new Error(json.message || '请先登录');
    }
    if (!res.ok || json.success === false) {
      throw new Error(json.message || '操作失败 (' + res.status + ')');
    }
    return json;
  },
  async getAllRaw(key, params) {
    const endpoint = this.KEYS[key] || key + '/';
    const json = await this.get(this.BASE + endpoint, params);
    return json.data || [];
  },
  async getById(key, id) {
    const endpoint = this.KEYS[key] || key + '/';
    const json = await this.get(this.BASE + endpoint + id + '/');
    return json.data || null;
  },
  async create(key, data) {
    const endpoint = this.KEYS[key] || key + '/';
    const json = await this.post(this.BASE + endpoint, data);
    return json.data;
  },
  async update(key, id, data) {
    const endpoint = this.KEYS[key] || key + '/';
    const json = await this.post(this.BASE + endpoint + id + '/', data);
    return json.data;
  },
  async getCaptures() { return this._get('/api/business/captures/'); },
  async getCapture(id) { return this._get(`/api/business/captures/${id}/`); },
  async createCapture(data) { return this._post('/api/business/captures/create/', data); },
  async ownerReturn(captureId, data) { return this._post(`/api/business/captures/${captureId}/owner-return/`, data); },
  async getOwnerReturns() { return this._get('/api/business/owner-returns/'); },
  async getTransfers() { return this._get('/api/business/transfers/'); },
  async createTransfer(data) { return this._post('/api/business/transfers/create/', data); },
  async receiveTransfer(id) { return this._post(`/api/business/transfers/${id}/receive/`, {}); },
  async rejectTransfer(id, reason) { return this._post(`/api/business/transfers/${id}/reject/`, {reason}); },
  async withdrawTransfer(id) { return this._post(`/api/business/transfers/${id}/withdraw/`, {}); },
  // 注：resendTransfer（重新下发）已移除 —— 被驳回的动物会自动回到「待转运」备选框，
  // 由操作员重新勾选后下发新单；旧的「重新下发」可对同一单反复下发，已废弃。
  async getTreatments() { return this._get('/api/business/treatments/'); },
  async createTreatment(data) { return this._post('/api/business/treatments/create/', data); },
  async getMaterials() { return this._get('/api/business/materials/'); },
  async purchaseMaterial(data) { return this._post('/api/business/materials/purchase/', data); },
  async dispatchMaterial(data) { return this._post('/api/business/materials/dispatch/', data); },
  async receiveMaterial(id) { return this._post(`/api/business/materials/${id}/receive/`, {}); },
  async adjustStock(data) { return this._post('/api/business/materials/adjustment/', data); },
  async getMaterialTransactions() { return this._get('/api/business/materials/transactions/'); },
  async getShelterLedger() { return this._get('/api/business/materials/shelter-ledger/'); },
  async getHospitalLedger() { return this._get('/api/business/materials/hospital-ledger/'); },
  async getReleases() { return this._get('/api/business/releases/'); },
  async createRelease(data) { return this._post('/api/business/releases/create/', data); },
  async confirmRelease(id, data) { return this._post(`/api/business/releases/${id}/confirm/`, data); },
  async getAdoptions() { return this._get('/api/business/adoptions/'); },
  /* 领养人在线提交的申请单（待审核）。与 Adoption（线下领养登记）是两张表：
     Adoption.status 只有 pending_claim/completed/cancelled，没有 pending，
     所以「待审核」必须看 AdoptionApplication。 */
  async getAdoptionApplications(status) {
    const qs = status ? `?status=${encodeURIComponent(status)}` : '';
    return this._get('/api/business/adoptions/applications/' + qs);
  },
  async registerAdoption(data) { return this._post('/api/business/adoptions/register/', data); },
  async confirmAdoptionClaim(id, data) { return this._post(`/api/business/adoptions/${id}/confirm-claim/`, data || {}); },
  async reclaimAdoption(id, data) { return this._post(`/api/business/adoptions/${id}/reclaim/`, data || {}); },
  async getCheckins() { return this._get('/api/business/checkins/'); },
  async reviewCheckin(id, data) { return this._post(`/api/business/checkins/${id}/review/`, data); },
  async getBlacklist() { return this._get('/api/business/blacklist/'); },
  async createBlacklist(data) { return this._post('/api/business/blacklist/create/', data); },
  async updateBlacklist(id, data) { return this._post(`/api/business/blacklist/${id}/update/`, data); },
  async deleteBlacklist(id) { return this._post(`/api/business/blacklist/${id}/delete/`, {}); },
  /* 黑名单校验**不能用 `_get`**（第二十二轮）。
   *
   * `_get` 把任何非 2xx 静默变成 `[]`，而三处调用点都写成
   * `if (bl && (bl.name || bl.idCard))` —— 于是 403 / 500 / 断网时
   * 一律走进 else 分支：主人领回**不弹黑名单警告**、线下登记**直接放行**、
   * 「黑名单查询拦截」还会明晃晃显示「✓ 通过：未在黑名单中」。
   * **界面说的和实际发生的不是一回事** —— 而且这是安全控制的前端预检。
   *
   * 改用 `get()`：失败**抛错**，调用方必须显式处理（提示「校验失败，请重试」），
   * 而不是把「没查成」渲染成「通过」。
   *
   * 服务端在 `owner_return` / `adoption_create` 里都会**硬拦**
   * （`return json_fail(...)`），所以这里不是绕过，但错误结论同样不能留。
   *
   * 返回**完整响应体** `{success, data, message}`，调用方取 `r.data`。
   */
  async checkBlacklist(idCard, phone) {
    const params = new URLSearchParams();
    if (idCard) params.set('id_card', idCard);
    if (phone) params.set('phone', phone);
    return this.get(`/api/business/blacklist/check/?${params}`);
  },
  async getEuthanasia() { return this._get('/api/business/euthanasia/'); },
  async createEuthanasia(data) { return this._post('/api/business/euthanasia/create/', data); },
  async receiveBody(id) { return this._post(`/api/business/euthanasia/${id}/body-receive/`, {}); },
  // === 医院端专用 ===
  async getHospitalPets(status) {
    const url = status ? `/api/business/pets/?status=${status}` : '/api/business/pets/';
    return this._get(url);
  },
  // getPets: getHospitalPets 的别名（捕捉点端也使用此接口获取在途宠物列表）
  async getPets(status) {
    const url = status ? `/api/business/pets/?status=${status}` : '/api/business/pets/';
    return this._get(url);
  },
  async getHallListings() { return this._get('/api/business/hall-listings/'); },
  // 一宠一档：动物档案台账（每只动物一行，含全生命周期聚合数据）
  async getPetArchive() { return this._get('/api/business/pets/archive/'); },
  async getPetLifecycle(petId) { return this._get(`/api/business/pets/${petId}/lifecycle/`); },
  async editAdoptionInfo(petId, data) { return this._post(`/api/business/adoptions/${petId}/edit-info/`, data); },
  async uploadPetPhoto(petId, photoField, file) {
    const fd = new FormData();
    fd.append(photoField, file);
    return this._postForm(`/api/business/adoptions/${petId}/edit-info/`, fd);
  },
  async getInstitutions(type) {
    const url = type ? `/api/supervision/institutions/?type=${type}` : '/api/supervision/institutions/';
    return this._get(url);
  },
  async getDistricts() { return this._get('/api/supervision/districts/'); },
  // === 政府监管端专用 ===
  async getDashboardStats() { return this._get('/api/supervision/dashboard/'); },
  async createInstitution(data) { return this._post('/api/supervision/institutions/create/', data); },
  async editInstitution(id, data) { return this._post(`/api/supervision/institutions/${id}/edit/`, data); },
  async toggleInstitution(id) { return this._post(`/api/supervision/institutions/${id}/toggle/`, {}); },
  async createDistrict(data) { return this._post('/api/supervision/districts/create/', data); },
  async editDistrict(id, data) { return this._post(`/api/supervision/districts/${id}/edit/`, data); },
  async toggleDistrict(id) { return this._post(`/api/supervision/districts/${id}/toggle/`, {}); },
  async deleteDistrict(id) { return this._post(`/api/supervision/districts/${id}/delete/`, {}); },
  async getUsers(role) {
    const url = role ? `/api/supervision/users/?role=${role}` : '/api/supervision/users/';
    return this._get(url);
  },
  async createUser(data) { return this._post('/api/supervision/users/create/', data); },
  async toggleUser(id) { return this._post(`/api/supervision/users/${id}/toggle/`, {}); },
  async getBusinessSupervision(type) {
    const url = type ? `/api/supervision/business/?business_type=${type}` : '/api/supervision/business/';
    return this._get(url);
  },
  async getMaterialSupervision() { return this._get('/api/supervision/materials/'); },
  /* === 严格读内核：失败**抛错**，成功返回 `data` ===
   *
   * 与 `_get()` 的唯一区别是「失败不静默」。`_get()` **丢掉响应信封**
   * （只返回 `data`），调用方拿到的 `[]` **无法区分**「没有数据」与
   * 「服务端拒绝」—— 凡是在 `try/catch` 里包着读接口、并在 catch 里渲染
   * 失败态的代码，那个失败态**覆盖不到 HTTP 错误**（第二十五轮实测 12 处）。
   *
   * 后端信封统一由 `json_ok` / `json_fail` 产出：
   *   `{success: true,  data, message}` / `{success: false, data, message}`
   * （全仓只有 2 处裸 `JsonResponse`，见 accounts/views.py，也都带 `success`）。
   * 所以 `!res.ok || !data.success` 就是完整的失败判据。
   *
   * 分工：
   *   * 需要「失败可见」的调用点 → `getXxxStrict()`（见下方严格读方法区）；
   *   * 需要「失败降级成空」的列表页 → 继续用 `getXxx()`，**默认语义未变**。
   */
  async getData(url) {
    const res = await fetch(url, { credentials: 'same-origin' });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      this._handleUnauthorized(data.message);
      throw new Error(data.message || '请先登录');
    }
    if (!res.ok || !data.success) {
      throw new Error(data.message || `加载失败（HTTP ${res.status}）`);
    }
    return data.data;
  },
  async getLedger(filters) {
    const params = new URLSearchParams();
    if (filters) {
      Object.keys(filters).forEach(k => {
        if (filters[k] !== undefined && filters[k] !== null && filters[k] !== '') params.append(k, filters[k]);
      });
    }
    const qs = params.toString();
    const url = '/api/supervision/ledger/' + (qs ? '?' + qs : '');
    // ⚠ 这里**不能**走 `_get()` —— 理由见 `getData()` 的说明。
    // 第二十四轮实测：`getLedger({institution_id:'abc'})` 返回 `{ok:true, count:0}`，
    // 而服务端其实是 400「机构必须是数字」—— 筛选条件被拒却渲染成「暂无台账数据」。
    // 台账接口的失败必须**抛出去**，由调用方决定怎么显示。
    return this.getData(url);
  },
  async getOperationLogs(limit) {
    const url = limit ? `/api/supervision/logs/?limit=${limit}` : '/api/supervision/logs/';
    return this._get(url);
  },
  // getSystemConfig 已移除（第十九轮）。
  // 它经 `_get()` 在非 2xx 时**静默返回 `[]`**，把 403 渲染成「配置为空」——
  // 政府端「系统配置」页因此把真实的 CAP/TRF/… 前缀显示成空白输入框。
  // 读配置请用 `TNR_API.get('/api/supervision/config/')`（失败会抛错）。
  async updateSystemConfig(data) { return this._post('/api/supervision/config/', data); },
  async generatePetCodes(count) {
    try {
      const res = await fetch('/api/business/captures/codes-preview/?count=' + count, { credentials: 'same-origin' });
      const json = await res.json();
      if (json.success && Array.isArray(json.data)) return json.data;
    } catch (e) { /* 接口不可用时回退到本地预览 */ }
    const d = new Date();
    const yearStr = String(d.getFullYear()).substr(-2) + String(d.getMonth()+1).padStart(2,'0') + String(d.getDate()).padStart(2,'0');
    return Array.from({length: count}, (_, i) => 'TNR' + yearStr + String(i+1).padStart(3,'0'));
  },
  /* === 严格读便捷方法（失败抛错） ===
   *
   * 与同名 `getXxx()` **一一对应**，只是把 `_get()` 换成 `getData()`：
   * 非 2xx 或 `success:false` 一律抛错，让调用方已经写好的 `catch` 真正生效。
   * 供「**已经写了 `try/catch` 且 catch 里呈现失败态**」的调用点使用（第二十五轮）。
   *
   * ⚠ 与软版本的**唯一**区别就是失败可见性，URL 与返回结构完全一致。
   * 新增接口时若要在「呈现失败」的调用点使用，必须同时加对应的 `XxxStrict`；
   * 否则界面又会退回「失败 = 空」。
   * `business/tests/test_silent_read_contract.py` 会静态检查这一点。
   */
  async getPetLifecycleStrict(petId) { return this.getData(`/api/business/pets/${petId}/lifecycle/`); },
  async getCapturesStrict() { return this.getData('/api/business/captures/'); },
  async getTreatmentsStrict() { return this.getData('/api/business/treatments/'); },
  async getReleasesStrict() { return this.getData('/api/business/releases/'); },
  async getAdoptionsStrict() { return this.getData('/api/business/adoptions/'); },
  async getEuthanasiaStrict() { return this.getData('/api/business/euthanasia/'); },
  async getTransfersStrict() { return this.getData('/api/business/transfers/'); },
  async getMaterialsStrict() { return this.getData('/api/business/materials/'); },
  async getMaterialTransactionsStrict() { return this.getData('/api/business/materials/transactions/'); },
  async getHallListingsStrict() { return this.getData('/api/business/hall-listings/'); },
  // getPetsStrict / getHospitalPetsStrict 是同一接口的两个别名，与软版本保持一致
  async getPetsStrict(status) {
    const url = status ? `/api/business/pets/?status=${status}` : '/api/business/pets/';
    return this.getData(url);
  },
  async getHospitalPetsStrict(status) {
    const url = status ? `/api/business/pets/?status=${status}` : '/api/business/pets/';
    return this.getData(url);
  },
  async getDistrictsStrict() { return this.getData('/api/supervision/districts/'); },
  async getInstitutionsStrict(type) {
    const url = type ? `/api/supervision/institutions/?type=${type}` : '/api/supervision/institutions/';
    return this.getData(url);
  },
  async getMaterialSupervisionStrict() { return this.getData('/api/supervision/materials/'); },
  async getOperationLogsStrict(limit) {
    const url = limit ? `/api/supervision/logs/?limit=${limit}` : '/api/supervision/logs/';
    return this.getData(url);
  },
  getPetStatusText(status) {
    const map = {'in_transit':'在途','in_treatment':'待诊疗/诊疗中','pending_adopt':'待领养','pending_claim':'待领出','adopted':'已领养','released':'已放养','euthanized':'已死亡','owner_returned':'主人领回'};
    return map[status] || status;
  },
  // 一宠一档：把动物的物种/品种/性别拼成一行可读文案（如「猫 · 中华田园犬 · 公」）。
  // 转运/回收/诊疗/领养/放养/死亡各环节共用，保证全流程展示口径一致。
  petAttrText(pet, sep) {
    if (!pet) return '—';
    const parts = [pet.species, pet.breed, pet.gender]
      .filter(v => v && String(v).trim());
    return parts.length ? parts.join(sep || ' · ') : '—';
  },
  getPetStatusBadge(status) {
    const map = {'in_transit':'badge-warning','in_treatment':'badge-info','pending_adopt':'badge-cinnabar','pending_claim':'badge-cinnabar','adopted':'badge-success','released':'badge-success','euthanized':'badge-danger','owner_returned':'badge-default'};
    return map[status] || 'badge-default';
  },
  getMaterialCategoryText(cat) {
    const map = {'vaccine':'疫苗','dewormer':'驱虫药','chip':'芯片'};
    return map[cat] || cat;
  }
};
window.TNR_DB = TNR_API;
window.TNR_API = TNR_API;
