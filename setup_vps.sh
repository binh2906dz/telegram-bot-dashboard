#!/bin/bash
# =============================================================================
# setup_vps.sh — Cài đặt hoàn chỉnh Web Stack cho Telegram Bot Dashboard
# Dán toàn bộ script này vào Termius rồi nhấn Enter.
# =============================================================================
set -e

REPO_URL="https://github.com/binh2906dz/telegram-bot-dashboard.git"
REPO_BRANCH="setup-local-streaming"
APP_DIR="/root/telegram-bot-dashboard"
DOMAIN="ngeyhsvge683874.online"
GUNICORN_PORT="5000"
SERVICE_NAME="bot"

echo "=================================================================="
echo "🚀 BẮT ĐẦU CÀI ĐẶT HỆ THỐNG WEB DASHBOARD LÊN MÁY CHỦ"
echo "=================================================================="

# ------------------------------------------------------------------
# BƯỚC 1 — Cập nhật danh sách gói và cài đặt các phụ thuộc hệ thống
# ------------------------------------------------------------------
echo ""
echo "[1/6] Cập nhật apt và cài đặt nginx, python3-venv, python3-pip, ffmpeg..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y nginx python3-pip python3-venv ffmpeg git ufw

# Kiểm tra nginx đã cài thành công chưa
if ! command -v nginx &>/dev/null; then
    echo "❌ LỖI: nginx không cài được."
    echo "   Nguyên nhân có thể: lỗi kết nối mạng, hết dung lượng đĩa, hoặc kho apt bị lỗi."
    echo "   Thử chạy thủ công: apt-get install -y nginx"
    exit 1
fi
echo "✅ nginx đã được cài đặt: $(nginx -v 2>&1)"

# Đảm bảo nginx đang chạy
systemctl enable nginx
systemctl start nginx || true

# ------------------------------------------------------------------
# BƯỚC 2 — Clone / cập nhật mã nguồn từ GitHub
# ------------------------------------------------------------------
echo ""
echo "[2/6] Tải mã nguồn từ GitHub (nhánh: $REPO_BRANCH)..."
if [ -d "$APP_DIR/.git" ]; then
    echo "  Thư mục đã tồn tại — đang kéo bản mới nhất..."
    cd "$APP_DIR"
    git fetch origin
    git checkout "$REPO_BRANCH"
    git pull origin "$REPO_BRANCH"
else
    rm -rf "$APP_DIR"
    git clone -b "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
echo "✅ Mã nguồn đã sẵn sàng tại $APP_DIR"

# ------------------------------------------------------------------
# BƯỚC 3 — Thiết lập môi trường Python
# ------------------------------------------------------------------
echo ""
echo "[3/6] Tạo môi trường Python (venv) và cài đặt thư viện..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
pip install gunicorn --quiet
deactivate
echo "✅ Môi trường Python đã sẵn sàng"

# ------------------------------------------------------------------
# BƯỚC 4 — Tạo file .env nếu chưa có
# ------------------------------------------------------------------
echo ""
if [ -f "$APP_DIR/.env" ]; then
    echo "[4/6] File .env đã tồn tại — bỏ qua bước nhập Bot Token."
else
    echo "[4/6] Chưa có file .env. Vui lòng nhập Bot Token của bạn."
    echo "======================================"
    read -p "👉 DÁN BOT TOKEN VÀO ĐÂY RỒI ẤN ENTER: " BOT_TOKEN
    echo "======================================"
    if [ -z "$BOT_TOKEN" ]; then
        echo "⚠️  Cảnh báo: Không nhập Bot Token. Bạn có thể tạo file .env thủ công sau."
        echo "TOKEN=" > "$APP_DIR/.env"
    else
        echo "TOKEN=$BOT_TOKEN" > "$APP_DIR/.env"
    fi
    echo "APP_BASE_URL=https://$DOMAIN" >> "$APP_DIR/.env"
    echo "DOMAIN=https://$DOMAIN" >> "$APP_DIR/.env"
    # Bảo vệ file .env khỏi người dùng khác (chứa thông tin nhạy cảm)
    chmod 600 "$APP_DIR/.env"
    echo "✅ File .env đã được tạo"
fi

# ------------------------------------------------------------------
# BƯỚC 5 — Cấu hình Nginx
# ------------------------------------------------------------------
echo ""
echo "[5/6] Cấu hình Nginx cho tên miền $DOMAIN..."

