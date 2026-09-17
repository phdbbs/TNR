"""一宠一档：逐只登记属性、动物档案台账、全生命周期照片。

覆盖本轮新增能力：
1. 新增捕捉支持逐只录入 物种(猫/狗) / 性别(公/母) / 品种 / 昵称；
   非法取值直接报错且**不落任何库**（两段式：先全校验、再事务写入）。
2. 前端预览过的编号必须被沿用（``pet_codes``），否则逐只属性与照片会
   与落库编号错位、全部静默落空。
3. 编辑捕捉可逐只修正属性，且只改显式提交的字段（不把物种重置成默认「猫」），
   四个字段前缀（物种/性别/品种/昵称）都要能单独生效。
4. `GET /api/business/pets/archive/` 返回每只动物一行的「一宠一档」台账，
   字段含 物种/品种/性别/芯片/来源/去向/照片，并遵守区县隔离。
5. `pets/<id>/lifecycle/` 各阶段事件带上照片，响应带 pet_brief + photos。
"""
from django.utils import timezone

from business.models import (
    Adoption, Capture, Euthanasia, OwnerReturn, Pet, Release, Treatment,
)
from business.services import pet_archive_records, pet_photo_list
from business.tests.base import (
    BusinessTestBase, make_capture, make_district, make_pet,
)

CAPTURES_URL = '/api/business/captures/'
ARCHIVE_URL = '/api/business/pets/archive/'


def _pet_codes(n, start=901):
    """构造 n 个格式合法的宠物编号（TNR + YYMMDD + 3 位序号）。

    新增捕捉页的真实流程是「先调 codes-preview 拿编号 → 再按编号逐只填
    物种/性别/品种/昵称与照片」，所以测试必须走同一条路：把编号一并提交，
    属性字段的 key 才和最终落库的编号对得上。序号从 901 起，避开
    ``generate_pet_codes`` 会生成的区间。
    """
    ymd = timezone.localdate().strftime('%y%m%d')
    return [f'TNR{ymd}{start + i:03d}' for i in range(n)]


def _pet_attr_payload(pet_codes, species=None, gender=None, breed='', name=''):
    """按编号构造逐只属性字段（与前端 pet_<字段>_<编号> 命名一致）。"""
    payload = {}
    for i, code in enumerate(pet_codes):
        if species is not None:
            payload[f'pet_species_{code}'] = species[i] if isinstance(species, list) else species
        if gender is not None:
            payload[f'pet_gender_{code}'] = gender[i] if isinstance(gender, list) else gender
        payload[f'pet_breed_{code}'] = breed[i] if isinstance(breed, list) else breed
        payload[f'pet_name_{code}'] = name[i] if isinstance(name, list) else name
    return payload


