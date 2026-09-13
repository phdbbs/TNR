from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from accounts.models import User


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