cat > /etc/nginx/sites-available/$SERVICE_NAME <<EOF
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    # Tăng giới hạn upload file (50 MB)
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:$GUNICORN_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
    }

    # Phục vụ file video HLS trực tiếp (tăng hiệu suất)
    # Access-Control-Allow-Origin * cần thiết để Telegram Mini App phát HLS từ domain khác
    location /static/uploads/ {
        alias $APP_DIR/static/uploads/;
        add_header Access-Control-Allow-Origin *;
        add_header Cache-Control "public, max-age=3600";
        types {
            application/vnd.apple.mpegurl m3u8;
            video/mp2t               ts;
            video/mp4                mp4;
        }
    }
}
EOF

# Kích hoạt site và tắt site mặc định
ln -sf /etc/nginx/sites-available/$SERVICE_NAME /etc/nginx/sites-enabled/$SERVICE_NAME
rm -f /etc/nginx/sites-enabled/default

# Kiểm tra cấu hình nginx trước khi reload
nginx -t
systemctl reload nginx
echo "✅ Nginx đã được cấu hình và reload thành công"

# ------------------------------------------------------------------
# BƯỚC 6 — Tạo systemd service cho Gunicorn
# ------------------------------------------------------------------
echo ""
echo "[6/6] Cấu hình dịch vụ hệ thống (systemd) cho Gunicorn và Bot..."

cat > /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=Telegram Bot Dashboard (Gunicorn)
After=network.target

[Service]
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment="PATH=$APP_DIR/venv/bin"
ExecStart=$APP_DIR/venv/bin/gunicorn -w 2 -b 127.0.0.1:$GUNICORN_PORT --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Service riêng cho tiến trình Bot Telegram (run_bot.py)
cat > /etc/systemd/system/${SERVICE_NAME}-worker.service <<EOF
[Unit]
Description=Telegram Bot Worker (python-telegram-bot polling)
After=network.target ${SERVICE_NAME}.service

[Service]
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment="PATH=$APP_DIR/venv/bin"
ExecStart=$APP_DIR/venv/bin/python run_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl restart $SERVICE_NAME
systemctl enable ${SERVICE_NAME}-worker
systemctl restart ${SERVICE_NAME}-worker

# Chờ 3 giây để service khởi động
sleep 3

# Kiểm tra trạng thái service
if systemctl is-active --quiet $SERVICE_NAME; then
    echo "✅ Dịch vụ $SERVICE_NAME (Gunicorn) đang chạy thành công"
else
    echo "❌ Dịch vụ $SERVICE_NAME KHÔNG chạy được. Xem log bên dưới:"
    journalctl -u $SERVICE_NAME --no-pager -n 30
    exit 1
fi

if systemctl is-active --quiet ${SERVICE_NAME}-worker; then
    echo "✅ Dịch vụ ${SERVICE_NAME}-worker (Bot Telegram) đang chạy thành công"
else
    echo "⚠️  Dịch vụ ${SERVICE_NAME}-worker chưa sẵn sàng (có thể đang khởi động). Xem log:"
    journalctl -u ${SERVICE_NAME}-worker --no-pager -n 10
fi

# ------------------------------------------------------------------
# MỞ CỔNG UFW (Tường lửa)
# ------------------------------------------------------------------
echo ""
echo "🔓 Mở các cổng tường lửa (UFW)..."
# Các lệnh allow PHẢI chạy trước --force enable để không bị mất SSH
ufw allow 22/tcp   comment 'SSH'
ufw allow 80/tcp   comment 'HTTP'
ufw allow 443/tcp  comment 'HTTPS'
ufw --force enable
echo "✅ UFW đã được cấu hình"

# ------------------------------------------------------------------
# TỔNG KẾT
# ------------------------------------------------------------------
echo ""
echo "=================================================================="
echo "🎉 HOÀN TẤT! HỆ THỐNG WEB CỦA BẠN ĐÃ ONLINE!"
echo "------------------------------------------------------------------"
echo "  Tên miền  : https://$DOMAIN"
echo "  Trạng thái Gunicorn : $(systemctl is-active $SERVICE_NAME)"
echo "  Trạng thái Bot      : $(systemctl is-active ${SERVICE_NAME}-worker)"
echo "  Trạng thái Nginx    : $(systemctl is-active nginx)"
echo "------------------------------------------------------------------"
echo "  Xem log Web   : journalctl -u $SERVICE_NAME -f"
echo "  Xem log Bot   : journalctl -u ${SERVICE_NAME}-worker -f"
echo "  Khởi động lại : systemctl restart $SERVICE_NAME ${SERVICE_NAME}-worker"
echo "=================================================================="
