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
  async _get(url) {
    const res = await fetch(url);
    const data = await res.json();
    return data.success ? data.data : (Array.isArray(data.data) ? data.data : []);
  },
  async _post(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': this._getCSRF()},
      body: JSON.stringify(body)
    });
    return res.json();
  },
  async _postForm(url, formData) {
    const res = await fetch(url, {
      method: 'POST',
      headers: {'X-CSRFToken': this._getCSRF()},
      body: formData
    });
    return res.json();
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
  async resendTransfer(id) { return this._post(`/api/business/transfers/${id}/resend/`, {}); },
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
  async registerAdoption(data) { return this._post('/api/business/adoptions/register/', data); },
  async confirmAdoptionClaim(id, data) { return this._post(`/api/business/adoptions/${id}/confirm-claim/`, data || {}); },
  async getCheckins() { return this._get('/api/business/checkins/'); },
  async reviewCheckin(id, data) { return this._post(`/api/business/checkins/${id}/review/`, data); },
  async getBlacklist() { return this._get('/api/business/blacklist/'); },
  async createBlacklist(data) { return this._post('/api/business/blacklist/create/', data); },
  async checkBlacklist(idCard, phone) {
    const params = new URLSearchParams();
    if (idCard) params.set('id_card', idCard);
    if (phone) params.set('phone', phone);
    return this._get(`/api/business/blacklist/check/?${params}`);
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
  async getLedger(filters) {
    const params = new URLSearchParams();
    if (filters) {
      Object.keys(filters).forEach(k => {
        if (filters[k] !== undefined && filters[k] !== null && filters[k] !== '') params.append(k, filters[k]);
      });
    }
    const qs = params.toString();
    return this._get('/api/supervision/ledger/' + (qs ? '?' + qs : ''));
  },
  async getOperationLogs(limit) {
    const url = limit ? `/api/supervision/logs/?limit=${limit}` : '/api/supervision/logs/';
    return this._get(url);
  },
  async getSystemConfig() { return this._get('/api/supervision/config/'); },
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
  getPetStatusText(status) {
    const map = {'in_transit':'在途','in_treatment':'待诊疗/诊疗中','pending_adopt':'待领养','pending_claim':'待领出','adopted':'已领养','released':'已放养','euthanized':'已安乐死','owner_returned':'主人领回'};
    return map[status] || status;
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
