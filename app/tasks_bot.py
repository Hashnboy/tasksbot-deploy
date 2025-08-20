# -*- coding: utf-8 -*-
"""
TasksBot (polling, PostgreSQL)
- Telegram: pyTelegramBotAPI (TeleBot)
- База: PostgreSQL (SQLAlchemy)
- Фичи:
  • Задачи/подзадачи, дедлайны, напоминания, поиск
  • Повторяемость (шаблоны): «каждые N дней», «каждый вторник 12:00», «по пн,ср…»
  • Поставщики и автопланирование «заказ → приёмка → следующий заказ»
  • Делегирование задач другим пользователям, зависимости между задачами
  • Профиль: TZ, ежедневный дайджест 08:00 (локальная TZ)
  • GPT‑ассистент (OPENAI_API_KEY, опционально)
Env:
  TELEGRAM_TOKEN, DATABASE_URL, TZ (default Europe/Moscow), OPENAI_API_KEY (optional)
"""

import os, re, json, time, hmac, hashlib, logging, threading
from datetime import datetime, timedelta, date, time as dtime

import pytz
import schedule

from telebot import TeleBot, types
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Date, Time, DateTime, Boolean,
    ForeignKey, func, UniqueConstraint, and_, or_
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

# --------- ENV / LOG ---------
API_TOKEN   = os.getenv("TELEGRAM_TOKEN")
DB_URL      = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TZ_NAME     = os.getenv("TZ", "Europe/Moscow")

if not API_TOKEN or not DB_URL:
    raise RuntimeError("Need TELEGRAM_TOKEN and DATABASE_URL envs")

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
    id = Column(Integer, primary_key=True)  # Telegram chat id
    name = Column(String(255), default="")
    tz = Column(String(64), default="Europe/Moscow")
    digest_08 = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint('user_id','date','text','category','subcategory','is_repeating', name='uq_task_day'),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    assignee_id = Column(Integer, nullable=True)  # делегировано кому (chat id)
    date = Column(Date, index=True, nullable=False)
    category = Column(String(120), default="Личное", index=True)
    subcategory = Column(String(120), default="", index=True)  # ТТ/локация
    text = Column(Text, nullable=False)
    deadline = Column(Time, nullable=True)
    status = Column(String(40), default="")  # "", "выполнено"
    repeat_rule = Column(String(255), default="")
    source = Column(String(255), default="")  # supplier, repeat-instance и т.п.
    is_repeating = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

class Subtask(Base):
    __tablename__ = "subtasks"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    text = Column(Text, nullable=False)
    status = Column(String(40), default="")

class Dependency(Base):
    __tablename__ = "dependencies"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    depends_on_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), index=True)

class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    rule = Column(String(255), default="")         # "каждые 2 дня" / "shelf 72h"
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

# --------- BOT ---------
bot = TeleBot(API_TOKEN, parse_mode="HTML")
PAGE = 8

# --------- Utils ---------
def now_local():
    return datetime.now(LOCAL_TZ)

def dstr(d: date): return d.strftime("%d.%m.%Y")
def tstr(t: dtime|None): return t.strftime("%H:%M") if t else "—"
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

def ensure_user(sess, uid, name="", tz=None):
    u = sess.query(User).filter_by(id=uid).first()
    if not u:
        u = User(id=uid, name=name or "", tz=tz or TZ_NAME)
        sess.add(u); sess.commit()
    return u

# --------- Suppliers rules / plan ---------
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
            m = re.findall(r"\d+", rl); n = int(m[0]) if m else 2
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
        sess.merge(Task(user_id=user_id, date=delivery, category=category, subcategory=subcategory,
                        text=f"{rule['emoji']} Принять поставку {supplier} ({subcategory or '—'})",
                        deadline=parse_time("10:00")))
        sess.merge(Task(user_id=user_id, date=next_order, category=category, subcategory=subcategory,
                        text=f"{rule['emoji']} Заказать {supplier} ({subcategory or '—'})",
                        deadline=parse_time(rule["deadline"])))
        sess.commit()
        out = [("delivery", delivery), ("order", next_order)]
    else:
        delivery = today + timedelta(days=rule["delivery_offset"])
        next_order = delivery + timedelta(days=max(1, (rule.get("shelf_days",3)-1)))
        sess.merge(Task(user_id=user_id, date=delivery, category=category, subcategory=subcategory,
                        text=f"{rule['emoji']} Принять поставку {supplier} ({subcategory or '—'})",
                        deadline=parse_time("11:00")))
        sess.merge(Task(user_id=user_id, date=next_order, category=category, subcategory=subcategory,
                        text=f"{rule['emoji']} Заказать {supplier} ({subcategory or '—'})",
                        deadline=parse_time(rule["deadline"])))
        sess.commit()
        out = [("delivery", delivery), ("order", next_order)]
    return out

