#!/bin/bash
# ============================================
# TNR 流浪动物管理系统 - 一键部署脚本
# 适用: Ubuntu 22.04+ / Debian 12+ VPS (推荐 2核4G)
# 部署架构: Nginx + Gunicorn + MySQL + Supervisor
# 用法: sudo bash deploy.sh
# ============================================

set -e

# === 配置区 ===
PROJECT_NAME="tnr_system"
PROJECT_DIR="/opt/tnr"
DOMAIN="${DOMAIN:-100.99.98.71}"  # 默认使用服务器IP，可改为域名: DOMAIN=example.com sudo bash deploy.sh
PYTHON_VERSION="python3"
DB_NAME="tnr_system"
DB_USER="tnr"

# 数据库口令：未指定则自动生成随机口令（不再内置公开的弱默认值）。
# 注意只允许安全字符集——该口令会被拼进 SQL 语句和 .env，
# 含 ' " $ \ ` 空格等字符会破坏解析。
if [ -z "${DB_PASS:-}" ]; then
    DB_PASS="$($PYTHON_VERSION -c 'import secrets; print(secrets.token_urlsafe(18))')"
    DB_PASS_GENERATED=1
else
    DB_PASS_GENERATED=0
fi
if ! [[ "$DB_PASS" =~ ^[A-Za-z0-9_.@%+=-]+$ ]]; then
    echo "错误：DB_PASS 含不安全字符（仅允许字母数字与 _ . @ % + = -）。"
    echo "      该值会被拼进 SQL 与 .env，特殊字符会导致解析出错。"
    echo "      建议直接不设置 DB_PASS，由脚本自动生成。"
    exit 1
fi

# 超级管理员口令：未指定则自动生成（仅在新建超级管理员时使用）。
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

echo "============================================"
echo "  TNR 流浪动物管理系统 - 部署脚本"
echo "============================================"

# === 1. 系统依赖 ===
echo "[1/8] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev build-essential \
    libmysqlclient-dev nginx supervisor mysql-server pkg-config > /dev/null

# === 2. 创建项目目录 ===
echo "[2/8] 创建项目目录..."
mkdir -p $PROJECT_DIR
# 假设当前目录是项目代码，复制到部署目录
if [ "$(pwd)" != "$PROJECT_DIR" ]; then
    cp -r . "$PROJECT_DIR/"
    # 清掉不该进部署目录的本地痕迹：.git 体积大且会泄漏历史，
    # db.sqlite3 是本地开发库（生产用 MySQL），.venv 是 macOS 虚拟环境，
    # server.log 是本地日志。留着只会让排障时误判。
    rm -rf "$PROJECT_DIR/.git" "$PROJECT_DIR/.venv" "$PROJECT_DIR/db.sqlite3" \
           "$PROJECT_DIR/server.log" "$PROJECT_DIR/.workbuddy-ai" \
           "$PROJECT_DIR/gui-test-screenshots"
fi
cd "$PROJECT_DIR"

# === 3. Python 虚拟环境 ===
echo "[3/8] 创建 Python 虚拟环境..."
$PYTHON_VERSION -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# === 4. MySQL 数据库 ===
echo "[4/8] 配置 MySQL 数据库..."
mysql -e "CREATE DATABASE IF NOT EXISTS $DB_NAME CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -e "CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';"
mysql -e "GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_USER'@'localhost';"
mysql -e "FLUSH PRIVILEGES;"

# === 5. 环境变量 ===
echo "[5/8] 生成 .env..."
cat > "$PROJECT_DIR/.env" <<EOF
# 由 deploy.sh 生成于 $(date '+%Y-%m-%d %H:%M:%S')
# 本文件含密钥，请勿提交到版本库（已在 .gitignore 中）。
DEBUG=False
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
ALLOWED_HOSTS=$DOMAIN,localhost,127.0.0.1
DB_ENGINE=mysql
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASS
DB_HOST=127.0.0.1
DB_PORT=3306

# HTTPS 开关。执行完 certbot 申请到证书后改为 on 并重启应用，
# 会一并启用 Secure Cookie / HSTS / 强制跳转 HTTPS。
HTTPS=off
# 站点通过域名访问时，Django 会校验 Origin/Referer；未列入的域名提交表单会 403。
# 申请证书后把 https://$DOMAIN 填进来，多个用逗号分隔。
CSRF_TRUSTED_ORIGINS=
LOG_LEVEL=INFO

# 地图服务（高德开放平台 https://lbs.amap.com 申请 Web 服务类型 Key）
# 用于捕捉登记「定位」功能的逆地理编码，必须配置否则定位不可用。
TNR_AMAP_KEY=
EOF
# .env 含密钥，收紧权限（gunicorn 以 www-data 运行，故属组给 www-data）
chown root:www-data "$PROJECT_DIR/.env"
chmod 640 "$PROJECT_DIR/.env"

# === 6. Django 初始化 ===
echo "[6/8] 初始化 Django..."
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py seed_data

