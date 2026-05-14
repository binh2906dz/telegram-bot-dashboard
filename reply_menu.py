"""Reply-keyboard menu (JSON) helpers for Telegram bot + dashboard API.

Schema (per bot):
  enabled: bool
  prompt: str — second message after /start when keyboard is shown
  ref_param_prefix: str — Telegram deep-link payload prefix (default REF)
  rows: list[list[cell]] — each cell: label, type, optional fields

Action types:
  text          — body (placeholders: {user_id}, {username}, {first_name},
                  {ref_link}, {bot_username})
  ref_link      — body (intro), bonus_text; appends invite link
  url           — body, url, optional button_label
  webapp        — body, album_id (Mini App bridge)
  remove_keyboard — optional body; hides reply keyboard
  stats         — public aggregate stats for this bot
  stats_admin   — same as /stats command (owner_only enforced)
"""
from __future__ import annotations

import json
import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

DEFAULT_REPLY_MENU: dict[str, Any] = {
    "enabled": False,
    "prompt": "👇 Chọn một tùy chọn bên dưới",
    "ref_param_prefix": "REF",
    "rows": [],
}

_ALLOWED_CELL_KEYS = frozenset(
    {"label", "type", "body", "url", "album_id", "owner_only", "bonus_text", "button_label"}
)
_ALLOWED_TYPES = frozenset(
    {"text", "ref_link", "url", "webapp", "remove_keyboard", "stats", "stats_admin"}
)

# Sample shown in dashboard ("Tải mẫu") — owner can edit freely after load.
SAMPLE_REPLY_MENU: dict[str, Any] = {
    "enabled": True,
    "prompt": "👇 Chọn một tùy chọn bên dưới",
    "ref_param_prefix": "REF",
    "rows": [
        [
            {"label": "💰 Số dư của tôi", "type": "text", "body": "💰 Số dư demo: bạn chưa liên kết ví.\n\nChỉnh nội dung nút này trên web dashboard."},
            {"label": "👥 Mời bạn bè", "type": "ref_link", "body": "🌟 Mời bạn bè", "bonus_text": "🎁 Mời thành công mỗi người để nhận thưởng (nội dung tùy chỉnh)."},
        ],
        [
            {"label": "📊 Thống kê bot", "type": "stats", "body": "📊 Thống kê bot"},
            {"label": "🎁 Code & Link", "type": "url", "body": "🎁 Nhận code tại đây:", "url": "https://t.me", "button_label": "Mở link"},
        ],
    ],
}


def expand_placeholders(template: str, mapping: dict[str, str]) -> str:
    if not template:
        return ""

    def _repl(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(mapping.get(key, ""))

    return _PLACEHOLDER_RE.sub(_repl, template)


def _sanitize_cell(cell: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(cell, dict):
        return None
    label = str(cell.get("label", "")).strip()
    if not label:
        return None
    typ = str(cell.get("type", "text") or "text").strip().lower()
    if typ not in _ALLOWED_TYPES:
        typ = "text"
    out: dict[str, Any] = {"label": label, "type": typ}
    if typ in ("text", "ref_link", "url", "webapp", "remove_keyboard", "stats", "stats_admin"):
        body = cell.get("body")
        if body is not None:
            out["body"] = str(body)
    if typ == "ref_link" and cell.get("bonus_text") is not None:
        out["bonus_text"] = str(cell.get("bonus_text"))
    if typ == "url":
        out["url"] = str(cell.get("url", "")).strip()
        if cell.get("button_label"):
            out["button_label"] = str(cell.get("button_label")).strip()
    if typ == "webapp":
        out["album_id"] = str(cell.get("album_id", "")).strip()
    if typ == "stats_admin":
        out["owner_only"] = True
    elif cell.get("owner_only"):
        out["owner_only"] = True
    return out


def normalize_reply_menu(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a safe, bounded menu config dict."""
    if not isinstance(raw, dict):
        raw = {}
    enabled = bool(raw.get("enabled"))
    prompt = str(raw.get("prompt") or DEFAULT_REPLY_MENU["prompt"]).strip()
    if not prompt:
        prompt = str(DEFAULT_REPLY_MENU["prompt"])
    ref_prefix = str(raw.get("ref_param_prefix") or "REF").strip() or "REF"
    rows_in = raw.get("rows")
    if not isinstance(rows_in, list):
        rows_in = []
    rows_out: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    max_rows = 16
    max_cols = 4
    for row in rows_in[:max_rows]:
        if not isinstance(row, list):
            continue
        out_row: list[dict[str, Any]] = []
        for cell in row[:max_cols]:
            if not isinstance(cell, dict):
                continue
            # Only allow known keys (defense in depth for API POST)
            slim = {k: cell[k] for k in cell if k in _ALLOWED_CELL_KEYS}
            san = _sanitize_cell(slim)
            if not san:
                continue
            lab = san["label"]
            if lab in seen:
                continue
            seen.add(lab)
            out_row.append(san)
        if out_row:
            rows_out.append(out_row)
    return {
        "enabled": enabled,
        "prompt": prompt,
        "ref_param_prefix": ref_prefix,
        "rows": rows_out,
    }


def find_menu_action_for_text(cfg: dict[str, Any], message_text: str) -> dict[str, Any] | None:
    t = (message_text or "").strip()
    if not t:
        return None
    for row in cfg.get("rows") or []:
        if not isinstance(row, list):
            continue
        for cell in row:
            if not isinstance(cell, dict):
                continue
            if str(cell.get("label", "")).strip() == t:
                return cell
    return None


def build_reply_keyboard_markup(cfg: dict[str, Any]):
    """Return ReplyKeyboardMarkup or None if disabled / empty."""
    try:
        from telegram import KeyboardButton, ReplyKeyboardMarkup
    except ImportError:
        return None
    if not cfg.get("enabled"):
        return None
    kb_rows: list[list[KeyboardButton]] = []
    for row in cfg.get("rows") or []:
        if not isinstance(row, list):
            continue
        kb_row: list[KeyboardButton] = []
        for cell in row:
            if not isinstance(cell, dict):
                continue
            label = str(cell.get("label", "")).strip()
            if label:
                kb_row.append(KeyboardButton(label))
        if kb_row:
            kb_rows.append(kb_row)
    if not kb_rows:
        return None
    return ReplyKeyboardMarkup(
        kb_rows,
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=False,
    )


def reply_menu_json_sample() -> str:
    return json.dumps(SAMPLE_REPLY_MENU, ensure_ascii=False, indent=2)