# --------- Repeats ---------
def rule_hits_date(rule_text:str, created_at:datetime, target:date, template_deadline: dtime|None) -> dtime|None:
    if not rule_text: return None
    rl = rule_text.strip().lower()
    if rl.startswith("каждые"):
        m = re.findall(r"\d+", rl); n = int(m[0]) if m else 1
        base = created_at.date() if created_at else date(2025,1,1)
        delta = (target - base).days
        return template_deadline if (delta >= 0 and delta % n == 0) else None
    if rl.startswith("каждый"):
        days = {"пн":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6,
                "понедельник":0,"вторник":1,"среда":2,"четверг":3,"пятница":4,"суббота":5,"воскресенье":6}
        wd = None
        for k,v in days.items():
            if f" {k}" in f" {rl}": wd=v; break
        if wd is None or target.weekday()!=wd: return None
        tm = re.search(r"(\d{1,2}:\d{2})", rl)
        return parse_time(tm.group(1)) if tm else template_deadline
    if rl.startswith("по "):
        m = re.findall(r"(пн|вт|ср|чт|пт|сб|вс)", rl)
        mapd = {"пн":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6}
        wds = {mapd[x] for x in m} if m else set()
        return template_deadline if target.weekday() in wds else None
    return None

def expand_repeats_for_date(sess, uid:int, target:date):
    templates = sess.query(Task).filter(Task.user_id==uid, Task.is_repeating==True).all()
    for t in templates:
        hit_deadline = rule_hits_date(t.repeat_rule or "", t.created_at, target, t.deadline)
        if not hit_deadline: continue
        exists = (sess.query(Task)
                  .filter(Task.user_id==uid, Task.date==target, Task.is_repeating==False,
                          Task.text==t.text, Task.category==t.category, Task.subcategory==t.subcategory)
                  .first())
        if exists: continue
        sess.add(Task(user_id=uid, date=target, category=t.category, subcategory=t.subcategory,
                      text=t.text, deadline=hit_deadline, status="", repeat_rule="",
                      source="repeat-instance", is_repeating=False))
    sess.commit()

# --------- Formatting / keyboards ---------
def short_line(t: Task, idx=None):
    assignee = f" → @{t.assignee_id}" if t.assignee_id and t.assignee_id!=t.user_id else ""
    p = f"{idx}. " if idx is not None else ""
    return f"{p}{t.category}/{t.subcategory or '—'}: {t.text[:40]}… (до {tstr(t.deadline)}){assignee}"

def format_grouped(tasks, header_date=None):
    if not tasks: return "Задач нет."
    out = []
    if header_date:
        out.append(f"• {weekday_ru(tasks[0].date)} — {header_date}\n")
    cur_cat = cur_sub = None
    for t in sorted(tasks, key=lambda x: (x.category or "", x.subcategory or "", x.deadline or dtime.min, x.text)):
        icon = "✅" if t.status=="выполнено" else "⬜"
        if t.category != cur_cat:
            out.append(f"📂 <b>{t.category or '—'}</b>"); cur_cat = t.category; cur_sub = None
        if t.subcategory != cur_sub:
            out.append(f"  └ <b>{t.subcategory or '—'}</b>"); cur_sub = t.subcategory
        line = f"    └ {icon} {t.text}"
        if t.deadline: line += f"  <i>(до {tstr(t.deadline)})</i>"
        if t.assignee_id and t.assignee_id!=t.user_id: line += f"  [делегировано: {t.assignee_id}]"
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

def tasks_for_date(sess, uid:int, d:date):
    return (sess.query(Task)
            .filter(Task.user_id==uid, Task.date==d, Task.is_repeating==False)
            .order_by(Task.category.asc(), Task.subcategory.asc(), Task.deadline.asc().nulls_last())
            ).all()