class PerPetAttributeCreateTest(BusinessTestBase):
    """新增捕捉：逐只录入物种/性别/品种/昵称。"""

    def _payload(self, pet_count=2, **kw):
        payload = {
            'shelter_id': self.shelter_a.id,
            'property_name': '阳光物业',
            'community_name': '甲区阳光小区',
            'address': '幸福路1号',
            'contact_person': '物业张三',
            'contact_phone': '13800001234',
            'pet_count': pet_count,
            'signature': 'data:image/png;base64,AAAA',
        }
        payload.update(kw)
        return payload

    def _create(self, pet_count=2, **kw):
        self.login_as(self.shelter_user_a)
        return self.ok(self.post_json(f'{CAPTURES_URL}create/', self._payload(pet_count, **kw)))

    def test_per_pet_attributes_are_saved(self):
        """每只动物各自落库，互不串味。"""
        self.login_as(self.shelter_user_a)
        codes = _pet_codes(2)
        payload = self._payload(pet_count=2, pet_codes=codes)
        payload.update(_pet_attr_payload(
            codes,
            species=['狗', '猫'],
            gender=['公', '母'],
            breed=['中华田园犬', '狸花猫'],
            name=['大黄', '小花'],
        ))
        self.ok(self.post_json(f'{CAPTURES_URL}create/', payload))

        pets = {p.code: p for p in Pet.objects.filter(code__in=codes)}
        self.assertEqual(len(pets), 2, '提交的编号应被原样使用')
        self.assertEqual(pets[codes[0]].species, '狗')
        self.assertEqual(pets[codes[0]].gender, '公')
        self.assertEqual(pets[codes[0]].breed, '中华田园犬')
        self.assertEqual(pets[codes[0]].name, '大黄')
        self.assertEqual(pets[codes[1]].species, '猫')
        self.assertEqual(pets[codes[1]].gender, '母')
        self.assertEqual(pets[codes[1]].breed, '狸花猫')
        self.assertEqual(pets[codes[1]].name, '小花')

    def test_missing_attributes_fall_back_to_cat_and_unknown_gender(self):
        """缺省时不报错：物种回退「猫」，性别留空（模型允许未知）。"""
        body = self._create(pet_count=1)
        pet = Pet.objects.get(code=body['data']['pet_codes'][0])
        self.assertEqual(pet.species, '猫')
        self.assertEqual(pet.gender, '')

    def test_batch_species_field_still_applies(self):
        """兼容旧客户端：只传整批 species 时，每只都用它。"""
        body = self._create(pet_count=2, species='狗')
        for code in body['data']['pet_codes']:
            self.assertEqual(Pet.objects.get(code=code).species, '狗')

    def test_server_generates_codes_when_not_submitted(self):
        """未提交编号时（脚本/老客户端）由服务端生成，编号格式合法且不重复。"""
        body = self._create(pet_count=3)
        codes = body['data']['pet_codes']
        self.assertEqual(len(codes), 3)
        self.assertEqual(len(set(codes)), 3)
        self.assertEqual(
            sorted(codes),
            sorted(Pet.objects.filter(code__in=codes).values_list('code', flat=True)))

    def test_invalid_species_rejected_without_writing_anything(self):
        """非法物种必须报错，且**不能留下只建了一半的捕捉单**。

        这是两段式写入的回归点：校验若发生在 Capture 落库之后，
        这里会留下一张孤儿捕捉单 + 已被占用的编号。
        """
        self.login_as(self.shelter_user_a)
        codes = _pet_codes(2)
        payload = self._payload(pet_count=2, pet_codes=codes)
        # 第 2 只填了非法值，第 1 只完全合法
        payload[f'pet_species_{codes[0]}'] = '猫'
        payload[f'pet_species_{codes[1]}'] = '犬'
        before_captures = Capture.objects.count()
        before_pets = Pet.objects.count()

        resp = self.post_json(f'{CAPTURES_URL}create/', payload)
        self.expect_fail(resp, message='只能是猫或狗')
        self.assertEqual(Capture.objects.count(), before_captures,
                         '校验失败时不应留下任何捕捉单')
        self.assertEqual(Pet.objects.count(), before_pets,
                         '校验失败时不应留下任何宠物档案')

    def test_invalid_gender_rejected(self):
        self.login_as(self.shelter_user_a)
        codes = _pet_codes(1)
        payload = self._payload(pet_count=1, pet_codes=codes)
        payload[f'pet_gender_{codes[0]}'] = '雄'
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/', payload),
            message='只能是公或母')

    def test_code_count_mismatch_rejected(self):
        """编号数量与捕捉数量不一致时必须报错，不能悄悄换一套编号。

        悄悄换编号 = 用户刚填的逐只属性全丢，且界面上看不出来。
        """
        self.login_as(self.shelter_user_a)
        payload = self._payload(pet_count=2, pet_codes=_pet_codes(1))
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/', payload),
            message='不一致')

    def test_duplicate_codes_rejected(self):
        self.login_as(self.shelter_user_a)
        codes = _pet_codes(1)
        payload = self._payload(pet_count=2, pet_codes=codes * 2)
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/', payload),
            message='重复')

    def test_taken_code_rejected(self):
        """编号已被占用时明确报错（Pet.code 是 unique，放过去会 500）。"""
        make_pet(code=_pet_codes(1)[0], district=self.district_a)
        self.login_as(self.shelter_user_a)
        payload = self._payload(pet_count=1, pet_codes=_pet_codes(1))
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/', payload),
            message='已被占用')

    def test_malformed_code_rejected(self):
        self.login_as(self.shelter_user_a)
        payload = self._payload(pet_count=1, pet_codes=['TNRBAD001'])
        self.expect_fail(
            self.post_json(f'{CAPTURES_URL}create/', payload),
            message='格式不正确')

    def test_comma_separated_codes_accepted(self):
        """JSON 客户端可能把编号拼成逗号串，也要认。"""
        self.login_as(self.shelter_user_a)
        codes = _pet_codes(2)
        payload = self._payload(pet_count=2, pet_codes=','.join(codes))
        payload.update(_pet_attr_payload(codes, species='狗', gender='公'))
        self.ok(self.post_json(f'{CAPTURES_URL}create/', payload))
        self.assertEqual(
            Pet.objects.filter(code__in=codes, species='狗').count(), 2)


