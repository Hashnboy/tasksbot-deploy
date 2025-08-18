# -*- coding: utf-8 -*-
"""
TasksBot (minimal PostgreSQL-only)
- Flask webhook (no polling)
- Gunicorn + systemd friendly
- SQLAlchemy (PostgreSQL)
- Telegram via pyTelegramBotAPI
- Optional OpenAI assistant
Env:
  TELEGRAM_TOKEN, WEBHOOK_BASE, DATABASE_URL, OPENAI_API_KEY (optional), TZ, PORT
"""

import os, re, json, time, hmac, hashlib, logging, threading
from datetime import datetime, timedelta, date
import pytz
import schedule

from flask import Flask, request
from telebot import TeleBot, types

from sqlalchemy import create_engine, Column, Integer, String, Text, Date, Time, DateTime, Boolean, func
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

# --------- ENV ---------
API_TOKEN   = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_BASE= os.getenv("WEBHOOK_BASE")
DB_URL      = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TZ_NAME     = os.getenv("TZ", "Europe/Moscow")
PORT        = int(os.getenv("PORT","10000"))
WEBHOOK_URL = f"{WEBHOOK_BASE}/{API_TOKEN}" if (API_TOKEN and WEBHOOK_BASE) else None

if not API_TOKEN or not DB_URL or not WEBHOOK_URL:
    raise RuntimeError("Need TELEGRAM_TOKEN, DATABASE_URL, WEBHOOK_BASE envs")

LOCAL_TZ = pytz.timezone(TZ_NAME)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

# --------- OpenAI (optional) ---------
openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        log.warning("OpenAI disabled: %s", e)

