#!/bin/bash
# ============================================
# TNR 流浪动物管理系统 - 本地调试一键启动脚本
#
# 【重要经验】每次部署/调试启动后，必须确保以下演示账号可用：
#   统一密码: 123456
#   admin       - 市级政府管理员（全市）
#   cy_gov      - 襄城区政府管理员
#   hd_gov      - 樊城区政府管理员
#   cy_shelter  - 捕捉点操作员（襄城流浪动物捕捉点）
#   hd_shelter  - 捕捉点操作员（樊城流浪动物捕捉点）
#   aixin_hosp  - 医院操作员（爱心宠物医院）
#   ruipeng_hosp- 医院操作员（瑞鹏宠物医院）
#   babitang_hosp- 医院操作员（芭比堂动物医院）
#   adopter1    - 领养人
#
# 以上账号由 `python manage.py seed_data` 创建。该命令**幂等**：区县/机构按
# 稳定编号匹配，业务记录按业务单号匹配，重复执行不会产生重复数据；
# 同时每次都会把演示账号的角色/归属/启用状态校准回上表（被停用也能自愈），
# 但不会重置密码。
#
# 用法: bash start_dev.sh [端口]   默认端口 8000
# ============================================

set -e
cd "$(dirname "$0")"

PORT="${1:-8000}"
VENV=".venv"

echo "============================================"
echo "  TNR 流浪动物管理系统 - 调试启动"
echo "============================================"

# === 1. Python 虚拟环境 ===
if [ ! -d "$VENV" ]; then
    echo "[1/4] 创建虚拟环境..."
    python3 -m venv "$VENV"
fi
PY="$VENV/bin/python"

# === 2. 安装依赖 ===
# 开发环境使用 sqlite3，mysqlclient 仅生产部署需要，这里跳过
if ! "$PY" -c "import django" 2>/dev/null; then
    echo "[2/4] 安装依赖..."
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q Django==5.0.6 djangorestframework==3.15.1 \
        Pillow==12.3.0 python-dotenv==1.0.1 django-q2==1.6.2 gunicorn==22.0.0
else
    echo "[2/4] 依赖已就绪，跳过安装"
fi

# === 3. 环境变量 ===
if [ ! -f .env ]; then
    echo "[3/4] 生成 .env（开发配置，sqlite3）..."
    cp .env.example .env
else
    echo "[3/4] .env 已存在，跳过"
fi

# === 4. 数据库迁移 + 演示账号种子数据（幂等） ===
echo "[4/4] 迁移数据库并确保演示账号存在..."
"$PY" manage.py migrate --noinput
"$PY" manage.py seed_data   # 幂等：已存在则跳过，并校准演示账号归属/启用状态

echo ""
echo "============================================"
echo "  启动开发服务器: http://127.0.0.1:$PORT"
echo "  演示账号（密码均为 123456）:"
echo "    政府 admin / 捕捉点 cy_shelter / 医院 aixin_hosp / 领养人 adopter1"
echo "============================================"
echo ""

exec "$PY" manage.py runserver "0.0.0.0:$PORT"
