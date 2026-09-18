# TNR 系统部署文档

## 一、新服务器部署步骤（逐项执行）

> **推荐**：直接用 `sudo bash deploy.sh` 一键部署（含 Nginx + Gunicorn + MySQL + Supervisor）。
> 下面是手工部署步骤，适用于已有环境或自定义架构。

```bash
# 1. 拉取代码
git pull origin main        # 或 git clone https://cnb.cool/tnrxy/TNR.git

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量（关键！.env 不随 git 分发，必须手动创建/更新）
cp .env.example .env
# 编辑 .env，至少填写：
#   TNR_AMAP_KEY=高德"Web服务"类型Key（逆地理编码必需）
#   SECRET_KEY=随机长字符串    <-- 注意变量名没有 DJANGO_ 前缀
#   DEBUG=False
#   ALLOWED_HOSTS=服务器IP或域名
# 生成随机密钥：python -c "import secrets; print(secrets.token_urlsafe(50))"
vim .env

# 4. 数据库迁移（经纬度/地址字段在 0006 迁移中，未执行会报"no such column"）
python manage.py migrate

# 5. 收集静态文件 + 确保超级管理员可用
python manage.py collectstatic --noinput
python manage.py ensure_superuser          # 已有启用的超管则跳过，不会覆盖其口令
# 需指定口令：ADMIN_PASSWORD=你的口令 python manage.py ensure_superuser --reset-password

# 6. 启动前自检（生产配置缺失会在这里就报出来，而不是等到线上出问题）
python manage.py check --deploy

# 6.1 数据一致性巡检（只读，不改库；有违规时退出码为 1）
python manage.py check_data_integrity
# 检查「界面上看不出来、但会让某个角色看到错误数据」的隐性问题：
# 账号区县 ≠ 所属机构区县、业务记录区县 ≠ 归属对象区县、捕捉单作废但宠物未作废。
# 有输出时逐条人工确认后再处理，命令本身不会自动修复。

# 7. 按你的部署方式重启（gunicorn/supervisor/systemd 或 runserver）
```

### 关于 `DEBUG=False` 下的「启动即失败」

生产模式下（`DEBUG=False`）以下两项**缺失会导致应用直接启动失败**，这是有意设计：

| 缺失项 | 报错 |
|---|---|
| `SECRET_KEY` | `ImproperlyConfigured: 未配置 SECRET_KEY…` |
| `ALLOWED_HOSTS` | `ImproperlyConfigured: 未配置 ALLOWED_HOSTS…` |

原因：早期版本给 `SECRET_KEY` 内置了一个公开的兜底值、`DEBUG` 默认 `True`、
`ALLOWED_HOSTS` 默认 `*`。三者叠加意味着任何绕过部署脚本的启动方式
（手动 gunicorn、`.env` 丢失）都会**静默降级**成「堆栈全公开 + 所有人共用同一把
公开密钥 + 任意 Host 都接受」，而服务状态看起来完全正常。现在改为缺配置就拒绝启动。

## 二、启用 HTTPS（强烈建议）

nginx 只监听 80，且 Django 在反代后必须显式被告知原始协议，否则
`request.is_secure()` 恒为 `False`，所有 HTTPS 相关判断都会失真。

```bash
apt install certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

拿到证书后编辑 `.env`：

```ini
HTTPS=on
CSRF_TRUSTED_ORIGINS=https://你的域名
```

然后重启应用。`HTTPS=on` 会一并启用 Secure Cookie、HSTS 与强制跳转 HTTPS。

## 三、目录属主（手工部署必查）

应用以 `www-data` 运行，但项目目录通常是 root 建的。**不改属主会导致照片、
合照、签名等上传全部失败，而服务状态看起来完全正常。**

```bash
chown -R root:www-data /opt/tnr
chmod -R u=rwX,g=rX,o= /opt/tnr
chown -R www-data:www-data /opt/tnr/media /opt/tnr/staticfiles
chmod 640 /opt/tnr/.env
```

## 四、验证定位相关接口

```bash
# 登录（用现有账号，如 cy_shelter/123456）
curl -s -c /tmp/c.txt -o /dev/null http://127.0.0.1:8000/login/
CSRF=$(grep csrftoken /tmp/c.txt | awk '{print $7}')
curl -s -b /tmp/c.txt -c /tmp/c.txt -d "username=cy_shelter&password=123456&csrfmiddlewaretoken=$CSRF" \
  -H 'Referer: http://127.0.0.1:8000/login/' -o /dev/null -w '登录: %{http_code}\n' http://127.0.0.1:8000/login/

# 逆地理编码（应返回 success:true 与地址）
curl -s -b /tmp/c.txt 'http://127.0.0.1:8000/api/business/geocode/reverse/?lat=30.25&lng=119.90'

# IP 定位兜底（应返回城市/区级地址）
curl -s -b /tmp/c.txt http://127.0.0.1:8000/api/business/geocode/ip/
```

## 五、部署后自检清单

| 检查项 | 命令 / 方式 | 期望 |
|---|---|---|
| 配置无告警 | `python manage.py check --deploy` | 除 HSTS 子域/预载两项外无警告 |
| 超级管理员可登录 | 访问 `/admin/` 用 `ensure_superuser` 输出的口令 | 能进入后台 |
| 演示账号可用 | 访问 `/login/` 用 `cy_shelter` / `123456` | 能进入捕捉点门户 |
| 照片上传可用 | 捕捉登记里传一张照片 | 上传成功，`media/` 出现文件 |
| 数据隔离生效 | 用 `cy_gov` 访问他区宠物生命周期 | 返回 404 |
| 数据一致性 | `python manage.py check_data_integrity` | 输出「未发现一致性问题」 |
| 定时任务在跑 | `supervisorctl status` | `tnr-qworker` 为 RUNNING |

## 六、常见报错对照

| 报错 | 原因 | 处理 |
|---|---|---|
| `ImproperlyConfigured: 未配置 SECRET_KEY` | `.env` 缺 `SECRET_KEY`（注意不是 `DJANGO_SECRET_KEY`） | 填入随机长字符串后重启 |
| `ImproperlyConfigured: 未配置 ALLOWED_HOSTS` | `.env` 缺 `ALLOWED_HOSTS` | 填入域名/IP 后重启 |
| `未配置地图服务Key，请在 .env 中设置 TNR_AMAP_KEY` | 新服务器 `.env` 缺 Key（.env 不入 git） | 编辑 `.env` 填入 Key 后**重启服务** |
| `地图服务返回错误：INVALID_USER_KEY` | Key 拼写错误或不是"Web服务"类型 | 高德控制台核对 Key 及其绑定服务平台 |
| `地图服务返回错误：USERKEY_PLAT_NOMATCH (10009)` | Key 绑定的是"Web端(JS API)"而非"Web服务" | 同一应用下添加"Web服务"类型 Key |
| `地图服务请求失败：...timeout/Connection refused` | 服务器出网被墙/无外网 | 开放对 `restapi.amap.com:443` 的出网访问 |
| 前端定位报 `Only secure origins are allowed` | HTTP 非安全源，浏览器禁止 GPS 定位 | 正常现象，前端已自动降级 IP 定位（城市/区级）；如需精确定位请配 HTTPS |
| `no such column: business_capture.latitude` | 数据库迁移未执行 | `python manage.py migrate` |
| 上传照片报 500 / Permission denied | `media/` 属主不是 `www-data` | 见第三节「目录属主」 |
| 提交表单报 403 CSRF | 通过域名访问但未配 `CSRF_TRUSTED_ORIGINS` | 按第二节填入 `https://域名` |