# --------- DB ---------
Base = declarative_base()
engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)          # Telegram chat id
    name = Column(String(255), default="")
    created_at = Column(DateTime, server_default=func.now())

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    date = Column(Date, index=True, nullable=False)
    category = Column(String(120), default="Личное", index=True)
    subcategory = Column(String(120), default="", index=True)
    text = Column(Text, nullable=False)
    deadline = Column(Time, nullable=True)
    status = Column(String(40), default="")     # "", "выполнено"
    repeat_rule = Column(String(255), default="")
    source = Column(String(255), default="")
    is_repeating = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    rule = Column(String(255), default="")              # "каждые 2 дня" / "shelf 72h"
    order_deadline = Column(String(10), default="14:00")
    emoji = Column(String(8), default="📦")
    delivery_offset_days = Column(Integer, default=1)
    shelf_days = Column(Integer, default=0)
    start_cycle = Column(Date, nullable=True)
    auto = Column(Boolean, default=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    task_id = Column(Integer, index=True)
    date = Column(Date, nullable=False)
    time = Column(Time, nullable=False)
    fired = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

Base.metadata.create_all(bind=engine)

# --------- BOT / APP ---------
bot = TeleBot(API_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# --------- Utils ---------
PAGE = 8

def now_local():
    return datetime.now(LOCAL_TZ)

def dstr(d: date):
    return d.strftime("%d.%m.%Y")

def parse_date(s): return datetime.strptime(s, "%d.%m.%Y").date()
def parse_time(s): return datetime.strptime(s, "%H:%M").time()

def weekday_ru(d: date):
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[d.weekday()]

def mk_cb(action, **kwargs):
    payload = {"a": action, **kwargs}
    s = json.dumps(payload, ensure_ascii=False)
    sig = hmac.new(b"cb-key", s.encode("utf-8"), hashlib.sha1).hexdigest()[:6]
    return f"{sig}|{s}"

def parse_cb(data):
    try:
        sig, s = data.split("|", 1)
        if hmac.new(b"cb-key", s.encode("utf-8"), hashlib.sha1).hexdigest()[:6] != sig:
            return None
        return json.loads(s)
    except Exception:
        return None

def ensure_user(sess, uid, name=""):
    u = sess.query(User).filter_by(id=uid).first()
    if not u:
        u = User(id=uid, name=name or "")
        sess.add(u); sess.commit()
    return u

# --------- Suppliers rules ---------
BASE_SUP_RULES = {
    "к-экспро": {"kind":"cycle_every_n_days","n_days":2,"delivery_offset":1,"deadline":"14:00","emoji":"📦"},
    "ип вылегжанина": {"kind":"delivery_shelf_then_order","delivery_offset":1,"shelf_days":3,"deadline":"14:00","emoji":"🥘"},
}
def norm_sup(name:str): return (name or "").strip().lower()

def load_rule(sess, supplier_name:str):
    s = sess.query(Supplier).filter(func.lower(Supplier.name)==norm_sup(supplier_name)).first()
    if s and s.active:
        rl = (s.rule or "").lower()
        if "каждые" in rl:
            import re
            n = 2
            m = re.findall(r"\d+", rl)
            if m: n = int(m[0])
            return {"kind":"cycle_every_n_days","n_days":n,"delivery_offset":s.delivery_offset_days or 1,
                    "deadline":s.order_deadline or "14:00","emoji":s.emoji or "📦"}
        if any(x in rl for x in ["shelf","72","хранен"]):
            return {"kind":"delivery_shelf_then_order","delivery_offset":s.delivery_offset_days or 1,
                    "shelf_days":s.shelf_days or 3,"deadline":s.order_deadline or "14:00","emoji":s.emoji or "🥘"}
    return BASE_SUP_RULES.get(norm_sup(supplier_name))

def plan_next(sess, user_id:int, supplier:str, category:str, subcategory:str):
    rule = load_rule(sess, supplier)
    if not rule: return []
    today = now_local().date()
    out = []
    if rule["kind"]=="cycle_every_n_days":
        delivery = today + timedelta(days=rule["delivery_offset"])
        next_order = today + timedelta(days=rule["n_days"])
        sess.add(Task(user_id=user_id, date=delivery, category=category, subcategory=subcategory,
                      text=f"{rule['emoji']} Принять поставку {supplier} ({subcategory or '—'})",
                      deadline=parse_time("10:00")))
        sess.add(Task(user_id=user_id, date=next_order, category=category, subcategory=subcategory,
                      text=f"{rule['emoji']} Заказать {supplier} ({subcategory or '—'})",
                      deadline=parse_time(rule["deadline"])))
        sess.commit()
        out = [("delivery", delivery), ("order", next_order)]
    else:
        delivery = today + timedelta(days=rule["delivery_offset"])
        next_order = delivery + timedelta(days=max(1, (rule.get("shelf_days",3)-1)))
        sess.add(Task(user_id=user_id, date=delivery, category=category, subcategory=subcategory,
                      text=f"{rule['emoji']} Принять поставку {supplier} ({subcategory or '—'})",
                      deadline=parse_time("11:00")))
        sess.add(Task(user_id=user_id, date=next_order, category=category, subcategory=subcategory,
                      text=f"{rule['emoji']} Заказать {supplier} ({subcategory or '—'})",
                      deadline=parse_time(rule["deadline"])))
        sess.commit()
        out = [("delivery", delivery), ("order", next_order)]
    return out

# --------- Formatting ---------
def short_line(t: Task, idx=None):
    dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
    p = f"{idx}. " if idx is not None else ""
    return f"{p}{t.category}/{t.subcategory}: {t.text[:40]}… (до {dl})"

def format_grouped(tasks, header_date=None):
    if not tasks: return "Задач нет."
    out = []
    if header_date:
        out.append(f"• {weekday_ru(tasks[0].date)} — {header_date}\n")
    cur_cat = cur_sub = None
    for t in sorted(tasks, key=lambda x: (x.category or "", x.subcategory or "", x.deadline or datetime.min.time(), x.text)):
        icon = "✅" if t.status=="выполнено" else "⬜"
        if t.category != cur_cat:
            out.append(f"📂 <b>{t.category or '—'}</b>"); cur_cat = t.category; cur_sub = None
        if t.subcategory != cur_sub:
            out.append(f"  └ <b>{t.subcategory or '—'}</b>"); cur_sub = t.subcategory
        line = f"    └ {icon} {t.text}"
        if t.deadline: line += f"  <i>(до {t.deadline.strftime('%H:%M')})</i>"
        out.append(line)
    return "\n".join(out)

def page_kb(items, page, total, action="open"):
    kb = types.InlineKeyboardMarkup()
    for label, tid in items:
        kb.add(types.InlineKeyboardButton(label, callback_data=mk_cb(action, id=tid)))
    nav = []
    if page>1: nav.append(types.InlineKeyboardButton("⬅️", callback_data=mk_cb("page", p=page-1, pa=action)))
    nav.append(types.InlineKeyboardButton(f"{page}/{total}", callback_data="noop"))
    if page<total: nav.append(types.InlineKeyboardButton("➡️", callback_data=mk_cb("page", p=page+1, pa=action)))
    if nav: kb.row(*nav)
    return kb

# --------- Data access ---------
def tasks_for_date(sess, uid:int, d:date):
    return (sess.query(Task)
            .filter(Task.user_id==uid, Task.date==d)
            .order_by(Task.category.asc(), Task.subcategory.asc(), Task.deadline.asc().nulls_last())
            ).all()

def tasks_for_week(sess, uid:int, base:date):
    days = [base + timedelta(days=i) for i in range(7)]
    return (sess.query(Task)
            .filter(Task.user_id==uid, Task.date.in_(days))
            .order_by(Task.date.asc(), Task.category.asc(), Task.subcategory.asc(), Task.deadline.asc().nulls_last())
            ).all()

# --------- Menus ---------
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня","📆 Неделя")
    kb.row("➕ Добавить","✅ Я сделал…","🧠 Ассистент")
    return kb

# --------- NLP add ---------
def ai_parse_items(text, uid):
    # try OpenAI JSON
    if openai_client:
        try:
            sys = ("Ты парсер задач. Верни только JSON-массив объектов: "
                   "{date:'ДД.ММ.ГГГГ'|'', time:'ЧЧ:ММ'|'', category, subcategory, task, repeat:'', supplier:''}.")
            resp = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"system","content":sys},{"role":"user","content":text}],
                temperature=0.2
            )
            raw = resp.choices[0].message.content.strip()
            data = json.loads(raw)
            if isinstance(data, dict): data = [data]
            out = []
            for it in data:
                out.append({
                    "date": it.get("date") or "",
                    "time": it.get("time") or "",
                    "category": it.get("category") or "Личное",
                    "subcategory": it.get("subcategory") or "",
                    "task": it.get("task") or "",
                    "repeat": it.get("repeat") or "",
                    "supplier": it.get("supplier") or "",
                    "user_id": uid
                })
            return out
        except Exception as e:
            log.warning("AI parse fail: %s", e)
    # fallback
    tl = text.lower()
    cat = "Кофейня" if any(x in tl for x in ["кофейн","к-экспро","вылегжан"]) else ("Табачка" if "табач" in tl else "Личное")
    sub = "Центр" if "центр" in tl else ("Полет" if ("полет" in tl or "полёт" in tl) else ("Климово" if "климов" in tl else ""))
    tm = re.search(r"(\d{1,2}:\d{2})", text)
    time_s = tm.group(1) if tm else ""
    if "сегодня" in tl: ds = dstr(now_local().date())
    elif "завтра" in tl: ds = dstr(now_local().date()+timedelta(days=1))
    else:
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text); ds = m.group(1) if m else ""
    supplier = "К-Экспро" if ("к-экспро" in tl or "k-exp" in tl or "к экспро" in tl) else ("ИП Вылегжанина" if "вылегжан" in tl else "")
    return [{
        "date": ds, "time": time_s, "category": cat, "subcategory": sub,
        "task": text.strip(), "repeat":"", "supplier": supplier, "user_id": uid
    }]