class PerPetAttributeUpdateTest(BusinessTestBase):
    """编辑捕捉：逐只修正属性，且只改显式提交的字段。"""

    def setUp(self):
        self.capture = make_capture(
            district=self.district_a, shelter=self.shelter_a,
            pet_codes=['TNREDIT01'])
        self.pet = make_pet(
            code='TNREDIT01', district=self.district_a, capture=self.capture,
            species='狗', gender='公', breed='中华田园犬')

    def _update(self, data):
        self.login_as(self.shelter_user_a)
        return self.post_json(f'{CAPTURES_URL}{self.capture.id}/update/', data)

    def test_partial_update_does_not_reset_species(self):
        """只提交性别时，物种不能被缺省值「猫」覆盖掉。"""
        self.ok(self._update({f'pet_gender_{self.pet.id}': '母'}))
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.gender, '母')
        self.assertEqual(self.pet.species, '狗', '未提交的物种不应被改动')
        self.assertEqual(self.pet.breed, '中华田园犬')

    def test_only_breed_submitted_is_applied(self):
        """只改品种也要生效——探测前缀漏判会让这类提交整批静默失效。"""
        self.ok(self._update({f'pet_breed_{self.pet.id}': '德国牧羊犬'}))
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.breed, '德国牧羊犬')
        self.assertEqual(self.pet.species, '狗')
        self.assertEqual(self.pet.gender, '公')

    def test_only_name_submitted_is_applied(self):
        self.ok(self._update({f'pet_name_{self.pet.id}': '大黄'}))
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.name, '大黄')
        self.assertEqual(self.pet.species, '狗')
        self.assertEqual(self.pet.breed, '中华田园犬')

    def test_update_all_attributes(self):
        self.ok(self._update({
            f'pet_species_{self.pet.id}': '猫',
            f'pet_gender_{self.pet.id}': '母',
            f'pet_breed_{self.pet.id}': '狸花猫',
            f'pet_name_{self.pet.id}': '小花',
        }))
        self.pet.refresh_from_db()
        self.assertEqual(
            (self.pet.species, self.pet.gender, self.pet.breed, self.pet.name),
            ('猫', '母', '狸花猫', '小花'))

    def test_invalid_value_rejected_and_unchanged(self):
        resp = self._update({f'pet_species_{self.pet.id}': '鸟'})
        self.expect_fail(resp, message='只能是猫或狗')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.species, '狗')

    def test_invalid_gender_rejected_and_unchanged(self):
        resp = self._update({f'pet_gender_{self.pet.id}': '雄'})
        self.expect_fail(resp, message='只能是公或母')
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.gender, '公')

    def test_non_numeric_pet_key_is_ignored(self):
        """伪造的键（pet_species_abc）不能让接口 500，也不能误改任何动物。"""
        resp = self._update({f'pet_species_{self.pet.id}': '猫',
                             'pet_species_abc': '狗'})
        self.ok(resp)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.species, '猫')

    def test_other_captures_pet_is_not_touched(self):
        """跨单的宠物 id 必须改不动（越权写入）。"""
        other_capture = make_capture(
            district=self.district_a, shelter=self.shelter_a,
            pet_codes=['TNREDIT02'])
        other_pet = make_pet(
            code='TNREDIT02', district=self.district_a, capture=other_capture,
            species='猫')
        self.ok(self._update({f'pet_species_{other_pet.id}': '狗'}))
        other_pet.refresh_from_db()
        self.assertEqual(other_pet.species, '猫', '不属于本捕捉单的动物不应被修改')