def tasks_for_week(sess, uid:int, base:date):
    days = [base + timedelta(days=i) for i in range(7)]
    return (sess.query(Task)
            .filter(Task.user_id==uid, Task.date.in_(days), Task.is_repeating==False)
            .order_by(Task.date.asc(), Task.category.asc(), Task.subcategory.asc(), Task.deadline.asc().nulls_last())
            ).all()

def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📅 Сегодня","📆 Неделя")
    kb.row("➕ Добавить","✅ Я сделал…","🧠 Ассистент")
    kb.row("🚚 Поставки","🔎 Найти")
    kb.row("👤 Профиль","🧩 Зависимости","🤝 Делегирование")
    return kb

# --------- NLP add ---------
def ai_parse_items(text, uid):
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
        ensure_user(sess, m.chat.id, m.from_user.full_name if m.from_user else "", tz=TZ_NAME)
    finally:
        sess.close()
    bot.send_message(m.chat.id, "Привет! Я твой ассистент по задачам.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📅 Сегодня")
def today(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        expand_repeats_for_date(sess, uid, now_local().date())
        rows = tasks_for_date(sess, uid, now_local().date())
        if not rows:
            bot.send_message(uid, f"📅 Задачи на {dstr(now_local().date())}\n\nЗадач нет.", reply_markup=main_menu()); return
        items = [(short_line(t, i), t.id) for i,t in enumerate(rows, start=1)]
        total = (len(items)+PAGE-1)//PAGE or 1
        kb = page_kb(items[:PAGE], 1, total, "open")
        header = f"📅 Задачи на {dstr(now_local().date())}\n\n" + format_grouped(rows, header_date=dstr(now_local().date())) + "\n\nОткрой карточку:"
        bot.send_message(uid, header, reply_markup=main_menu())
        bot.send_message(uid, "Навигация по задачам:", reply_markup=kb)
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "📆 Неделя")
def week(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        base = now_local().date()
        for i in range(7):
            expand_repeats_for_date(sess, uid, base+timedelta(days=i))
        rows = tasks_for_week(sess, uid, base)
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
    sent = bot.send_message(m.chat.id, "Опиши задачу одним сообщением (дату/время/категорию распознаю).")
    bot.register_next_step_handler(sent, add_text)

def add_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        items = ai_parse_items(m.text.strip(), uid)
        created = 0; templates = 0
        for it in items:
            dt = parse_date(it["date"]) if it["date"] else now_local().date()
            tm = parse_time(it["time"]) if it["time"] else None
            is_rep = bool((it.get("repeat") or "").strip())
            t = Task(user_id=uid, date=dt, category=it["category"], subcategory=it["subcategory"],
                     text=it["task"], deadline=tm, repeat_rule=it["repeat"], source=it["supplier"],
                     is_repeating=is_rep)
            sess.add(t)
            if is_rep: templates += 1
            else: created += 1
        sess.commit()
        msg = f"✅ Добавлено задач: {created}."
        if templates: msg += f" Создано шаблонов повторения: {templates}."
        bot.send_message(uid, msg, reply_markup=main_menu())
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "✅ Я сделал…")
def done_free(m):
    sent = bot.send_message(m.chat.id, "Напиши что именно сделал (например: сделал заказы к-экспро центр).")
    bot.register_next_step_handler(sent, done_text)

