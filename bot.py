import json, os, asyncio, datetime, sqlite3, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import Forbidden, TelegramError

TOKEN = os.getenv("TOKEN", "")
_admin_str = os.getenv("ADMIN_ID", "")
ADMIN_ID = int(_admin_str) if _admin_str.isdigit() else None

# Base URL for Mini App bridge links.  Falls back through several common env
# var names so the deployment environment can use whichever it prefers.
WEBHOOK_URL = (
    os.getenv("APP_BASE_URL")
    or os.getenv("DOMAIN")
    or os.getenv("WEBHOOK_URL")
    or ""
).rstrip("/")

# Resolve the DB file relative to this script's directory so bot.py can be
# run from any working directory and still find the shared SQLite database.
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_BOT_DIR, "data.db")

# Number of items per page in Telegram inline keyboard menus
_PAGE_SIZE = 8


def _get_db():
    """Return a thread-safe SQLite connection to the shared data.db."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _db_get_albums() -> dict:
    try:
        conn = _get_db()
        rows = conn.execute("SELECT id, data_json FROM albums").fetchall()
        conn.close()
        return {row["id"]: json.loads(row["data_json"]) for row in rows}
    except Exception:
        return {}


def _db_get_categories() -> list:
    """Return all categories as list of dicts [{id, name}], sorted by name.
    Gracefully returns [] if the categories table does not exist yet.
    """
    try:
        conn = _get_db()
        rows = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
        conn.close()
        return [{"id": row["id"], "name": row["name"]} for row in rows]
    except Exception:
        return []


def _db_get_config() -> dict:
    try:
        conn = _get_db()
        row = conn.execute("SELECT value FROM app_config WHERE key='config'").fetchone()
        conn.close()
        if row:
            return json.loads(row["value"])
        return {"hour": 0, "minute": 0}
    except Exception:
        return {"hour": 0, "minute": 0}


def _db_save_config(cfg: dict):
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO app_config (key, value) VALUES ('config', ?)",
        (json.dumps(cfg, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()


def _db_get_subscribers() -> list:
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT user_id FROM subscribers WHERE bot_id='global'"
        ).fetchall()
        conn.close()
        return [row["user_id"] for row in rows]
    except Exception:
        return []


def _db_get_all_target_ids() -> list:
    """Return deduplicated list of all target IDs: all subscribers + all active IDs."""
    ids: set = set()
    try:
        conn = _get_db()
        # All subscribers (people who pressed /start)
        rows = conn.execute("SELECT DISTINCT user_id FROM subscribers").fetchall()
        for row in rows:
            ids.add(int(row["user_id"]))
        # All IDs with status='active' in telegram_ids
        rows = conn.execute(
            "SELECT user_id FROM telegram_ids WHERE status = 'active'"
        ).fetchall()
        for row in rows:
            try:
                ids.add(int(row["user_id"]))
            except (ValueError, TypeError):
                pass
        conn.close()
    except Exception:
        pass
    return list(ids)


def _db_mark_blocked(user_id: int) -> None:
    """Remove a blocked user from subscribers and set their telegram_ids status to 'blocked'.

    Note: subscribers.user_id is INTEGER; telegram_ids.user_id is TEXT — both
    conversions below are intentional to match their respective column types.
    """
    try:
        conn = _get_db()
        conn.execute("DELETE FROM subscribers WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE telegram_ids SET status='blocked' WHERE user_id=?",
            (str(user_id),),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _db_add_subscriber(user_id: int):
    try:
        conn = _get_db()
        conn.execute(
            "INSERT OR IGNORE INTO subscribers (bot_id, user_id) VALUES ('global', ?)",
            (user_id,),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ================= START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    _db_add_subscriber(user_id)

    m1 = await update.message.reply_text("🚀 NƠI BÓNG TỐI BẮT ĐẦU... NƠI BẢN NĂNG THỨC TỈNH 🚀")
    await asyncio.sleep(1)
    m2 = await update.message.reply_text("👿 KHÔNG DÀNH CHO NGƯỜI YẾU TIM - CHỈ DÀNH CHO KẺ DÁM KHÁM PHÁ 👿")
    await asyncio.sleep(1)
    m3 = await update.message.reply_text("👀 BƯỚC VÀO ĐÂY BẠN SẼ KHÔNG MUỐN QUAY LẠI 👀")

    await asyncio.sleep(2)
    for m in [m1, m2, m3]:
        try:
            await m.delete()
        except:
            pass

    keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="menu")]]
    await update.message.reply_text("NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏", reply_markup=InlineKeyboardMarkup(keyboard))

# ================= MENU =================
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Parse page number from callback data (e.g. "menu_p2")
    page = 0
    if query.data and query.data.startswith("menu_p"):
        try:
            page = int(query.data[6:])
        except ValueError:
            page = 0

    categories = _db_get_categories()
    albums = _db_get_albums()

    if categories:
        # Show paginated list of categories
        total = len(categories)
        start = page * _PAGE_SIZE
        end = start + _PAGE_SIZE
        page_cats = categories[start:end]

        buttons = []
        for cat in page_cats:
            buttons.append([InlineKeyboardButton(f"📁 {cat['name']}", callback_data=f"cat_{cat['id']}")])

        # Show uncategorized albums button if any exist
        uncategorized = [k for k, v in albums.items() if isinstance(v, dict) and not v.get("category_id")]
        if uncategorized:
            buttons.append([InlineKeyboardButton("📋 Album chưa phân loại", callback_data="cat__uncategorized")])

        # Pagination nav row
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Trang trước", callback_data=f"menu_p{page - 1}"))
        if end < total:
            nav.append(InlineKeyboardButton("Tiếp theo ➡️", callback_data=f"menu_p{page + 1}"))
        if nav:
            buttons.append(nav)

        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back")])
        await query.edit_message_text("📂 CHỌN DANH MỤC:", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        # No categories — show all albums with pagination
        all_albums = sorted(albums.items())
        total = len(all_albums)
        start = page * _PAGE_SIZE
        end = start + _PAGE_SIZE
        page_albums = all_albums[start:end]

        buttons = []
        for key, val in page_albums:
            date = key.replace("album_", "").replace("_", "-")
            label = f"Link🔥 {date[8:10]}-{date[5:7]}"
            title = val.get("title", label) if isinstance(val, dict) else label
            if WEBHOOK_URL:
                bridge_url = f"{WEBHOOK_URL}/miniapp/bridge/{key}"
                buttons.append([InlineKeyboardButton(title, web_app=WebAppInfo(url=bridge_url))])
            else:
                buttons.append([InlineKeyboardButton(title, callback_data=key)])

        # Pagination nav row
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Trang trước", callback_data=f"menu_p{page - 1}"))
        if end < total:
            nav.append(InlineKeyboardButton("Tiếp theo ➡️", callback_data=f"menu_p{page + 1}"))
        if nav:
            buttons.append(nav)

        buttons.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="back")])
        await query.edit_message_text("Chọn album:", reply_markup=InlineKeyboardMarkup(buttons))


# ================= CATEGORY PAGE =================
async def category_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show albums within a specific category, with pagination."""
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "cat_abc123" or "cat_abc123_p2" or "cat__uncategorized"
    inner = data[4:]  # strip leading "cat_"

    # Parse trailing "_p<digits>" as page number
    page = 0
    _m = re.search(r"_p(\d+)$", inner)
    if _m:
        page = int(_m.group(1))
        cat_id = inner[: _m.start()]
    else:
        cat_id = inner

    albums = _db_get_albums()

    if cat_id == "_uncategorized":
        filtered = sorted(
            [(k, v) for k, v in albums.items() if isinstance(v, dict) and not v.get("category_id")]
        )
        title_text = "📋 Album chưa phân loại"
    else:
        filtered = sorted(
            [(k, v) for k, v in albums.items() if isinstance(v, dict) and v.get("category_id") == cat_id]
        )
        categories = _db_get_categories()
        cat_name = next((c["name"] for c in categories if c["id"] == cat_id), cat_id)
        title_text = f"📁 {cat_name}"

    total = len(filtered)
    start = page * _PAGE_SIZE
    end = start + _PAGE_SIZE
    page_albums = filtered[start:end]

    buttons = []
    for key, val in page_albums:
        t = val.get("title", key) if isinstance(val, dict) else key
        if WEBHOOK_URL:
            bridge_url = f"{WEBHOOK_URL}/miniapp/bridge/{key}"
            buttons.append([InlineKeyboardButton(f"🔥 {t}", web_app=WebAppInfo(url=bridge_url))])
        else:
            buttons.append([InlineKeyboardButton(f"🔥 {t}", callback_data=key)])

    # Pagination nav row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Trang trước", callback_data=f"cat_{cat_id}_p{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Tiếp theo ➡️", callback_data=f"cat_{cat_id}_p{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("⬅️ Danh mục", callback_data="menu")])

    body = f"{title_text}:" if page_albums else f"{title_text}\n\n(Không có bài viết nào)"
    await query.edit_message_text(body, reply_markup=InlineKeyboardMarkup(buttons))