# 超级管理员。
#
# 【历史缺陷】这里原本写的是「若 admin 不存在则创建超级用户」，但上一步
# seed_data 已经建了一个**非超级用户**的 admin（市级政府管理员），于是
# 判断恒为假、分支永不执行，而脚本结尾却提示「超级管理员: admin / admin123456」
# ——运维照提示登录必然失败。
#
# 现在交由 `manage.py ensure_superuser` 处理（判断的是「是否已有启用的超级管理员」
# 这一真正的业务条件），并且可以单独写单测覆盖。
# 已有超级管理员则不动口令，避免每次重新部署都覆盖运维改过的密码。
ADMIN_OUTPUT="$(python manage.py ensure_superuser --username admin)"
echo "$ADMIN_OUTPUT" | grep -v "^ADMIN_CREDENTIALS=" | grep -v "^ADMIN_RESULT="
ADMIN_CREDENTIALS="$(echo "$ADMIN_OUTPUT" | grep "^ADMIN_CREDENTIALS=" | tail -n 1)"
if [ -n "$ADMIN_CREDENTIALS" ]; then
    ADMIN_SUMMARY="超级管理员: ${ADMIN_CREDENTIALS#ADMIN_CREDENTIALS=}  （仅本次显示，请立即保存；已覆盖演示口令 123456）"
else
    ADMIN_SUMMARY="超级管理员: 已存在，口令未改动（重置：ADMIN_PASSWORD=新口令 重跑本脚本）"
fi
echo "  $ADMIN_SUMMARY"

# === 6.5 目录属主与权限 ===
# 【历史缺陷】原脚本全程没有 chown。gunicorn / qcluster 以 www-data 运行，
# 但 /opt/tnr 是 root 建的 → media/ 不可写，捕捉照片、合照、签名上传全部失败，
# 而服务状态看起来完全正常。
echo "[6.5/8] 设置目录属主与权限..."
mkdir -p "$PROJECT_DIR/media" "$PROJECT_DIR/staticfiles" /var/log/tnr
# 代码归 root、属组 www-data 只读：应用被攻破也无法改写自己的代码
chown -R root:www-data "$PROJECT_DIR"
chmod -R u=rwX,g=rX,o= "$PROJECT_DIR"
# 上传目录与静态目录需要 www-data 可写
chown -R www-data:www-data "$PROJECT_DIR/media" "$PROJECT_DIR/staticfiles"
# .env 含密钥，保持 root 可写、www-data 只读
chmod 640 "$PROJECT_DIR/.env"

# === 7. Supervisor 配置 ===
echo "[7/8] 配置 Supervisor..."
cat > /etc/supervisor/conf.d/tnr.conf <<EOF
[program:tnr-gunicorn]
command=$PROJECT_DIR/venv/bin/gunicorn --config $PROJECT_DIR/gunicorn.conf.py tnr_system.wsgi:application
directory=$PROJECT_DIR
user=www-data
autostart=true
autorestart=true
stdout_logfile=/var/log/tnr/gunicorn.log
stderr_logfile=/var/log/tnr/gunicorn-error.log
environment=DJANGO_SETTINGS_MODULE="tnr_system.settings"

[program:tnr-qworker]
command=$PROJECT_DIR/venv/bin/python manage.py qcluster
directory=$PROJECT_DIR
user=www-data
autostart=true
autorestart=true
stdout_logfile=/var/log/tnr/qworker.log
stderr_logfile=/var/log/tnr/qworker-error.log
EOF
mkdir -p /var/log/tnr
supervisorctl reread
supervisorctl update
supervisorctl restart tnr-gunicorn tnr-qworker

# === 8. Nginx 配置 ===
echo "[8/8] 配置 Nginx..."
cat > /etc/nginx/sites-available/tnr <<'EOF'
server {
    listen 80;
    server_name _;  # 修改为你的域名

    # 静态文件
    location /static/ {
        alias /opt/tnr/staticfiles/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    # 媒体文件（上传的照片等）
    location /media/ {
        alias /opt/tnr/media/;
        expires 7d;
    }

    # 反向代理到 Gunicorn
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_redirect off;
        client_max_body_size 20M;  # 允许上传大文件（照片）
    }
}
EOF
ln -sf /etc/nginx/sites-available/tnr /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo ""
echo "============================================"
echo "  部署完成！"
echo "============================================"
echo ""
echo "访问地址: http://$DOMAIN"
echo "$ADMIN_SUMMARY"
echo ""
echo "演示账号（由 seed_data 创建，密码均为 123456，详见 DEMO_ACCOUNTS.md）:"
echo "  政府 admin / 捕捉点 cy_shelter / 医院 aixin_hosp / 领养人 adopter1"
if [ -n "$ADMIN_CREDENTIALS" ]; then
    echo "  注意：admin 的密码已被上面的超级管理员口令覆盖，不再是 123456。"
fi
if [ "$DB_PASS_GENERATED" = "1" ]; then
    echo ""
    echo "数据库口令（本次自动生成，请保存）: $DB_PASS"
fi
echo ""
echo "常用命令:"
echo "  查看状态: supervisorctl status"
echo "  重启应用: supervisorctl restart tnr-gunicorn"
echo "  查看日志: tail -f /var/log/tnr/gunicorn.log"
echo "  MySQL:   mysql -u $DB_USER -p $DB_NAME"
echo ""
echo "还需手动完成两件事:"
echo "  1) 配置高德地图 Key —— 编辑 $PROJECT_DIR/.env 填入 TNR_AMAP_KEY，"
echo "     否则捕捉登记的「定位」功能不可用；改完 supervisorctl restart tnr-gunicorn"
echo "  2) 配置 HTTPS（推荐）:"
echo "       apt install certbot python3-certbot-nginx"
echo "       certbot --nginx -d $DOMAIN"
echo "     拿到证书后把 .env 里的 HTTPS=off 改为 HTTPS=on、"
echo "     CSRF_TRUSTED_ORIGINS 填 https://$DOMAIN，再重启应用。"
echo ""