def done_text(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        txt = (m.text or "").lower()
        supplier = ""
        if any(x in txt for x in ["к-экспро","k-exp","к экспро"]): supplier = "К-Экспро"
        if "вылегжан" in txt: supplier = "ИП Вылегжанина"
        rows = tasks_for_date(sess, uid, now_local().date())
        changed = 0; last = None
        for t in rows:
            if t.status=="выполнено": continue
            low = (t.text or "").lower()
            is_order = ("заказ" in low or "закуп" in low)
            if supplier:
                if norm_sup(supplier) not in norm_sup(low): continue
                if not is_order: continue
            elif not any(w in low for w in ["заказ","закуп","сделал"]):
                continue
            t.status = "выполнено"; last = t; changed += 1
        sess.commit()
        msg = f"✅ Отмечено выполненным: {changed}."
        if changed and supplier and last:
            created = plan_next(sess, uid, supplier, last.category, last.subcategory)
            if created: msg += " Запланирована приемка/следующий заказ."
        bot.send_message(uid, msg, reply_markup=main_menu())
    finally:
        sess.close()

# ----- Поставки -----
@bot.message_handler(func=lambda msg: msg.text == "🚚 Поставки")
def supplies_menu(m):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📦 Заказы сегодня","🆕 Добавить поставщика")
    kb.row("⬅️ Назад")
    bot.send_message(m.chat.id, "Меню поставок:", reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Назад")
def back_main(m):
    bot.send_message(m.chat.id, "Ок.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📦 Заказы сегодня")
def orders_today(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        rows = tasks_for_date(sess, uid, now_local().date())
        orders = [t for t in rows if "заказ" in (t.text or "").lower()]
        if not orders:
            bot.send_message(uid, "На сегодня заказов нет.", reply_markup=main_menu()); return
        items = [(short_line(t, i), t.id) for i,t in enumerate(orders, start=1)]
        total = (len(items)+PAGE-1)//PAGE or 1
        kb = page_kb(items[:PAGE], 1, total, "open")
        bot.send_message(uid, "Заказы на сегодня:", reply_markup=kb)
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "🆕 Добавить поставщика")
def add_supplier(m):
    text = ("Формат:\n"
            "Название; правило; дедлайн; emoji; delivery_offset; shelf_days; auto(1/0); active(1/0)\n\n"
            "Примеры:\n"
            "К-Экспро; каждые 2 дня; 14:00; 📦; 1; 0; 1; 1\n"
            "ИП Вылегжанина; shelf 72h; 14:00; 🥘; 1; 3; 1; 1")
    sent = bot.send_message(m.chat.id, text)
    bot.register_next_step_handler(sent, add_supplier_parse)

def add_supplier_parse(m):
    sess = SessionLocal()
    try:
        parts = [p.strip() for p in (m.text or "").split(";")]
        if len(parts) < 8:
            bot.send_message(m.chat.id, "Ошибка формата. Нужны 8 полей.", reply_markup=main_menu()); return
        name, rule, deadline, emoji, offs, shelf, auto, active = parts[:8]
        s = sess.query(Supplier).filter(func.lower(Supplier.name)==name.strip().lower()).first() or Supplier(name=name.strip())
        s.rule = rule; s.order_deadline = deadline; s.emoji = emoji
        s.delivery_offset_days = int(offs or 1); s.shelf_days = int(shelf or 0)
        s.auto = bool(int(auto)); s.active = bool(int(active))
        sess.add(s); sess.commit()
        bot.send_message(m.chat.id, "✅ Поставщик сохранён.", reply_markup=main_menu())
    finally:
        sess.close()

# ----- Поиск / Ассистент -----
@bot.message_handler(func=lambda msg: msg.text == "🔎 Найти")
def search_prompt(m):
    sent = bot.send_message(m.chat.id, "Что ищем? (текст, категория/подкатегория или дата ДД.ММ.ГГГГ)")
    bot.register_next_step_handler(sent, do_search)

def do_search(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id; q = (m.text or "").strip()
        ds = re.search(r"(\d{2}\.\d{2}\.\d{4})", q)
        conds = [Task.user_id==uid, Task.is_repeating==False]
        if ds: conds.append(Task.date==parse_date(ds.group(1)))
        if "/" in q:
            cat, sub = [x.strip() for x in q.split("/",1)]
            conds.extend([Task.category.ilike(f"%{cat}%"), Task.subcategory.ilike(f"%{sub}%")])
        elif q:
            conds.append(or_(Task.text.ilike(f"%{q}%"),
                             Task.category.ilike(f"%{q}%"),
                             Task.subcategory.ilike(f"%{q}%")))
        rows = (sess.query(Task).filter(and_(*conds))
                .order_by(Task.date.asc(), Task.category.asc(), Task.subcategory.asc()).all())
        if not rows:
            bot.send_message(uid, "Ничего не найдено.", reply_markup=main_menu()); return
        by = {}
        for t in rows: by.setdefault(dstr(t.date), []).append(t)
        parts = []
        for ds in sorted(by.keys(), key=lambda s: parse_date(s)):
            parts.append(format_grouped(by[ds], header_date=ds)); parts.append("")
        bot.send_message(uid, "\n".join(parts), reply_markup=main_menu())
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "🧠 Ассистент")
def assistant(m):
    sent = bot.send_message(m.chat.id, "Спроси меня о приоритете/плане. Я учту твою неделю.", reply_markup=main_menu())
    bot.register_next_step_handler(sent, assistant_answer)

def assistant_answer(m):
    uid = m.chat.id
    sess = SessionLocal()
    try:
        base = now_local().date()
        rows = tasks_for_week(sess, uid, base)
        context_lines = []
        for t in rows[:200]:
            context_lines.append(f"{dstr(t.date)} | {t.category}/{t.subcategory or '—'} | {t.text} | до {tstr(t.deadline)} | {t.status or '—'}")
        question = m.text or ""
        if openai_client:
            try:
                system = ("Ты ассистент‑планировщик. Кратко (маркерами) дай приоритеты, сроки, предупреждения по дедлайнам. Русским языком.")
                user = "Контекст задач на неделю:\n" + "\n".join(context_lines) + "\n\nВопрос:\n" + question
                resp = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role":"system","content":system},{"role":"user","content":user}],
                    temperature=0.2
                )
                bot.send_message(uid, resp.choices[0].message.content.strip(), reply_markup=main_menu())
                return
            except Exception as e:
                log.warning("assistant fail: %s", e)
        bot.send_message(uid, "• Начни с задач с временем до 12:00.\n• Далее — «Заказы» поставщиков (до дедлайнов).\n• Потом личные без срока.", reply_markup=main_menu())
    finally:
        sess.close()

# ----- Профиль / Делегирование / Зависимости -----
@bot.message_handler(func=lambda msg: msg.text == "👤 Профиль")
def profile(m):
    sess = SessionLocal()
    try:
        u = ensure_user(sess, m.chat.id)
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("🕒 TZ", "📨 Дайджест 08:00")
        kb.row("⬅️ Назад")
        bot.send_message(m.chat.id, f"Твой профиль:\n• TZ: {u.tz}\n• Дайджест 08:00: {'вкл' if u.digest_08 else 'выкл'}", reply_markup=kb)
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "🕒 TZ")
def profile_tz(m):
    sent = bot.send_message(m.chat.id, "Введи IANA TZ, напр. Europe/Moscow")
    bot.register_next_step_handler(sent, profile_tz_set)