class PetArchiveApiTest(BusinessTestBase):
    """一宠一档台账接口：字段齐备 + 区县隔离。"""

    def test_record_carries_one_pet_one_code_fields(self):
        make_pet(code='TNRARC001', district=self.district_a, shelter=self.shelter_a,
                 species='狗', gender='公', breed='中华田园犬', name='大黄',
                 chip_no='1000010001')
        self.login_as(self.shelter_user_a)
        rows = self.ok(self.get_json(ARCHIVE_URL))['data']
        row = next(r for r in rows if r['ledger_no'] == 'TNRARC001')
        self.assertEqual(row['business_type'], 'pet')
        self.assertEqual(row['species'], '狗')
        self.assertEqual(row['breed'], '中华田园犬')
        self.assertEqual(row['gender'], '公')
        self.assertEqual(row['name'], '大黄')
        self.assertEqual(row['chip_no'], '1000010001')
        self.assertIn('status_display', row)
        self.assertIn('photos', row)

    def test_record_emits_both_snake_and_camel_keys(self):
        """两种命名必须同时产出。

        捕捉端读 camelCase（``r.ledgerNo``）、政府端读 snake_case
        （``r.ledger_no``）。只出一套时，读不到的那一端会**静默**出问题：
        编号列整列空白、编号点不开档案，接口却仍是 200。
        """
        make_pet(code='TNRARC002', district=self.district_a, shelter=self.shelter_a,
                 species='狗', breed='中华田园犬', gender='公')
        self.login_as(self.shelter_user_a)
        row = next(r for r in self.ok(self.get_json(ARCHIVE_URL))['data']
                   if r['ledger_no'] == 'TNRARC002')
        for snake, camel in (
            ('ledger_no', 'ledgerNo'), ('business_type', 'businessType'),
            ('status_display', 'statusDisplay'), ('chip_no', 'chipNo'),
            ('district_name', 'districtName'), ('intake_from', 'intakeFrom'),
            ('outbound_reason', 'outboundReason'), ('delivery_unit', 'deliveryUnit'),
            ('sterilized_at', 'sterilizedAt'), ('treatment_records', 'treatmentRecords'),
        ):
            self.assertIn(snake, row, f'缺少 {snake}')
            self.assertIn(camel, row, f'缺少 {camel}（捕捉端读的就是这个）')
            self.assertEqual(row[snake], row[camel], f'{snake} 与 {camel} 取值不一致')
        # detail 里两套键同样要齐（政府端读 detail.treatment_records）
        self.assertIn('treatment_records', row['detail'])
        self.assertIn('treatmentRecords', row['detail'])

    def test_shelter_sees_only_own_district(self):
        """区县隔离：甲区捕捉点看不到乙区动物。"""
        make_pet(code='TNRARCA1', district=self.district_a)
        make_pet(code='TNRARCB1', district=self.district_b)
        self.login_as(self.shelter_user_a)
        codes = {r['ledger_no'] for r in self.ok(self.get_json(ARCHIVE_URL))['data']}
        self.assertIn('TNRARCA1', codes)
        self.assertNotIn('TNRARCB1', codes)

    def test_district_gov_sees_only_own_district(self):
        make_pet(code='TNRARCA2', district=self.district_a)
        make_pet(code='TNRARCB2', district=self.district_b)
        self.login_as(self.gov_a)
        codes = {r['ledger_no'] for r in self.ok(self.get_json(ARCHIVE_URL))['data']}
        self.assertIn('TNRARCA2', codes)
        self.assertNotIn('TNRARCB2', codes)

    def test_city_gov_sees_all_districts(self):
        make_pet(code='TNRARCA3', district=self.district_a)
        make_pet(code='TNRARCB3', district=self.district_b)
        self.login_as(self.gov_city)
        codes = {r['ledger_no'] for r in self.ok(self.get_json(ARCHIVE_URL))['data']}
        self.assertIn('TNRARCA3', codes)
        self.assertIn('TNRARCB3', codes)

    def test_deleted_pets_are_excluded(self):
        make_pet(code='TNRARCDEL', district=self.district_a, is_deleted=True)
        self.login_as(self.shelter_user_a)
        codes = {r['ledger_no'] for r in self.ok(self.get_json(ARCHIVE_URL))['data']}
        self.assertNotIn('TNRARCDEL', codes)

    def test_adopter_forbidden(self):
        self.login_as(self.adopter)
        self.expect_fail(self.get_json(ARCHIVE_URL), status=403)

    def test_anonymous_unauthorized(self):
        self.expect_fail(self.get_json(ARCHIVE_URL), status=401)