# --------- Handlers ---------
@bot.message_handler(commands=["start"])
def start(m):
    sess = SessionLocal()
    try:
        ensure_user(sess, m.chat.id, m.from_user.full_name if m.from_user else "")
    finally:
        sess.close()
    bot.send_message(m.chat.id, "Привет! Я твой ассистент по задачам.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def today(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        rows = tasks_for_date(sess, uid, now_local().date())
        if not rows:
            bot.send_message(uid, f"📅 Задачи на {dstr(now_local().date())}\n\nЗадач нет.", reply_markup=main_menu()); return
        items = [(short_line(t, i), t.id) for i,t in enumerate(rows, start=1)]
        total = (len(items)+PAGE-1)//PAGE
        kb = page_kb(items[:PAGE], 1, total, "open")
        header = f"📅 Задачи на {dstr(now_local().date())}\n\n" + format_grouped(rows, header_date=dstr(now_local().date())) + "\n\nОткрой карточку:"
        bot.send_message(uid, header, reply_markup=main_menu(), reply_markup_inline=kb)
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def week(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        rows = tasks_for_week(sess, uid, now_local().date())
        if not rows:
            bot.send_message(uid, "На неделю задач нет.", reply_markup=main_menu()); return
        by = {}
        for t in rows: by.setdefault(dstr(t.date), []).append(t)
        parts = []
        for ds in sorted(by.keys(), key=lambda s: parse_date(s)):
            parts.append(format_grouped(by[ds], header_date=ds)); parts.append("")
        bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "➕ Добавить")
def add(m):
    bot.send_message(m.chat.id, "Опиши задачу одним сообщением (дату/время/категорию распознаю).")
    bot.register_next_step_handler(m, add_text)

def add_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        items = ai_parse_items(m.text.strip(), uid)
        created = 0
        for it in items:
            dt = parse_date(it["date"]) if it["date"] else now_local().date()
            tm = parse_time(it["time"]) if it["time"] else None
            t = Task(user_id=uid, date=dt, category=it["category"], subcategory=it["subcategory"],
                     text=it["task"], deadline=tm, repeat_rule=it["repeat"], source=it["supplier"],
                     is_repeating=bool(it["repeat"]))
            sess.add(t); created += 1
        sess.commit()
        bot.send_message(uid, f"✅ Добавлено задач: {created}", reply_markup=main_menu())
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "✅ Я сделал…")
def done_free(m):
    bot.send_message(m.chat.id, "Напиши что именно сделал (например: сделал заказы к-экспро центр).")
    bot.register_next_step_handler(m, done_text)