def profile_tz_set(m):
    sess = SessionLocal()
    try:
        tz = (m.text or "").strip()
        try:
            pytz.timezone(tz)
        except Exception:
            bot.send_message(m.chat.id, "Некорректная TZ. Пример: Europe/Moscow", reply_markup=main_menu()); return
        u = ensure_user(sess, m.chat.id)
        u.tz = tz; sess.commit()
        bot.send_message(m.chat.id, f"✅ TZ обновлена: {tz}", reply_markup=main_menu())
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "📨 Дайджест 08:00")
def profile_digest_toggle(m):
    sess = SessionLocal()
    try:
        u = ensure_user(sess, m.chat.id)
        u.digest_08 = not (u.digest_08 or False)
        sess.commit()
        bot.send_message(m.chat.id, f"Дайджест теперь: {'вкл' if u.digest_08 else 'выкл'}", reply_markup=main_menu())
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "🤝 Делегирование")
def delegation_menu(m):
    text = ("Отправь ID чата (телеграм ID получателя) и ID задачи через пробел.\n"
            "Пример: 123456789 42")
    sent = bot.send_message(m.chat.id, text)
    bot.register_next_step_handler(sent, delegation_set)

def delegation_set(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        parts = (m.text or "").strip().split()
        if len(parts)!=2 or not parts[0].isdigit() or not parts[1].isdigit():
            bot.send_message(uid, "Формат: <assignee_chat_id> <task_id>", reply_markup=main_menu()); return
        assignee = int(parts[0]); tid = int(parts[1])
        t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
        if not t:
            bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); return
        t.assignee_id = assignee; sess.commit()
        bot.send_message(uid, f"✅ Задача делегирована {assignee}.", reply_markup=main_menu())
        try:
            bot.send_message(assignee, f"Вам делегирована задача от {uid}: «{t.text}» на {dstr(t.date)} (до {tstr(t.deadline)})")
        except Exception: pass
    finally:
        sess.close()

@bot.message_handler(func=lambda msg: msg.text == "🧩 Зависимости")
def deps_menu(m):
    text = ("Создать зависимость: «child_id parent_id» (child ждёт parent).\n"
            "Пример: 50 42 — задача 50 зависит от выполнения 42.")
    sent = bot.send_message(m.chat.id, text)
    bot.register_next_step_handler(sent, deps_set)

