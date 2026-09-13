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
DB_PASS="${DB_PASS:-ChangeMeInProduction!}"  # 建议通过环境变量覆盖: DB_PASS=xxx

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
DEBUG=False
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
ALLOWED_HOSTS=$DOMAIN,localhost,127.0.0.1,100.99.98.71
DB_ENGINE=mysql
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASS
DB_HOST=127.0.0.1
DB_PORT=3306
EOF

# === 6. Django 初始化 ===
echo "[6/8] 初始化 Django..."
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py seed_data
# 创建超级用户（如不存在）
python manage.py shell -c "
from accounts.models import User
if not User.objects.filter(username='admin').exists():
    u = User.objects.create_superuser('admin', 'admin@tnr.local', 'admin123456', role='gov_city')
    print('Superuser created: admin / admin123456')
else:
    print('Superuser already exists')
"

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
echo "超级管理员: admin / admin123456"
echo ""
echo "演示账号（由 seed_data 创建，密码均为 123456，详见 DEMO_ACCOUNTS.md）:"
echo "  政府 admin / 捕捉点 cy_shelter / 医院 aixin_hosp / 领养人 adopter1"
echo ""
echo "常用命令:"
echo "  查看状态: supervisorctl status"
echo "  重启应用: supervisorctl restart tnr-gunicorn"
echo "  查看日志: tail -f /var/log/tnr/gunicorn.log"
echo "  MySQL:   mysql -u $DB_USER -p $DB_NAME"
echo ""
echo "SSL配置（推荐）:"
echo "  apt install certbot python3-certbot-nginx"
echo "  certbot --nginx -d $DOMAIN"
echo ""
