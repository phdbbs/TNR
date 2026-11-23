"""批量导出一宠一档 PDF（ZIP 下载）。

⚠ 路径**不放 `/api/`**：这里返回的是 `application/zip` 二进制流，
  而 `/api/` 的契约是 `json_ok` 信封（静态判据也会检查）。与 `/print/`
  一样属于「文件出口」，因此用独立前缀 `/export/`。

权限与取数复用 `pet_for_lifecycle` + `build_pet_lifecycle`：即使前端传了
越权的宠物 id，也只是不产出那一份 PDF，不存在与越权不可区分。
"""
import io
import json
import re
import zipfile
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from accounts.decorators import role_required
from business.services_lifecycle import pet_for_lifecycle
from business.services_pdf import (
    MAX_BATCH_PET_ARCHIVE, render_pet_archive_pdf, zip_filename,
)
from core.http import json_fail


@csrf_exempt
@role_required('shelter', 'hospital', 'gov_city', 'gov_district')
@login_required
def batch_pet_archive_pdf(request):
    """POST /export/pet-archive/batch/  `{pet_ids: [1,2,...]}` → ZIP。

    `pet_ids` 来自前端**当前筛选结果**（所见即所得），后端不重写筛选逻辑；
    数量上限与前端共用 `MAX_BATCH_PET_ARCHIVE`，避免「界面让选 500 只、
    服务端只给 100 只」这种静默截断。
    """
    if request.method != 'POST':
        return json_fail('仅支持 POST 请求', status=405)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return json_fail('请求格式错误', status=400)

    raw_ids = payload.get('pet_ids')
    if not isinstance(raw_ids, list) or not raw_ids:
        return json_fail('请先选择要导出的宠物', status=400)

    ids = []
    for value in raw_ids:
        # bool 是 int 的子类，必须经过这一关，否则 True 会变成 id=1
        if isinstance(value, bool):
            return json_fail('宠物编号格式错误', status=400)
        try:
            pid = int(value)
        except (TypeError, ValueError, OverflowError):
            return json_fail('宠物编号格式错误', status=400)
        if pid <= 0:
            return json_fail('宠物编号格式错误', status=400)
        if pid not in ids:
            ids.append(pid)

    if len(ids) > MAX_BATCH_PET_ARCHIVE:
        return json_fail(
            f'一次最多导出 {MAX_BATCH_PET_ARCHIVE} 只，当前 {len(ids)} 只；'
            '请缩小筛选范围后重试', status=400)

    printed_by = request.user.get_full_name() or request.user.username
    buf = io.BytesIO()
    written = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for pid in ids:
            pet = pet_for_lifecycle(request.user, pid)
            if pet is None:
                # 不存在 / 越权一律跳过，不给前端任何区分线索
                continue
            pdf_bytes = render_pet_archive_pdf(pet, request.user, printed_by)
            safe_code = re.sub(r'[^\w\u4e00-\u9fff.\-]', '_', pet.code)
            archive.writestr(f'{safe_code}_一宠一档.pdf', pdf_bytes)
            written.append(pet.code)

    if not written:
        return json_fail('没有可导出的宠物（不存在或无权访问）', status=404)

    response = HttpResponse(buf.getvalue(), content_type='application/zip')
    name = quote(zip_filename())
    response['Content-Disposition'] = (
        f'attachment; filename="pet_archives.zip"; filename*=UTF-8\'\'{name}'
    )
    response['X-Pet-Archive-Count'] = str(len(written))
    return response