def deps_set(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        parts = (m.text or "").strip().split()
        if len(parts)!=2 or not all(p.isdigit() for p in parts):
            bot.send_message(uid, "Формат: <child_task_id> <parent_task_id>", reply_markup=main_menu()); return
        child, parent = map(int, parts)
        ct = sess.query(Task).filter(Task.id==child, Task.user_id==uid).first()
        pt = sess.query(Task).filter(Task.id==parent, Task.user_id==uid).first()
        if not ct or not pt:
            bot.send_message(uid, "Задача(и) не найдены.", reply_markup=main_menu()); return
        sess.add(Dependency(task_id=child, depends_on_id=parent)); sess.commit()
        bot.send_message(uid, "✅ Зависимость добавлена.", reply_markup=main_menu())
    finally:
        sess.close()

# ----- Callbacks (карточки) -----
@bot.callback_query_handler(func=lambda c: True)
def cb(c):
    data = parse_cb(c.data) if c.data and c.data!="noop" else None
    uid = c.message.chat.id
    if not data:
        bot.answer_callback_query(c.id); return
    a = data.get("a")
    sess = SessionLocal()
    try:
        if a=="open":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            dl = tstr(t.deadline)
            deps = sess.query(Dependency).filter(Dependency.task_id==t.id).all()
            dep_text = ""
            if deps:
                dep_ids = [str(d.depends_on_id) for d in deps]
                dep_text = f"\n🔗 Зависит от: {', '.join(dep_ids)}"
            text = (f"<b>{t.text}</b>\n"
                    f"📅 {weekday_ru(t.date)} — {dstr(t.date)}\n"
                    f"📁 {t.category}/{t.subcategory or '—'}\n"
                    f"⏰ Дедлайн: {dl}\n"
                    f"📝 Статус: {t.status or '—'}{dep_text}")
            kb = types.InlineKeyboardMarkup()
            kb.row(types.InlineKeyboardButton("✅ Выполнить", callback_data=mk_cb("done", id=tid)),
                   types.InlineKeyboardButton("🗑 Удалить", callback_data=mk_cb("del", id=tid)))
            kb.row(types.InlineKeyboardButton("✏️ Дедлайн", callback_data=mk_cb("setdl", id=tid)),
                   types.InlineKeyboardButton("⏰ Напоминание", callback_data=mk_cb("rem", id=tid)))
            kb.row(types.InlineKeyboardButton("➕ Подзадача", callback_data=mk_cb("sub", id=tid)),
                   types.InlineKeyboardButton("🤝 Делегировать", callback_data=mk_cb("dlg", id=tid)))
            bot.answer_callback_query(c.id)
            bot.send_message(uid, text, reply_markup=kb)
            return
        if a=="page":
            rows = tasks_for_date(sess, uid, now_local().date())
            items = [(short_line(t, i), t.id) for i,t in enumerate(rows, start=1)]
            page = int(data.get("p",1))
            total = (len(items)+PAGE-1)//PAGE or 1
            page = max(1, min(page, total))
            slice_items = items[(page-1)*PAGE:page*PAGE]
            kb = page_kb(slice_items, page, total, "open")
            try:
                bot.edit_message_reply_markup(uid, c.message.message_id, reply_markup=kb)
            except Exception:
                pass
            bot.answer_callback_query(c.id); return
        if a=="done":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            deps = sess.query(Dependency).filter(Dependency.task_id==t.id).all()
            if deps:
                undone = sess.query(Task).filter(Task.id.in_([d.depends_on_id for d in deps]),
                                                 Task.status!="выполнено").count()
                if undone>0:
                    bot.answer_callback_query(c.id, "Есть невыполненные зависимости.", show_alert=True); return
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
        if a=="setdl":
            tid = int(data.get("id"))
            bot.answer_callback_query(c.id)
            sent = bot.send_message(uid, "Введи время в формате ЧЧ:ММ")
            bot.register_next_step_handler(sent, set_deadline_text, tid)
            return
        if a=="rem":
            tid = int(data.get("id"))
            bot.answer_callback_query(c.id)
            sent = bot.send_message(uid, "Введи напоминание: ДД.ММ.ГГГГ ЧЧ:ММ")
            bot.register_next_step_handler(sent, add_reminder_text, tid)
            return
        if a=="sub":
            tid = int(data.get("id"))
            bot.answer_callback_query(c.id)
            sent = bot.send_message(uid, "Текст подзадачи:")
            bot.register_next_step_handler(sent, add_subtask_text, tid)
            return
        if a=="dlg":
            tid = int(data.get("id"))
            bot.answer_callback_query(c.id)
            sent = bot.send_message(uid, "Кому делегировать? Введи chat_id получателя.")
            bot.register_next_step_handler(sent, delegate_to_user, tid)
            return
    finally:
        sess.close()

def set_deadline_text(m, tid):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        try:
            tm = parse_time(m.text.strip())
        except Exception:
            bot.send_message(uid, "Формат времени: ЧЧ:ММ", reply_markup=main_menu()); return
        t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
        if not t: bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); return
        t.deadline = tm; sess.commit()
        bot.send_message(uid, "⏰ Дедлайн обновлён.", reply_markup=main_menu())
    finally:
        sess.close()