class PetArchiveOutboundTest(BusinessTestBase):
    """一宠一档台账的「去向」推导：放养 / 领养 / 死亡 / 主人领回。"""

    def _row(self, pet):
        return pet_archive_records([pet])[0]

    def test_released(self):
        pet = make_pet(code='TNRGO01', status='released', district=self.district_a)
        Release.objects.create(
            pet=pet, pet_code=pet.code, community_name='甲区小区',
            status='released', district=self.district_a)
        row = self._row(pet)
        self.assertEqual(row['outbound_reason'], '放养')
        self.assertEqual(row['delivery_unit'], '甲区小区')

    def test_adopted(self):
        pet = make_pet(code='TNRGO02', status='adopted', district=self.district_a)
        Adoption.objects.create(
            pet=pet, pet_code=pet.code, adopter_name='李四',
            status='completed', district=self.district_a)
        row = self._row(pet)
        self.assertEqual(row['outbound_reason'], '领养')
        self.assertIn('李四', row['delivery_unit'])

    def test_euthanized(self):
        pet = make_pet(code='TNRGO03', status='euthanized', district=self.district_a)
        Euthanasia.objects.create(
            pet=pet, pet_code=pet.code, hospital_name='甲区医院',
            reason='重病', district=self.district_a)
        row = self._row(pet)
        self.assertEqual(row['outbound_reason'], '死亡')
        self.assertEqual(row['delivery_unit'], '甲区医院')

    def test_owner_returned(self):
        pet = make_pet(code='TNRGO04', status='owner_returned', district=self.district_a)
        OwnerReturn.objects.create(
            pet=pet, pet_code=pet.code, owner_name='王五',
            reason='走失', district=self.district_a)
        row = self._row(pet)
        self.assertEqual(row['outbound_reason'], '主人领回')
        self.assertEqual(row['delivery_unit'], '王五')

    def test_in_transit_has_no_outbound(self):
        pet = make_pet(code='TNRGO05', district=self.district_a)
        row = self._row(pet)
        self.assertEqual(row['outbound_reason'], '')
        self.assertEqual(row['outbound_at'], '')

    def test_treatment_records_and_sterilized_flag(self):
        pet = make_pet(code='TNRGO06', district=self.district_a)
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital_name='甲区医院',
            items_sterilization=True, items_vaccine=True,
            sterilization_surgeon='张医生', vaccine_type='猫三联',
            district=self.district_a)
        row = self._row(pet)
        self.assertTrue(row['sterilized'])
        self.assertEqual(len(row['treatment_records']), 1)
        self.assertEqual(row['treatment_records'][0]['doctor'], '张医生')
        self.assertIn('绝育', row['treatment_records'][0]['items'])
        self.assertEqual(len(row['vaccine_records']), 1)
        self.assertEqual(row['vaccine_records'][0]['drug'], '猫三联')


class PetPhotoAndLifecycleTest(BusinessTestBase):
    """照片：一宠一档台账与生命周期各阶段都要带出可放大的图片。"""

    def _make_capture_with_pet(self):
        capture = make_capture(
            district=self.district_a, shelter=self.shelter_a,
            pet_codes=['TNRPHOT01'])
        capture.group_photo = 'photos/group.png'
        capture.save(update_fields=['group_photo'])
        pet = make_pet(code='TNRPHOT01', district=self.district_a,
                       capture=capture, shelter=self.shelter_a,
                       species='猫', gender='母')
        pet.photo_capture = 'photos/cap.png'
        pet.photo_before = 'photos/before.png'
        pet.photo_after = 'photos/after.png'
        pet.photo_treatment = 'photos/trt.png'
        pet.save()
        return capture, pet

    def test_photo_list_labels_and_order(self):
        _, pet = self._make_capture_with_pet()
        photos = pet_photo_list(pet)
        self.assertEqual(
            [p['label'] for p in photos],
            ['捕捉照片', '术前', '术后', '诊疗'])
        for p in photos:
            self.assertTrue(p['url'].startswith('/media/'))

    def test_archive_row_exposes_photos(self):
        _, pet = self._make_capture_with_pet()
        row = pet_archive_records([pet])[0]
        labels = {p['label'] for p in row['photos']}
        self.assertIn('捕捉照片', labels)
        self.assertEqual({p['label'] for p in row['detail']['photos']}, labels)

    def test_lifecycle_carries_pet_brief_and_photos(self):
        capture, pet = self._make_capture_with_pet()
        self.login_as(self.shelter_user_a)
        data = self.ok(self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))['data']
        self.assertEqual(data['pet_brief']['code'], 'TNRPHOT01')
        self.assertEqual(data['pet_brief']['species'], '猫')
        self.assertEqual(data['pet_brief']['gender'], '母')
        self.assertEqual(len(data['photos']), 4)

    def test_capture_event_has_capture_and_group_photo(self):
        capture, pet = self._make_capture_with_pet()
        self.login_as(self.shelter_user_a)
        events = self.ok(
            self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))['data']['events']
        capture_ev = next(e for e in events if e['type'] == 'capture')
        labels = {p['label'] for p in capture_ev['photos']}
        self.assertEqual(labels, {'捕捉照片', '整体合影'})
        self.assertEqual(capture_ev['property_name'], capture.property_name)

    def test_treatment_event_has_stage_photos(self):
        capture, pet = self._make_capture_with_pet()
        Treatment.objects.create(
            pet=pet, pet_code=pet.code, hospital_name='甲区医院',
            items_sterilization=True, district=self.district_a)
        self.login_as(self.shelter_user_a)
        events = self.ok(
            self.get_json(f'/api/business/pets/{pet.id}/lifecycle/'))['data']['events']
        trt_ev = next(e for e in events if e['type'] == 'treatment')
        self.assertEqual(
            {p['label'] for p in trt_ev['photos']},
            {'术前', '术后', '诊疗'})
