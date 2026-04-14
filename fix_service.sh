#!/bin/bash
# =============================================================================
# fix_service.sh — Khôi phục cấu hình systemd service đúng cho Bot Dashboard
# Chạy khi bot không phản hồi sau khi file service bị ghi đè thủ công.
# Cách dùng: bash /root/telegram-bot-dashboard/fix_service.sh
# =============================================================================
set -e

APP_DIR="/root/telegram-bot-dashboard"
SERVICE_NAME="bot"
GUNICORN_PORT="5000"

echo "=================================================================="
echo "🔧 KHÔI PHỤC CẤU HÌNH SYSTEMD SERVICE"
echo "=================================================================="

# Đảm bảo file .env tồn tại với BOT_TOKEN
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "⚠️  Không tìm thấy file .env."
    read -p "👉 DÁN BOT TOKEN VÀO ĐÂY RỒI ẤN ENTER: " INPUT_TOKEN
    if [ -z "$INPUT_TOKEN" ]; then
        echo "❌ Không có Bot Token – hủy."
        exit 1
    fi
    read -p "👉 NHẬP TÊN MIỀN (ví dụ: example.com): " INPUT_DOMAIN
    # Write all recognised token variable names so app.py picks it up
    # regardless of which name is checked first.
    echo "TELEGRAM_BOT_TOKEN=$INPUT_TOKEN" > "$APP_DIR/.env"
    echo "TOKEN=$INPUT_TOKEN" >> "$APP_DIR/.env"
    echo "BOT_TOKEN=$INPUT_TOKEN" >> "$APP_DIR/.env"
    echo "APP_BASE_URL=https://$INPUT_DOMAIN" >> "$APP_DIR/.env"
    echo "DOMAIN=https://$INPUT_DOMAIN" >> "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "✅ Đã tạo .env"
else
    echo "✅ File .env đã tồn tại"
    # Ensure all three token variable names are present so app.py always
    # finds the token even if the file was created by an older script.
    _EXISTING_TOKEN=""
    for _VAR in TELEGRAM_BOT_TOKEN TOKEN BOT_TOKEN; do
        # Use sed to extract everything after the first '=' so that tokens
        # containing '=' (e.g. base64 values) are not truncated.
        _EXISTING_TOKEN=$(grep -m1 "^${_VAR}=" "$APP_DIR/.env" | sed 's/^[^=]*=//')
        [ -n "$_EXISTING_TOKEN" ] && break
    done
    if [ -n "$_EXISTING_TOKEN" ]; then
        echo "   (Token tìm thấy)"
        # Back-fill any missing variable names so all three are present.
        grep -q "^TELEGRAM_BOT_TOKEN=" "$APP_DIR/.env" || \
            echo "TELEGRAM_BOT_TOKEN=$_EXISTING_TOKEN" >> "$APP_DIR/.env"
        grep -q "^TOKEN=" "$APP_DIR/.env" || \
            echo "TOKEN=$_EXISTING_TOKEN" >> "$APP_DIR/.env"
        grep -q "^BOT_TOKEN=" "$APP_DIR/.env" || \
            echo "BOT_TOKEN=$_EXISTING_TOKEN" >> "$APP_DIR/.env"
    else
        echo "⚠️  Không tìm thấy token trong .env – kiểm tra lại!"
    fi
fi

echo ""
echo "Đang ghi lại file systemd service đúng chuẩn..."

cat > /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=Telegram Bot Dashboard (Gunicorn + Bot)
After=network.target

[Service]
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment="PATH=$APP_DIR/venv/bin"
ExecStart=$APP_DIR/venv/bin/gunicorn -w 1 -b 127.0.0.1:$GUNICORN_PORT --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "✅ Service file đã được tái tạo với EnvironmentFile=$APP_DIR/.env"

echo ""
echo "Khởi động lại service..."
systemctl daemon-reload
systemctl restart $SERVICE_NAME
sleep 3

if systemctl is-active --quiet $SERVICE_NAME; then
    echo "✅ Service đang chạy!"
    echo ""
    echo "Xem log để xác nhận bot đã khởi động:"
    journalctl -u $SERVICE_NAME --no-pager -n 20
else
    echo "❌ Service vẫn không chạy được. Xem log:"
    journalctl -u $SERVICE_NAME --no-pager -n 40
    exit 1
fi

echo ""
echo "=================================================================="
echo "🎉 HOÀN TẤT! Hãy nhắn tin cho Bot Telegram để kiểm tra."
echo "   Xem log đầy đủ: journalctl -u $SERVICE_NAME -f"
echo "=================================================================="