def add_reminder_text(m, tid):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        try:
            parts = m.text.strip().split()
            dt = parse_date(parts[0]); tm = parse_time(parts[1])
        except Exception:
            bot.send_message(uid, "Формат: ДД.ММ.ГГГГ ЧЧ:ММ", reply_markup=main_menu()); return
        t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
        if not t: bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); return
        sess.add(Reminder(user_id=uid, task_id=tid, date=dt, time=tm, fired=False))
        sess.commit()
        bot.send_message(uid, "🔔 Напоминание создано.", reply_markup=main_menu())
    finally:
        sess.close()

def add_subtask_text(m, tid):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
        if not t: bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); return
        sess.add(Subtask(task_id=tid, text=(m.text or "").strip(), status=""))
        sess.commit()
        bot.send_message(uid, "➕ Подзадача добавлена.", reply_markup=main_menu())
    finally:
        sess.close()

def delegate_to_user(m, tid):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        try:
            assignee = int((m.text or "").strip())
        except Exception:
            bot.send_message(uid, "Нужен числовой chat_id.", reply_markup=main_menu()); return
        t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
        if not t: bot.send_message(uid, "Задача не найдена.", reply_markup=main_menu()); return
        t.assignee_id = assignee; sess.commit()
        bot.send_message(uid, f"✅ Делегировано: {assignee}", reply_markup=main_menu())
        try:
            bot.send_message(assignee, f"Вам делегирована задача от {uid}: «{t.text}» на {dstr(t.date)} (до {tstr(t.deadline)})")
        except Exception: pass
    finally:
        sess.close()

# --------- Schedulers ---------
def job_daily_digest():
    sess = SessionLocal()
    try:
        today = now_local().date()
        users = sess.query(User).all()
        for u in users:
            if not u.digest_08: continue
            expand_repeats_for_date(sess, u.id, today)
            tasks = tasks_for_date(sess, u.id, today)
            if not tasks: continue
            text = f"📅 План на {dstr(today)}\n\n" + format_grouped(tasks, header_date=dstr(today))
            try:
                bot.send_message(u.id, text)
            except Exception as e:
                log.error("digest send error: %s", e)
    finally:
        sess.close()

def job_check_reminders():
    sess = SessionLocal()
    try:
        now = now_local()
        due = (sess.query(Reminder)
               .filter(Reminder.fired==False,
                       or_(Reminder.date<now.date(),
                           and_(Reminder.date==now.date(), Reminder.time<=now.time())))
               .all())
        for r in due:
            t = sess.query(Task).filter(Task.id==r.task_id, Task.user_id==r.user_id).first()
            if not t: 
                r.fired = True
                continue
            try:
                bot.send_message(r.user_id, f"🔔 Напоминание: {t.text} (до {tstr(t.deadline)})")
            except Exception as e:
                log.error("reminder send error: %s", e)
            r.fired = True
        sess.commit()
    finally:
        sess.close()

def scheduler_loop():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)
    schedule.every(1).minutes.do(job_check_reminders)
    while True:
        schedule.run_pending()
        time.sleep(1)

# --------- START (polling) ---------
if __name__ == "__main__":
    try:
        bot.remove_webhook()
    except Exception:
        pass
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("Starting polling…")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50, skip_pending=True)
        except Exception as e:
            log.error("polling error: %s — retry in 3s", e)
            time.sleep(3)
