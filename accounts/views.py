from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from accounts.models import User
from core.http import read_json_body


def _redirect_by_role(user):
    """根据用户角色重定向到对应门户。"""
    role_map = {
        'gov_city': '/gov/',
        'gov_district': '/gov/',
        'shelter': '/shelter/',
        'hospital': '/hospital/',
        'adopter': '/adopter/',
    }
    return redirect(role_map.get(user.role, '/'))


def login_view(request):
    if request.user.is_authenticated:
        return _redirect_by_role(request.user)

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return _redirect_by_role(user)
        # 区分提示：账号存在但已停用时，明确告知原因而不是误导性的密码错误
        username_qs = User.objects.filter(username=username)
        if username_qs.exists() and not username_qs.filter(is_active=True).exists():
            messages.error(request, '该账号已被停用，请联系管理员恢复')
        else:
            messages.error(request, '用户名或密码错误')

    return render(request, 'login.html')


def logout_view(request):
    logout(request)
    return redirect('accounts:login')


@login_required
def dashboard_redirect(request):
    """根路径根据角色重定向到对应门户。"""
    return _redirect_by_role(request.user)


def api_me(request):
    """Return current user info as JSON"""
    if not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': '未登录'}, status=401)
    u = request.user
    return JsonResponse({
        'success': True,
        'data': {
            'id': u.id,
            'username': u.username,
            'name': u.get_full_name() or u.username,
            'role': u.role,
            'role_display': u.get_role_display(),
            'district_id': u.district_id,
            'district_name': u.district.name if u.district else '',
            'institution_id': u.institution_id,
            'institution_name': u.institution.name if u.institution else '',
            'phone': u.phone,
            'is_superuser': u.is_superuser,
        }
    })


@csrf_exempt
@login_required
def api_change_password(request):
    """修改当前登录用户的密码。

    POST JSON: {"old_password": "...", "new_password": "...", "confirm_password": "..."}
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': '仅支持 POST 请求'}, status=405)

    # ⚠ 必须走 `core.http.read_json_body()`，不能裸读 `request.body`：
    # `request.body` 超 `DATA_UPLOAD_MAX_MEMORY_SIZE`（默认 2.5MB）会抛
    # `RequestDataTooBig`，它是 `SuspiciousOperation` 子类、**不是** `ValueError`，
    # 原来只 catch `(ValueError, TypeError)` 接不住 → 冒泡成 **HTML 400 错误页**
    # （标题 `RequestDataTooBig at /api/me/password/`，DEBUG 下还带 Traceback）。
    # 实测 3MB 请求体就是这个结果；而同一体积下 `business` 侧已修的接口
    # 返回可读 JSON。前端拿到非 JSON 响应体会 `res.json()` 抛错 → 静默中断。
    data = read_json_body(request)
    if not data and request.POST:
        data = request.POST.dict()

    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')
    confirm_password = data.get('confirm_password', '')

    if not old_password or not new_password:
        return JsonResponse({'success': False, 'message': '请填写原密码与新密码'}, status=400)
    if new_password != confirm_password:
        return JsonResponse({'success': False, 'message': '两次输入的新密码不一致'}, status=400)
    if not request.user.check_password(old_password):
        return JsonResponse({'success': False, 'message': '原密码错误'}, status=400)
    if new_password == old_password:
        return JsonResponse({'success': False, 'message': '新密码不能与原密码相同'}, status=400)

    try:
        validate_password(new_password, request.user)
    except ValidationError as e:
        return JsonResponse({'success': False, 'message': '；'.join(e.messages)}, status=400)

    request.user.set_password(new_password)
    request.user.save(update_fields=['password'])
    # 改密后保持当前会话有效，避免用户被立即登出
    update_session_auth_hash(request, request.user)

    return JsonResponse({'success': True, 'data': None, 'message': '密码修改成功'})
