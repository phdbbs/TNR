# TNR 系统部署文档

## 一、新服务器部署步骤（逐项执行）

```bash
# 1. 拉取代码
git pull origin main        # 或 git clone https://cnb.cool/tnrxy/TNR.git

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量（关键！.env 不随 git 分发，必须手动创建/更新）
cp .env.example .env
# 编辑 .env，至少填写：
#   TNR_AMAP_KEY=高德"Web服务"类型Key（逆地理编码必需）
#   DJANGO_SECRET_KEY=随机长字符串
#   DEBUG=False
#   ALLOWED_HOSTS=服务器IP或域名
vim .env

# 4. 数据库迁移（经纬度/地址字段在 0006 迁移中，未执行会报"no such column"）
python manage.py migrate

# 5. 收集静态文件 + 重启服务
python manage.py collectstatic --noinput
# 按你的部署方式重启（gunicorn/supervisor/systemd 或 runserver）
```

## 二、验证定位相关接口

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

## 三、常见报错对照

| 报错 | 原因 | 处理 |
|---|---|---|
| `未配置地图服务Key，请在 .env 中设置 TNR_AMAP_KEY` | 新服务器 `.env` 缺 Key（.env 不入 git） | 编辑 `.env` 填入 Key 后**重启服务** |
| `地图服务返回错误：INVALID_USER_KEY` | Key 拼写错误或不是"Web服务"类型 | 高德控制台核对 Key 及其绑定服务平台 |
| `地图服务返回错误：USERKEY_PLAT_NOMATCH (10009)` | Key 绑定的是"Web端(JS API)"而非"Web服务" | 同一应用下添加"Web服务"类型 Key |
| `地图服务请求失败：...timeout/Connection refused` | 服务器出网被墙/无外网 | 开放对 `restapi.amap.com:443` 的出网访问 |
| 前端定位报 `Only secure origins are allowed` | HTTP 非安全源，浏览器禁止 GPS 定位 | 正常现象，前端已自动降级 IP 定位（城市/区级）；如需精确定位请配 HTTPS |
| `no such column: business_capture.latitude` | 数据库迁移未执行 | `python manage.py migrate` |