def done_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        txt = m.text.lower()
        supplier = ""
        if any(x in txt for x in ["к-экспро","k-exp","к экспро"]): supplier = "К-Экспро"
        if "вылегжан" in txt: supplier = "ИП Вылегжанина"
        rows = tasks_for_date(sess, uid, now_local().date())
        changed = 0; last = None
        for t in rows:
            if t.status=="выполнено": continue
            low = (t.text or "").lower()
            if supplier and norm_sup(supplier) not in norm_sup(low): continue
            if not supplier and not any(w in low for w in ["заказ","закуп","сделал"]): continue
            t.status = "выполнено"; last = t; changed += 1
        sess.commit()
        msg = f"✅ Отмечено выполненным: {changed}."
        if changed and supplier and last:
            created = plan_next(sess, uid, supplier, last.category, last.subcategory)
            if created: msg += " Запланирована приемка/следующий заказ."
        bot.send_message(uid, msg, reply_markup=main_menu())
    finally:
        sess.close()

@bot.callback_query_handler(func=lambda c: True)
def cb(c):
    data = parse_cb(c.data) if c.data and c.data!="noop" else None
    uid = c.message.chat.id
    if not data: bot.answer_callback_query(c.id); return
    a = data.get("a")
    sess = SessionLocal()
    try:
        if a=="open":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
            text = (f"<b>{t.text}</b>\n"
                    f"📅 {weekday_ru(t.date)} — {dstr(t.date)}\n"
                    f"📁 {t.category}/{t.subcategory or '—'}\n"
                    f"⏰ Дедлайн: {dl}\n"
                    f"📝 Статус: {t.status or '—'}")
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Выполнить", callback_data=mk_cb("done", id=tid)))
            kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=mk_cb("del", id=tid)))
            bot.answer_callback_query(c.id)
            bot.send_message(uid, text, reply_markup=kb)
            return
        if a=="done":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            t.status = "выполнено"; sess.commit()
            sup = ""
            low = (t.text or "").lower()
            if any(x in low for x in ["к-экспро","k-exp","к экспро"]): sup="К-Экспро"
            if "вылегжан" in low: sup="ИП Вылегжанина"
            msg = "✅ Готово."
            if sup:
                created = plan_next(sess, uid, sup, t.category, t.subcategory)
                if created: msg += " Запланирована приемка/следующий заказ."
            bot.answer_callback_query(c.id, msg, show_alert=True); return
        if a=="del":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            sess.delete(t); sess.commit()
            bot.answer_callback_query(c.id, "Удалено", show_alert=True); return
        if a=="page":
            # для простоты обновим список "сегодня"
            uid = c.message.chat.id
            rows = tasks_for_date(sess, uid, now_local().date())
            items = [(short_line(t, i), t.id) for i,t in enumerate(rows, start=1)]
            page = int(data.get("p",1))
            total = (len(items)+PAGE-1)//PAGE
            page = max(1, min(page, total))
            slice_items = items[(page-1)*PAGE:page*PAGE]
            kb = page_kb(slice_items, page, total, "open")
            try:
                bot.edit_message_reply_markup(uid, c.message.message_id, reply_markup=kb)
            except Exception:
                pass
            bot.answer_callback_query(c.id); return
    finally:
        sess.close()

# --------- Scheduler jobs ---------
def job_daily_digest():
    sess = SessionLocal()
    try:
        today = now_local().date()
        users = sess.query(User).all()
        for u in users:
            tasks = tasks_for_date(sess, u.id, today)
            if not tasks: continue
            text = f"📅 План на {dstr(today)}\n\n" + format_grouped(tasks, header_date=dstr(today))
            try:
                bot.send_message(u.id, text)
            except Exception as e:
                log.error("digest send error: %s", e)
    finally:
        sess.close()

def scheduler_loop():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)
    while True:
        schedule.run_pending()
        time.sleep(1)

# --------- Flask webhook ---------
@app.route("/" + API_TOKEN, methods=["POST"])
def webhook():
    try:
        update = request.get_data().decode("utf-8")
        upd = types.Update.de_json(update)
        bot.process_new_updates([upd])
    except Exception as e:
        log.error("webhook error: %s", e)
    return "OK", 200

@app.route("/")
def home():
    return "TasksBot is running"

# --------- START ---------
if __name__ == "__main__":
    try:
        bot.remove_webhook()
    except Exception:
        pass
    time.sleep(0.5)
    bot.set_webhook(url=WEBHOOK_URL)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