# ================= SEND ALBUM =================
async def send_album(chat_id, bot, album):
    media = []
    for i, p in enumerate(album["photos"]):
        url = p["url"]
        if url.startswith("/static/uploads/"):
            abs_path = os.path.join(_BOT_DIR, url.lstrip("/"))
            # Guard against path traversal: ensure the resolved path stays within _BOT_DIR
            real_path = os.path.realpath(abs_path)
            real_base = os.path.realpath(_BOT_DIR)
            if not real_path.startswith(real_base + os.sep):
                raise ValueError(f"Path traversal attempt detected: {url}")
            with open(real_path, "rb") as fh:
                photo_data = fh.read()
        else:
            photo_data = url
        if i == 0:
            media.append(InputMediaPhoto(photo_data, caption=p.get("caption", "")))
        else:
            media.append(InputMediaPhoto(photo_data))

    await bot.send_media_group(chat_id, media)

async def album_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "back":
        keyboard = [[InlineKeyboardButton("🔥 CHẠM LÀ NGHIỆN 🔥", callback_data="menu")]]
        await query.edit_message_text(
            "NHỮNG THỨ BẠN TÌM ĐỀU Ở ĐÂY 😏",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    albums = _db_get_albums()
    album = albums.get(query.data)

    chat_id = query.message.chat.id
    await query.delete_message()

    if album:
        await send_album(chat_id, context.bot, album)

# ================= SET TIME =================
async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or update.effective_chat.id != ADMIN_ID:
        return

    try:
        time_str = update.message.text.split("_")[1]
        h, m = map(int, time_str.split(":"))

        h_utc = (h - 7) % 24

        _db_save_config({"hour": h_utc, "minute": m})

        await update.message.reply_text(f"Đã set giờ: {h}:{m} (VN)")
    except:
        await update.message.reply_text("Sai format")

# ================= SCHEDULER =================
last_sent = None

async def scheduler_job(context: ContextTypes.DEFAULT_TYPE):
    global last_sent
    now = datetime.datetime.now(datetime.timezone.utc)
    cfg = _db_get_config()

    if now.hour == cfg["hour"] and now.minute == cfg["minute"]:
        key = f"{now.hour}:{now.minute}"
        if last_sent != key:
            last_sent = key

            albums = _db_get_albums()
            if not albums:
                return

            subs = _db_get_subscribers()

            # Build album list buttons (send notification instead of raw content)
            sched_buttons = []
            for album_id, album_data in sorted(albums.items()):
                title = album_data.get("title", album_id) if isinstance(album_data, dict) else album_id
                if WEBHOOK_URL:
                    bridge_url = f"{WEBHOOK_URL}/miniapp/bridge/{album_id}"
                    sched_buttons.append(
                        [InlineKeyboardButton(f"🔥 {title}", web_app=WebAppInfo(url=bridge_url))]
                    )
                else:
                    sched_buttons.append(
                        [InlineKeyboardButton(f"🔥 {title}", callback_data=album_id)]
                    )
            reply_markup = InlineKeyboardMarkup(sched_buttons) if sched_buttons else None

            success, fail = 0, 0

            for u in subs:
                try:
                    await context.bot.send_message(
                        u,
                        "🔔 Bài viết mới đã lên lịch!",
                        reply_markup=reply_markup,
                    )
                    success += 1
                except Exception:
                    fail += 1

            if ADMIN_ID:
                await context.bot.send_message(ADMIN_ID,
                    f"📊 Report\nUsers: {len(subs)}\nOK: {success}\nFail: {fail}"
                )

# ================= SENDALL =================
async def sendall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /sendall <text> [| ButtonText - URL ...]

    Sends a text message (with optional inline buttons) to all subscribers
    AND all IDs with status='active' in the database.

    Optional inline buttons are appended after a pipe (|) separator.
    Each button is formatted as "Button Text - URL" and separated by semicolons.
    Example: /sendall Hello world! | Click here - https://example.com; More - https://t.me/channel

    Blocked users (Forbidden error) are automatically marked in the database
    so they are excluded from future broadcasts.
    """
    if not ADMIN_ID or update.effective_chat.id != ADMIN_ID:
        return

    full_text = " ".join(context.args) if context.args else ""
    if not full_text:
        await update.message.reply_text(
            "Cú pháp: /sendall <nội dung> [| Tên nút - URL; Tên nút 2 - URL2]"
        )
        return

    # Parse optional inline buttons from pipe-separated section
    reply_markup = None
    if "|" in full_text:
        parts = full_text.split("|", 1)
        message_text = parts[0].strip()
        buttons_raw = parts[1].strip()
        keyboard = []
        for btn_def in buttons_raw.split(";"):
            btn_def = btn_def.strip()
            if " - " in btn_def:
                btn_text, btn_url = btn_def.rsplit(" - ", 1)
                btn_text = btn_text.strip()
                btn_url = btn_url.strip()
                if btn_text and btn_url:
                    keyboard.append([InlineKeyboardButton(btn_text, url=btn_url)])
        if keyboard:
            reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        message_text = full_text

    if not message_text:
        await update.message.reply_text("Nội dung tin nhắn không được để trống.")
        return

    # Combine subscribers + active IDs, deduplicated
    target_ids = _db_get_all_target_ids()
    if not target_ids:
        await update.message.reply_text("Chưa có người đăng ký nào.")
        return

    success, fail, blocked = 0, 0, 0
    for user_id in target_ids:
        try:
            await context.bot.send_message(user_id, message_text, reply_markup=reply_markup)
            success += 1
        except Forbidden:
            # User has blocked the bot — mark in DB so future broadcasts skip them.
            # Blocked users are counted in both `blocked` and `fail`.
            _db_mark_blocked(user_id)
            blocked += 1
            fail += 1
        except TelegramError:
            fail += 1
        except Exception:
            fail += 1

    summary = f"📢 Đã gửi thông báo!\n✅ Thành công: {success}\n❌ Thất bại: {fail}"
    if blocked:
        summary += f"\n🚫 Đã chặn bot (đánh dấu blocked): {blocked}"
    await update.message.reply_text(summary)


# ================= AUTO-REPLY =================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-reply with a Mini App link when the user's text matches an album title."""
    if not WEBHOOK_URL:
        return

    user_text = (update.message.text or "").strip().lower()
    if not user_text:
        return

    albums = _db_get_albums()
    for album_id, album_data in albums.items():
        title = (album_data.get("title", "") if isinstance(album_data, dict) else "").lower()
        if title and (user_text in title or title in user_text):
            bridge_url = f"{WEBHOOK_URL}/miniapp/bridge/{album_id}"
            keyboard = [[InlineKeyboardButton("🔥 Xem ngay", web_app=WebAppInfo(url=bridge_url))]]
            await update.message.reply_text(
                f"🔥 {album_data.get('title', album_id)}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return


def run_bot():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sendall", sendall_cmd))
    app.add_handler(CommandHandler("settudongguilink", set_time))
    app.add_handler(CallbackQueryHandler(menu, pattern=r"^menu(_p\d+)?$"))
    app.add_handler(CallbackQueryHandler(category_page, pattern=r"^cat_"))
    app.add_handler(CallbackQueryHandler(album_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(scheduler_job, interval=30, first=1)

    if WEBHOOK_URL and TOKEN:
        webhook_url = f"{WEBHOOK_URL}/webhook/{TOKEN}"
        app.run_webhook(
            listen="0.0.0.0",
            port=8443,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        app.run_polling()
