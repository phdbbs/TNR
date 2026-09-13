/* ============================================
   TNR 流浪动物管理系统 - 数据层
   localStorage 持久化 · 区县数据隔离 · CRUD
   ============================================ */

const TNR_DB = {
  // === 存储键 ===
  KEYS: {
    districts: 'tnr_districts',
    institutions: 'tnr_institutions',
    users: 'tnr_users',
    pets: 'tnr_pets',
    captures: 'tnr_captures',
    ownerReturns: 'tnr_owner_returns',
    transfers: 'tnr_transfers',
    treatments: 'tnr_treatments',
    materials: 'tnr_materials',
    materialTxns: 'tnr_material_txns',
    releases: 'tnr_releases',
    adoptions: 'tnr_adoptions',
    checkIns: 'tnr_checkins',
    blacklist: 'tnr_blacklist',
    euthanasia: 'tnr_euthanasia',
    adoptionHall: 'tnr_adoption_hall',
    messages: 'tnr_messages',
    currentUser: 'tnr_current_user',
    initialized: 'tnr_initialized'
  },

  // === 当前会话 ===
  _currentUser: null,

  // === 初始化 ===
  init() {
    if (localStorage.getItem(this.KEYS.initialized)) return;
    this._seedDistricts();
    this._seedInstitutions();
    this._seedUsers();
    this._seedMaterials();
    this._seedPets();
    this._seedCaptures();
    this._seedTransfers();
    this._seedTreatments();
    this._seedMaterialTxns();
    this._seedReleases();
    this._seedAdoptions();
    this._seedCheckIns();
    this._seedBlacklist();
    this._seedEuthanasia();
    this._seedMessages();
    localStorage.setItem(this.KEYS.initialized, '1');
  },

  // === 种子数据：区县 ===
  _seedDistricts() {
    const districts = [
      { id: 'D001', name: '朝阳区', code: 'CY', status: 'active', createdAt: '2025-01-01' },
      { id: 'D002', name: '海淀区', code: 'HD', status: 'active', createdAt: '2025-01-01' },
      { id: 'D003', name: '西城区', code: 'XC', status: 'active', createdAt: '2025-01-01' },
      { id: 'D004', name: '东城区', code: 'DC', status: 'active', createdAt: '2025-01-01' }
    ];
    this._save(this.KEYS.districts, districts);
  },

  // === 种子数据：机构 ===
  _seedInstitutions() {
    const institutions = [
      // 捕捉点
      { id: 'I001', name: '朝阳区流浪动物捕捉点', type: 'shelter', districtId: 'D001', address: '朝阳区建国路88号', contact: '王主任', phone: '13800001001', status: 'active' },
      { id: 'I002', name: '海淀区流浪动物捕捉点', type: 'shelter', districtId: 'D002', address: '海淀区中关村大街15号', contact: '李主任', phone: '13800001002', status: 'active' },
      // 医院
      { id: 'I003', name: '爱心宠物医院', type: 'hospital', districtId: 'D001', address: '朝阳区三里屯路12号', contact: '赵医生', phone: '13800002001', status: 'active' },
      { id: 'I004', name: '瑞鹏宠物医院', type: 'hospital', districtId: 'D001', address: '朝阳区望京SOHO旁', contact: '钱医生', phone: '13800002002', status: 'active' },
      { id: 'I005', name: '芭比堂动物医院', type: 'hospital', districtId: 'D002', address: '海淀区五道口', contact: '孙医生', phone: '13800002003', status: 'active' },
      { id: 'I006', name: '宠安宠物诊所', type: 'hospital', districtId: 'D003', address: '西城区西单', contact: '周医生', phone: '13800002004', status: 'active' },
      // 小区
      { id: 'C001', name: '阳光花园小区', type: 'community', districtId: 'D001', address: '朝阳区阳光花园', contact: '张物业', phone: '13800003001', status: 'active' },
      { id: 'C002', name: '翠湖天地小区', type: 'community', districtId: 'D001', address: '朝阳区翠湖天地', contact: '刘物业', phone: '13800003002', status: 'active' },
      { id: 'C003', name: '中关村南区', type: 'community', districtId: 'D002', address: '海淀区中关村南区', contact: '陈物业', phone: '13800003003', status: 'active' },
      { id: 'C004', name: '西单美居', type: 'community', districtId: 'D003', address: '西城区西单北大街', contact: '杨物业', phone: '13800003004', status: 'active' }
    ];
    this._save(this.KEYS.institutions, institutions);
  },

  // === 种子数据：用户 ===
  _seedUsers() {
    const users = [
      // 市级政府管理员
      { id: 'U001', username: 'admin', password: '123456', name: '市级管理员', role: 'gov_city', districtId: null, institutionId: null, phone: '13800000001', status: 'active', createdAt: '2025-01-01' },
      // 区级政府管理员
      { id: 'U002', username: 'cy_gov', password: '123456', name: '朝阳区政府管理员', role: 'gov_district', districtId: 'D001', institutionId: null, phone: '13800000002', status: 'active', createdAt: '2025-01-01' },
      { id: 'U003', username: 'hd_gov', password: '123456', name: '海淀区政府管理员', role: 'gov_district', districtId: 'D002', institutionId: null, phone: '13800000003', status: 'active', createdAt: '2025-01-01' },
      // 捕捉点用户
      { id: 'U004', username: 'cy_shelter', password: '123456', name: '朝阳捕捉点操作员', role: 'shelter', districtId: 'D001', institutionId: 'I001', phone: '13800000004', status: 'active', createdAt: '2025-01-01' },
      { id: 'U005', username: 'hd_shelter', password: '123456', name: '海淀捕捉点操作员', role: 'shelter', districtId: 'D002', institutionId: 'I002', phone: '13800000005', status: 'active', createdAt: '2025-01-01' },
      // 医院用户
      { id: 'U006', username: 'aixin_hosp', password: '123456', name: '爱心宠物医院', role: 'hospital', districtId: 'D001', institutionId: 'I003', phone: '13800000006', status: 'active', createdAt: '2025-01-01' },
      { id: 'U007', username: 'ruipeng_hosp', password: '123456', name: '瑞鹏宠物医院', role: 'hospital', districtId: 'D001', institutionId: 'I004', phone: '13800000007', status: 'active', createdAt: '2025-01-01' },
      { id: 'U008', username: 'babitang_hosp', password: '123456', name: '芭比堂动物医院', role: 'hospital', districtId: 'D002', institutionId: 'I005', phone: '13800000008', status: 'active', createdAt: '2025-01-01' },
      // 领养人
      { id: 'U009', username: 'adopter1', password: '123456', name: '王领养', role: 'adopter', districtId: null, institutionId: null, phone: '13800000009', status: 'active', createdAt: '2025-02-01' }
    ];
    this._save(this.KEYS.users, users);
  },

  // === 种子数据：物料 ===
  _seedMaterials() {
    const materials = [
      // 疫苗
      { id: 'MAT001', name: '狂犬疫苗', category: 'vaccine', unit: '支', specification: '1ml/支', supplier: '国药集团', batchNo: 'B20250101', shelterStock: 120, hospitalStock: { 'I003': 45, 'I004': 30, 'I005': 25, 'I006': 20 }, safetyStock: 50, expiryDate: '2025-12-31' },
      { id: 'MAT002', name: '猫三联疫苗', category: 'vaccine', unit: '支', specification: '1ml/支', supplier: '英特威', batchNo: 'B20250102', shelterStock: 80, hospitalStock: { 'I003': 20, 'I004': 15, 'I005': 10, 'I006': 8 }, safetyStock: 40, expiryDate: '2025-10-31' },
      // 驱虫药
      { id: 'MAT003', name: '体内外驱虫药', category: 'dewormer', unit: '盒', specification: '6片/盒', supplier: '拜耳', batchNo: 'Q20250101', shelterStock: 60, hospitalStock: { 'I003': 18, 'I004': 12, 'I005': 8, 'I006': 6 }, safetyStock: 30, expiryDate: '2026-06-30' },
      // 芯片
      { id: 'MAT004', name: '宠物芯片', category: 'chip', unit: '个', specification: '134.2kHz', supplier: '信码科技', batchNo: 'C20250101', shelterStock: 500, hospitalStock: { 'I003': 80, 'I004': 60, 'I005': 40, 'I006': 30 }, safetyStock: 200, expiryDate: '', chipRangeStart: '1000010001', chipRangeEnd: '1000010500' }
    ];
    this._save(this.KEYS.materials, materials);

    // 芯片号段池
    const chips = [];
    for (let i = 0; i < 500; i++) {
      chips.push({
        id: 'CHIP' + String(i + 1).padStart(4, '0'),
        number: '1000010' + String(i + 1).padStart(4, '0'),
        status: i < 15 ? 'used' : 'available',
        petId: i < 5 ? 'PET001' : (i < 10 ? 'PET002' : (i < 15 ? 'PET003' : null)),
        usedAt: i < 15 ? '2025-01-20' : null
      });
    }
    this._save('tnr_chips', chips);
  },

  // === 种子数据：宠物 ===
  _seedPets() {
    const pets = [
      { id: 'PET001', code: 'TNR2501001', name: '橘猫一号', species: '猫', breed: '橘猫', gender: '公', age: '约2岁', color: '橘色', weight: '4.5kg', status: 'adopted', districtId: 'D001', captureId: 'CAP001', shelterId: 'I001', hospitalId: 'I003', photos: { group: '', before: '', after: '', treatment: '' }, chipNo: '1000010001', description: '性格亲人，已绝育、免疫、驱虫、植入芯片', createdAt: '2025-01-10' },
      { id: 'PET002', code: 'TNR2501002', name: '狸花二号', species: '猫', breed: '狸花猫', gender: '母', age: '约1岁', color: '灰黑', weight: '3.2kg', status: 'released', districtId: 'D001', captureId: 'CAP001', shelterId: 'I001', hospitalId: 'I003', photos: { group: '', before: '', after: '', treatment: '' }, chipNo: '1000010006', description: '已绝育放养至原小区', createdAt: '2025-01-10' },
      { id: 'PET003', code: 'TNR2501003', name: '黑犬三号', species: '狗', breed: '中华田园犬', gender: '公', age: '约3岁', color: '黑色', weight: '15kg', status: 'pending_adopt', districtId: 'D001', captureId: 'CAP001', shelterId: 'I001', hospitalId: 'I003', photos: { group: '', before: '', after: '', treatment: '' }, chipNo: '1000010011', description: '性格温顺，适合家庭领养', createdAt: '2025-01-10' },
      { id: 'PET004', code: 'TNR2502004', name: '白猫四号', species: '猫', breed: '白猫', gender: '母', age: '约6月', color: '白色', weight: '2.5kg', status: 'in_treatment', districtId: 'D002', captureId: 'CAP002', shelterId: 'I002', hospitalId: 'I005', photos: { group: '', before: '', after: '', treatment: '' }, chipNo: '', description: '治疗中', createdAt: '2025-01-15' },
      { id: 'PET005', code: 'TNR2502005', name: '花猫五号', species: '猫', breed: '三花猫', gender: '母', age: '约1岁', color: '三花', weight: '3.0kg', status: 'in_transit', districtId: 'D002', captureId: 'CAP002', shelterId: 'I002', hospitalId: '', photos: { group: '', before: '', after: '', treatment: '' }, chipNo: '', description: '转运中', createdAt: '2025-01-18' },
      { id: 'PET006', code: 'TNR2501006', name: '黄犬六号', species: '狗', breed: '中华田园犬', gender: '公', age: '约2岁', color: '黄色', weight: '12kg', status: 'euthanized', districtId: 'D001', captureId: 'CAP001', shelterId: 'I001', hospitalId: 'I004', photos: { group: '', before: '', after: '', treatment: '' }, chipNo: '1000010016', description: '因病重安乐死', createdAt: '2025-01-10' }
    ];
    this._save(this.KEYS.pets, pets);
  },

  // === 种子数据：捕捉记录 ===
  _seedCaptures() {
    const captures = [
      {
        id: 'CAP001', districtId: 'D001', shelterId: 'I001', shelterName: '朝阳区流浪动物捕捉点',
        communityId: 'C001', communityName: '阳光花园小区', address: '朝阳区阳光花园3栋',
        propertyName: '阳光物业', contactPerson: '张物业', contactPhone: '13800003001',
        petCount: 3, petCodes: ['TNR2501001', 'TNR2501002', 'TNR2501003'],
        groupPhoto: '', petPhotos: {},
        signature: '', status: 'completed', operator: 'U004', operatorName: '朝阳捕捉点操作员',
        createdAt: '2025-01-10 09:30', ledgerNo: 'CAP-2025-0010-001'
      },
      {
        id: 'CAP002', districtId: 'D002', shelterId: 'I002', shelterName: '海淀区流浪动物捕捉点',
        communityId: 'C003', communityName: '中关村南区', address: '海淀区中关村南区5栋',
        propertyName: '中关物业', contactPerson: '陈物业', contactPhone: '13800003003',
        petCount: 2, petCodes: ['TNR2502004', 'TNR2502005'],
        groupPhoto: '', petPhotos: {},
        signature: '', status: 'completed', operator: 'U005', operatorName: '海淀捕捉点操作员',
        createdAt: '2025-01-15 14:00', ledgerNo: 'CAP-2025-0115-001'
      }
    ];
    this._save(this.KEYS.captures, captures);
  },

  // === 种子数据：主人领回 ===
  _seedOwnerReturns() {
    this._save(this.KEYS.ownerReturns, []);
  },

  // === 种子数据：转运 ===
  _seedTransfers() {
    const transfers = [
      {
        id: 'TRF001', captureId: 'CAP001', fromShelterId: 'I001', fromShelterName: '朝阳区流浪动物捕捉点',
        toHospitalId: 'I003', toHospitalName: '爱心宠物医院',
        petCodes: ['TNR2501001', 'TNR2501002', 'TNR2501003'], petCount: 3,
        status: 'received', receivedAt: '2025-01-12', rejectReason: '',
        operator: 'U004', operatorName: '朝阳捕捉点操作员',
        createdAt: '2025-01-11 10:00', ledgerNo: 'TRF-2025-0111-001',
        districtId: 'D001'
      },
      {
        id: 'TRF002', captureId: 'CAP002', fromShelterId: 'I002', fromShelterName: '海淀区流浪动物捕捉点',
        toHospitalId: 'I005', toHospitalName: '芭比堂动物医院',
        petCodes: ['TNR2502004'], petCount: 1,
        status: 'received', receivedAt: '2025-01-16', rejectReason: '',
        operator: 'U005', operatorName: '海淀捕捉点操作员',
        createdAt: '2025-01-15 16:00', ledgerNo: 'TRF-2025-0115-001',
        districtId: 'D002'
      },
      {
        id: 'TRF003', captureId: 'CAP002', fromShelterId: 'I002', fromShelterName: '海淀区流浪动物捕捉点',
        toHospitalId: 'I005', toHospitalName: '芭比堂动物医院',
        petCodes: ['TNR2502005'], petCount: 1,
        status: 'pending', receivedAt: '', rejectReason: '',
        operator: 'U005', operatorName: '海淀捕捉点操作员',
        createdAt: '2025-01-18 09:00', ledgerNo: 'TRF-2025-0118-001',
        districtId: 'D002'
      }
    ];
    this._save(this.KEYS.transfers, transfers);
  },

  // === 种子数据：诊疗 ===
  _seedTreatments() {
    const treatments = [
      {
        id: 'TRE001', petId: 'PET001', petCode: 'TNR2501001', hospitalId: 'I003', hospitalName: '爱心宠物医院',
        items: { sterilization: true, vaccine: true, deworming: true, chip: true },
        sterilization: { surgeryDate: '2025-01-14', surgeon: '赵医生', diagnosis: '健康成年橘猫，适合绝育', anesthesia: '吸入麻醉', procedure: '常规绝育手术', recovery: '良好' },
        vaccine: { type: '狂犬疫苗', batchNo: 'B20250101', date: '2025-01-13', quantity: 1 },
        deworming: { type: '体内外驱虫药', batchNo: 'Q20250101', date: '2025-01-13', quantity: 1 },
        chip: { chipNo: '1000010001', date: '2025-01-15' },
        status: 'completed', operator: 'U006', operatorName: '爱心宠物医院',
        createdAt: '2025-01-15 16:00', districtId: 'D001'
      },
      {
        id: 'TRE002', petId: 'PET002', petCode: 'TNR2501002', hospitalId: 'I003', hospitalName: '爱心宠物医院',
        items: { sterilization: true, vaccine: true, deworming: true, chip: true },
        sterilization: { surgeryDate: '2025-01-14', surgeon: '赵医生', diagnosis: '健康狸花猫', anesthesia: '注射麻醉', procedure: '常规绝育手术', recovery: '良好' },
        vaccine: { type: '猫三联疫苗', batchNo: 'B20250102', date: '2025-01-13', quantity: 1 },
        deworming: { type: '体内外驱虫药', batchNo: 'Q20250101', date: '2025-01-13', quantity: 1 },
        chip: { chipNo: '1000010006', date: '2025-01-15' },
        status: 'completed', operator: 'U006', operatorName: '爱心宠物医院',
        createdAt: '2025-01-15 16:30', districtId: 'D001'
      },
      {
        id: 'TRE003', petId: 'PET003', petCode: 'TNR2501003', hospitalId: 'I003', hospitalName: '爱心宠物医院',
        items: { sterilization: true, vaccine: true, deworming: true, chip: true },
        sterilization: { surgeryDate: '2025-01-16', surgeon: '赵医生', diagnosis: '健康中华田园犬', anesthesia: '吸入麻醉', procedure: '常规绝育手术', recovery: '良好' },
        vaccine: { type: '狂犬疫苗', batchNo: 'B20250101', date: '2025-01-15', quantity: 1 },
        deworming: { type: '体内外驱虫药', batchNo: 'Q20250101', date: '2025-01-15', quantity: 1 },
        chip: { chipNo: '1000010011', date: '2025-01-17' },
        status: 'completed', operator: 'U006', operatorName: '爱心宠物医院',
        createdAt: '2025-01-17 15:00', districtId: 'D001'
      },
      {
        id: 'TRE004', petId: 'PET004', petCode: 'TNR2502004', hospitalId: 'I005', hospitalName: '芭比堂动物医院',
        items: { sterilization: false, vaccine: true, deworming: true, chip: false },
        sterilization: null, vaccine: { type: '猫三联疫苗', batchNo: 'B20250102', date: '2025-01-17', quantity: 1 },
        deworming: { type: '体内外驱虫药', batchNo: 'Q20250101', date: '2025-01-17', quantity: 1 },
        chip: null,
        status: 'in_progress', operator: 'U008', operatorName: '芭比堂动物医院',
        createdAt: '2025-01-17 14:00', districtId: 'D002'
      }
    ];
    this._save(this.KEYS.treatments, treatments);
  },

  // === 种子数据：物料流水 ===
  _seedMaterialTxns() {
    const txns = [
      { id: 'MTX001', type: 'purchase', materialId: 'MAT001', materialName: '狂犬疫苗', quantity: 100, unit: '支', batchNo: 'B20250101', supplier: '国药集团', fromTo: '国药集团', operator: 'U004', operatorName: '朝阳捕捉点操作员', date: '2025-01-05', ledgerNo: 'PUR-2025-0105-001', districtId: 'D001', note: '采购入库' },
      { id: 'MTX002', type: 'dispatch', materialId: 'MAT001', materialName: '狂犬疫苗', quantity: 55, unit: '支', batchNo: 'B20250101', supplier: '', fromTo: '爱心宠物医院/瑞鹏宠物医院', operator: 'U004', operatorName: '朝阳捕捉点操作员', date: '2025-01-08', ledgerNo: 'DIS-2025-0108-001', districtId: 'D001', note: '下发至医院' },
      { id: 'MTX003', type: 'consume', materialId: 'MAT001', materialName: '狂犬疫苗', quantity: 3, unit: '支', batchNo: 'B20250101', supplier: '', fromTo: '诊疗消耗', operator: 'U006', operatorName: '爱心宠物医院', date: '2025-01-13', ledgerNo: 'CON-2025-0113-001', districtId: 'D001', note: 'PET001,PET002疫苗接种' },
      { id: 'MTX004', type: 'purchase', materialId: 'MAT004', materialName: '宠物芯片', quantity: 500, unit: '个', batchNo: 'C20250101', supplier: '信码科技', fromTo: '信码科技', operator: 'U004', operatorName: '朝阳捕捉点操作员', date: '2025-01-03', ledgerNo: 'PUR-2025-0103-001', districtId: 'D001', note: '芯片采购，号段1000010001-1000010500' },
      { id: 'MTX005', type: 'dispatch', materialId: 'MAT004', materialName: '宠物芯片', quantity: 210, unit: '个', batchNo: 'C20250101', supplier: '', fromTo: '多家医院', operator: 'U004', operatorName: '朝阳捕捉点操作员', date: '2025-01-07', ledgerNo: 'DIS-2025-0107-001', districtId: 'D001', note: '下发至各医院' },
      { id: 'MTX006', type: 'consume', materialId: 'MAT004', materialName: '宠物芯片', quantity: 3, unit: '个', batchNo: 'C20250101', supplier: '', fromTo: '诊疗消耗', operator: 'U006', operatorName: '爱心宠物医院', date: '2025-01-15', ledgerNo: 'CON-2025-0115-001', districtId: 'D001', note: 'PET001,PET002,PET003芯片植入' }
    ];
    this._save(this.KEYS.materialTxns, txns);
  },

  // === 种子数据：放养 ===
  _seedReleases() {
    const releases = [
      {
        id: 'REL001', petId: 'PET002', petCode: 'TNR2501002',
        communityId: 'C001', communityName: '阳光花园小区',
        receiverName: '张物业', receiverPhone: '13800003001',
        signature: '', status: 'released', releasedAt: '2025-01-25',
        operator: 'U004', operatorName: '朝阳捕捉点操作员',
        createdAt: '2025-01-24', ledgerNo: 'REL-2025-0124-001', districtId: 'D001'
      }
    ];
    this._save(this.KEYS.releases, releases);
  },

  // === 种子数据：领养 ===
  _seedAdoptions() {
    const adoptions = [
      {
        id: 'ADP001', petId: 'PET001', petCode: 'TNR2501001',
        adopterName: '王领养', adopterPhone: '13800000009', adopterIdCard: '110105****1234',
        adopterAddress: '朝阳区某某小区', qualification: '有稳定住所，有养宠经验',
        commitmentLetter: '已签署', adoptionAgreement: '线下已签',
        hospitalId: 'I003', hospitalName: '爱心宠物医院',
        status: 'completed', adoptedAt: '2025-02-01',
        operator: 'U004', operatorName: '朝阳捕捉点操作员',
        createdAt: '2025-01-28', ledgerNo: 'ADP-2025-0128-001', districtId: 'D001'
      }
    ];
    this._save(this.KEYS.adoptions, adoptions);
  },

  // === 种子数据：回访打卡 ===
  _seedCheckIns() {
    const checkIns = [
      { id: 'CHK001', petId: 'PET001', petCode: 'TNR2501001', adopterId: 'U009', adopterName: '王领养', month: '2025-02', photo: '', note: '猫咪适应良好，食欲正常', status: 'approved', createdAt: '2025-02-15' },
      { id: 'CHK002', petId: 'PET001', petCode: 'TNR2501001', adopterId: 'U009', adopterName: '王领养', month: '2025-03', photo: '', note: '一切正常，体重增长', status: 'approved', createdAt: '2025-03-14' }
    ];
    this._save(this.KEYS.checkIns, checkIns);
  },

  // === 种子数据：黑名单 ===
  _seedBlacklist() {
    const blacklist = [
      { id: 'BLK001', name: '李某某', idCard: '110102****5678', phone: '13900000001', reason: '弃养领养宠物', violationDate: '2024-12-15', operator: 'U004', operatorName: '朝阳捕捉点操作员', createdAt: '2024-12-20', districtId: 'D001' }
    ];
    this._save(this.KEYS.blacklist, blacklist);
  },

  // === 种子数据：安乐死 ===
  _seedEuthanasia() {
    const records = [
      {
        id: 'EUT001', petId: 'PET006', petCode: 'TNR2501006',
        hospitalId: 'I004', hospitalName: '瑞鹏宠物医院',
        reason: '严重外伤感染，无法救治', condition: '后腿骨折感染，多处伤口化脓',
        euthanizedAt: '2025-01-20', bodyReceived: true, bodyReceivedAt: '2025-01-21',
        bodyReceivedBy: 'U004', bodyReceivedByName: '朝阳捕捉点操作员',
        operator: 'U007', operatorName: '瑞鹏宠物医院',
        createdAt: '2025-01-20', ledgerNo: 'EUT-2025-0120-001', districtId: 'D001'
      }
    ];
    this._save(this.KEYS.euthanasia, records);
  },

  // === 种子数据：消息 ===
  _seedMessages() {
    const messages = [
      { id: 'MSG001', userId: 'U009', type: 'approval', title: '领养审核通过', content: '您的领养申请已通过审核，PET001已成功领养。', isRead: false, createdAt: '2025-02-01' },
      { id: 'MSG002', userId: 'U009', type: 'checkin_reminder', title: '月度打卡提醒', content: '请于本月完成PET001的回访打卡。', isRead: true, createdAt: '2025-02-10' },
      { id: 'MSG003', userId: 'U009', type: 'checkin_reminder', title: '3月打卡提醒', content: '请于3月完成PET001的月度回访打卡。', isRead: false, createdAt: '2025-03-01' }
    ];
    this._save(this.KEYS.messages, messages);
  },

  // === 底层存储方法 ===
  _load(key) {
    try {
      const data = localStorage.getItem(key);
      return data ? JSON.parse(data) : [];
    } catch (e) { return []; }
  },

  _save(key, data) {
    localStorage.setItem(key, JSON.stringify(data));
  },

  _genId(prefix) {
    return prefix + Date.now().toString(36).toUpperCase() + Math.random().toString(36).substr(2, 4).toUpperCase();
  },

  _genCode(prefix, date, seq) {
    const d = new Date();
    const dateStr = (d.getFullYear() + 1).toString().substr(-2) + String(d.getMonth() + 1).padStart(2, '0') + String(d.getDate()).padStart(2, '0');
    return prefix + dateStr + String(seq).padStart(3, '0');
  },

  _genLedgerNo(prefix) {
    const d = new Date();
    const dateStr = d.getFullYear().toString().substr(-2) + String(d.getMonth() + 1).padStart(2, '0') + String(d.getDate()).padStart(2, '0');
    const rand = String(Math.floor(Math.random() * 900) + 100);
    return prefix + '-' + dateStr + '-' + rand;
  },

  // === 用户/会话管理 ===
  login(username, password) {
    const users = this._load(this.KEYS.users);
    const user = users.find(u => u.username === username && u.password === password && u.status === 'active');
    if (user) {
      this._currentUser = user;
      sessionStorage.setItem('tnr_session', JSON.stringify(user));
      return user;
    }
    return null;
  },

  logout() {
    this._currentUser = null;
    sessionStorage.removeItem('tnr_session');
  },

  getCurrentUser() {
    if (this._currentUser) return this._currentUser;
    const session = sessionStorage.getItem('tnr_session');
    if (session) {
      this._currentUser = JSON.parse(session);
      return this._currentUser;
    }
    return null;
  },

  setCurrentUser(user) {
    this._currentUser = user;
    sessionStorage.setItem('tnr_session', JSON.stringify(user));
  },

  // === 区县过滤 ===
  getDistrictFilter() {
    const user = this.getCurrentUser();
    if (!user) return null;
    // 市级管理员可见所有数据
    if (user.role === 'gov_city') return null;
    return user.districtId;
  },

  filterByDistrict(data) {
    const districtId = this.getDistrictFilter();
    if (!districtId) return data;
    return data.filter(item => !item.districtId || item.districtId === districtId);
  },

  // === 通用 CRUD ===
  getAll(key) {
    const data = this._load(key);
    return this.filterByDistrict(data);
  },

  getAllRaw(key) {
    return this._load(key);
  },

  getById(key, id) {
    const data = this._load(key);
    return data.find(item => item.id === id);
  },

  create(key, item) {
    const data = this._load(key);
    const user = this.getCurrentUser();
    if (user && user.districtId && !item.districtId) {
      item.districtId = user.districtId;
    }
    if (!item.id) item.id = this._genId(key.substr(4, 3).toUpperCase());
    if (!item.createdAt) item.createdAt = new Date().toISOString().substr(0, 16).replace('T', ' ');
    data.push(item);
    this._save(key, data);
    return item;
  },

  update(key, id, updates) {
    const data = this._load(key);
    const idx = data.findIndex(item => item.id === id);
    if (idx !== -1) {
      data[idx] = { ...data[idx], ...updates };
      this._save(key, data);
      return data[idx];
    }
    return null;
  },

  delete(key, id) {
    const data = this._load(key);
    const filtered = data.filter(item => item.id !== id);
    this._save(key, filtered);
    return filtered.length < data.length;
  },

  // === 特殊业务方法 ===

  // 生成宠物编号
  generatePetCodes(count) {
    const pets = this._load(this.KEYS.pets);
    const codes = [];
    const d = new Date();
    const yearStr = (d.getFullYear() + 1).toString().substr(-2) + String(d.getMonth() + 1).padStart(2, '0') + String(d.getDate()).padStart(2, '0');
    const existing = pets.filter(p => p.code && p.code.includes(yearStr));
    let seq = existing.length + 1;
    for (let i = 0; i < count; i++) {
      codes.push('TNR' + yearStr + String(seq + i).padStart(3, '0'));
    }
    return codes;
  },

  // 获取可用芯片
  getAvailableChips() {
    const chips = this._load('tnr_chips');
    return chips.filter(c => c.status === 'available');
  },

  // 使用芯片
  useChip(chipNo, petId) {
    const chips = this._load('tnr_chips');
    const chip = chips.find(c => c.number === chipNo);
    if (chip && chip.status === 'available') {
      chip.status = 'used';
      chip.petId = petId;
      chip.usedAt = new Date().toISOString().substr(0, 10);
      this._save('tnr_chips', chips);
      return true;
    }
    return false;
  },

  // 检查黑名单
  checkBlacklist(idCard, phone) {
    const blacklist = this._load(this.KEYS.blacklist);
    return blacklist.find(b =>
      (idCard && b.idCard && b.idCard.replace(/\*/g, '').substr(0, 6) === idCard.replace(/\*/g, '').substr(0, 6)) ||
      (phone && b.phone === phone)
    );
  },

  // 获取宠物状态文案
  getPetStatusText(status) {
    const map = {
      'in_transit': '在途',
      'in_treatment': '待诊疗/诊疗中',
      'pending_adopt': '待领养',
      'adopted': '已领养',
      'released': '已放养',
      'euthanized': '已安乐死',
      'owner_returned': '主人领回'
    };
    return map[status] || status;
  },

  // 获取宠物状态徽章
  getPetStatusBadge(status) {
    const map = {
      'in_transit': 'badge-warning',
      'in_treatment': 'badge-info',
      'pending_adopt': 'badge-cinnabar',
      'adopted': 'badge-success',
      'released': 'badge-success',
      'euthanized': 'badge-danger',
      'owner_returned': 'badge-default'
    };
    return map[status] || 'badge-default';
  },

  // 获取物料分类文案
  getMaterialCategoryText(cat) {
    const map = { 'vaccine': '疫苗', 'dewormer': '驱虫药', 'chip': '芯片' };
    return map[cat] || cat;
  },

  // 获取机构
  getInstitutions(type, districtId) {
    let insts = this._load(this.KEYS.institutions).filter(i => i.type === type && i.status === 'active');
    if (districtId) insts = insts.filter(i => i.districtId === districtId);
    return insts;
  },

  // 获取区县
  getDistricts() {
    return this._load(this.KEYS.districts).filter(d => d.status === 'active');
  },

  // 重置数据
  resetAll() {
    Object.values(this.KEYS).forEach(key => localStorage.removeItem(key));
    localStorage.removeItem('tnr_chips');
    this.init();
  }
};

// 初始化数据库
TNR_DB.init();
