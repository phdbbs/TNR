============================================
  TNR 流浪动物管理系统 - 部署指南
============================================

【系统要求】
  - 操作系统: Ubuntu 22.04+ / Debian 12+
  - 服务器配置: 2核4G VPS（推荐）
  - 已安装: sudo 权限账户、可访问互联网
  - 软件栈: Nginx + Gunicorn + MySQL + Supervisor

【部署步骤】
  1. 上传项目代码到服务器（任意目录）
     例如: scp -r solo/ user@server:/tmp/tnr-code/

  2. 进入项目目录并执行一键部署脚本
     cd /tmp/tnr-code/solo
     sudo bash deploy.sh

  3. 脚本会自动完成以下步骤:
     - 安装系统依赖 (python3, nginx, supervisor, mysql-server 等)
     - 复制项目到 /opt/tnr
     - 创建 Python 虚拟环境并安装依赖
     - 配置 MySQL 数据库 (utf8mb4)
     - 生成 .env 配置文件
     - 执行数据库迁移、收集静态文件、初始化种子数据
     - 创建超级管理员账号
     - 配置并启动 Supervisor (gunicorn + qworker)
     - 配置并重载 Nginx

  4. 修改 deploy.sh 中的配置变量以适配你的环境:
     - DOMAIN: 改为你的实际域名
     - DB_PASS: 修改 MySQL 密码
     - DB_USER / DB_NAME: 视需要调整

【默认账号】
  超级管理员: admin / admin123456
  首次登录后请立即修改密码！

【常用运维命令】
  # 查看服务状态
  supervisorctl status

  # 重启应用服务
  supervisorctl restart tnr-gunicorn
  supervisorctl restart tnr-qworker

  # 重启所有服务
  supervisorctl restart tnr-gunicorn tnr-qworker

  # 重载 Nginx 配置
  sudo nginx -t && sudo systemctl reload nginx

  # 查看应用日志
  tail -f /var/log/tnr/gunicorn.log
  tail -f /var/log/tnr/gunicorn-error.log

  # 查看队列 worker 日志
  tail -f /var/log/tnr/qworker.log

  # 进入 MySQL
  mysql -u tnr -p tnr_system

  # 进入项目目录激活虚拟环境
  cd /opt/tnr
  source venv/bin/activate

  # Django 管理命令
  python manage.py migrate
  python manage.py collectstatic --noinput
  python manage.py createsuperuser
  python manage.py shell

【项目目录结构】
  /opt/tnr/                   # 项目根目录
    ├── venv/                 # Python 虚拟环境
    ├── staticfiles/          # collectstatic 输出目录
    ├── media/                # 用户上传文件
    ├── .env                  # 环境变量配置
    ├── gunicorn.conf.py      # Gunicorn 配置
    └── manage.py             # Django 管理入口

  /etc/nginx/sites-available/tnr   # Nginx 配置
  /etc/supervisor/conf.d/tnr.conf  # Supervisor 配置
  /var/log/tnr/                    # 日志目录

【SSL 配置建议（推荐）】
  使用 Let's Encrypt 免费 SSL 证书:

  1. 安装 certbot:
     sudo apt install certbot python3-certbot-nginx

  2. 申请并自动配置 SSL:
     sudo certbot --nginx -d your-domain.com

  3. 测试自动续期:
     sudo certbot renew --dry-run

  证书默认 90 天到期，certbot 会自动续期。
  配置完成后访问地址将变为 https://your-domain.com

【备份建议】
  # 备份数据库
  mysqldump -u tnr -p tnr_system > tnr_backup_$(date +%Y%m%d).sql

  ⚠⚠ 【2026-09-22 实测补充】上面这条命令只适用于「MySQL 与站点装在同一台机器」
     的那种部署（即本 README 配套的 deploy.sh 场景）。
     **当前生产不是这种形态** —— 库是 Docker 容器里的 MariaDB，
     而宿主机上的客户端是 Oracle 版 mysql-client 8.0，两者不兼容：
         mysqldump: Couldn't execute 'SELECT ... JSON_EXTRACT(HISTOGRAM, ...)
                     FROM information_schema.COLUMN_STATISTICS ...':
                     Unknown table 'COLUMN_STATISTICS' in information_schema (1109)
     它在**导出第一张表之后就 exit=2 退出**，而输出文件已经写出来了 ——
     如果只 `> 文件` 不检查退出码，会得到一份**看着有内容、实际只有 1 张表**的
     备份，比彻底失败更危险。
     实测对比：坏备份 2KB / 1 张表 vs 正确备份 220KB / 33 张表。

     当前生产请改用**容器内的 mariadb-dump**（版本与服务端一致）：
         docker exec 1Panel-mariadb-LQ69 mariadb-dump -utnr -p<PASS> \
             --single-transaction --routines --triggers tnr_system > backup.sql
     完整可用脚本见 `DEPLOY.md` 的「该服务器的更新方式」一节。

  # 备份媒体文件
  tar -czf media_backup_$(date +%Y%m%d).tar.gz /opt/tnr/media/

  ⚠ 代码包同理要留神：`tar ... -C /opt opt/tnr` 是**错的**（等于 /opt/opt/tnr），
    tar 会 exit=2 并产出 45 字节的空包；正确写法是 `-C / opt/tnr`。
    归档后必须做**大小闸门**（正常 8MB 量级），否则「备份成功」只是句空话。

  建议配置 crontab 定时备份并同步到对象存储。

【故障排查】
  - 502 Bad Gateway: 检查 gunicorn 是否运行
    supervisorctl status tnr-gunicorn
    tail -f /var/log/tnr/gunicorn-error.log

  - 静态文件 404: 重新收集静态文件
    cd /opt/tnr && source venv/bin/activate
    python manage.py collectstatic --noinput

  - 数据库连接失败: 检查 .env 中的 DB_* 配置
    cat /opt/tnr/.env

  - 权限问题: 确保 www-data 可访问项目目录
    chown -R www-data:www-data /opt/tnr/media
    chown -R www-data:www-data /opt/tnr/staticfiles

============================================
