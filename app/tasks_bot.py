# -*- coding: utf-8 -*-
import os, re, json, time, hmac, hashlib, logging, threading
from datetime import datetime, timedelta, date, time as dtime
import pytz, schedule

# --- aiogram v3 ---
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# --- DB ---
from sqlalchemy import (create_engine, Column, Integer, String, Text, Date, Time,
                        DateTime, Boolean, func, and_, or_)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

# ========== ENV ==========
TOKEN       = os.getenv("TELEGRAM_TOKEN")
DB_URL      = os.getenv("DATABASE_URL")  # postgresql+psycopg2://user:pass@host:5432/db
TZ_NAME     = os.getenv("TZ", "Europe/Moscow")
OPENAI_KEY  = os.getenv("OPENAI_API_KEY")  # опционально

if not TOKEN or not DB_URL:
    raise RuntimeError("Нужны TELEGRAM_TOKEN и DATABASE_URL")

LOCAL_TZ = pytz.timezone(TZ_NAME)
PAGE = 8

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

# ========== OpenAI (optional) ==========
openai_client = None
if OPENAI_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_KEY)
        log.info("OpenAI assistant enabled")
    except Exception as e:
        log.warning("OpenAI disabled: %s", e)

# ========== DB ==========
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

# ========== BOT ==========
bot = Bot(TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
WAITING: dict[int, dict] = {}   # простейшая FSM на чат

# ========== Utils ==========
def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def dstr(d: date) -> str:
    return d.strftime("%d.%m.%Y")

def parse_date(s: str) -> date:
    return datetime.strptime(s, "%d.%m.%Y").date()

def parse_time(s: str) -> dtime:
    return datetime.strptime(s, "%H:%M").time()

def weekday_ru(d: date) -> str:
    names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    return names[d.weekday()]

def ensure_user(sess, uid, name=""):
    u = sess.query(User).filter_by(id=uid).first()
    if not u:
        u = User(id=uid, name=name or "")
        sess.add(u); sess.commit()
    return u

def main_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📅 Сегодня"); kb.button(text="📆 Неделя")
    kb.button(text="➕ Добавить"); kb.button(text="✅ Я сделал…")
    kb.button(text="🚚 Поставки"); kb.button(text="🧠 Ассистент")
    kb.adjust(2,2,2)
    return kb.as_markup(resize_keyboard=True)

# --- inline callback pack ---
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

# ========== Suppliers rules ==========
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

# ========== Repeat rules ==========
def matches_every_n_days(created: date, candidate: date, n: int) -> bool:
    base = created or date(2025,1,1)
    return (candidate - base).days % n == 0

def expand_repeats_for_date(sess, uid:int, the_date:date):
    templates = (sess.query(Task)
                 .filter(Task.user_id==uid, Task.is_repeating==True)
                 .all())
    for t in templates:
        rr = (t.repeat_rule or "").lower()
        hit = False
        at_time = t.deadline
        if "каждые" in rr:
            m = re.findall(r"\d+", rr); n = int(m[0]) if m else 2
            hit = matches_every_n_days(t.created_at.date() if t.created_at else the_date, the_date, n)
        elif rr.startswith("каждый"):
            # «каждый вторник 12:00»
            wd_map = {"пн":0,"понедельник":0,"вт":1,"вторник":1,"ср":2,"среда":2,"чт":3,"четверг":3,"пт":4,"пятница":4,"сб":5,"суббота":5,"вс":6,"воскресенье":6}
            for k,v in wd_map.items():
                if k in rr:
                    hit = (the_date.weekday()==v)
                    break
            hhmm = re.search(r"(\d{1,2}:\d{2})", rr)
            if hhmm: at_time = parse_time(hhmm.group(1))
        elif rr.startswith("по "):
            # «по пн,ср[, ...]»
            wd_map = {"пн":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6}
            lst = [s.strip() for s in rr.replace("по ","").split(",")]
            days = [wd_map.get(x) for x in lst if wd_map.get(x) is not None]
            hit = (the_date.weekday() in days)

        if not hit:
            continue

        # дубль на этот день?
        exists = (sess.query(Task).filter(
            Task.user_id==uid,
            Task.date==the_date,
            Task.text==t.text,
            Task.category==t.category,
            Task.subcategory==t.subcategory
        ).first())
        if exists: continue

        sess.add(Task(
            user_id=uid, date=the_date,
            category=t.category, subcategory=t.subcategory,
            text=t.text, deadline=at_time,
            status="", repeat_rule="", is_repeating=False,
            source="repeat-instance"
        ))
    sess.commit()

# ========== Formatting ==========
def short_line(t: Task, idx=None):
    dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
    p = f"{idx}. " if idx is not None else ""
    return f"{p}{t.category}/{t.subcategory or '—'}: {t.text[:40]}… (до {dl})"

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
        if t.deadline: line += f"  <i>(до {t.deadline.strftime('%H:%M')})</i>"
        out.append(line)
    return "\n".join(out)

def page_kb(items, page, total, action="open"):
    kb = InlineKeyboardBuilder()
    for label, tid in items:
        kb.button(text=label, callback_data=mk_cb(action, id=int(tid)))
    # nav
    nav = InlineKeyboardBuilder()
    if page>1: nav.button(text="⬅️", callback_data=mk_cb("page", p=page-1, pa=action))
    nav.button(text=f"{page}/{total}", callback_data="noop")
    if page<total: nav.button(text="➡️", callback_data=mk_cb("page", p=page+1, pa=action))
    kb.adjust(1)
    kb.row(*nav.buttons)
    return kb.as_markup()

def task_card_kb(tid:int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнить", callback_data=mk_cb("done", id=tid))
    kb.button(text="🗑 Удалить",   callback_data=mk_cb("del", id=tid))
    kb.adjust(1)
    return kb.as_markup()

# ========== Data access ==========
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

# ========== NLP add ==========
def ai_parse_items(text, uid):
    # OpenAI JSON parser
    if openai_client:
        try:
            sys = ("Ты парсер задач. Верни ТОЛЬКО JSON-массив объектов: "
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
    # fallback heuristics
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

# ========== Week context for assistant ==========
def build_week_context(sess, uid:int):
    base = now_local().date()
    rows = tasks_for_week(sess, uid, base)
    by = {}
    for t in rows: by.setdefault(dstr(t.date), []).append(t)
    blocks = []
    for ds in sorted(by.keys(), key=lambda s: parse_date(s)):
        lines = [f"{ds}:"]
        for t in by[ds]:
            dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
            lines.append(f"- {t.category}/{t.subcategory or '—'} • {t.text} (до {dl}) [{t.status or '—'}]")
        blocks.append("\n".join(lines))
    ctx = "\n\n".join(blocks) or "Нет задач на неделю."
    ctx_lines = ctx.splitlines()
    if len(ctx_lines) > 200:
        ctx = "\n".join(ctx_lines[:200]) + "\n…"
    return ctx

# ========== Handlers ==========
@router.message(F.text == "/start")
async def cmd_start(message: Message):
    sess = SessionLocal()
    try:
        ensure_user(sess, message.chat.id, message.from_user.full_name if message.from_user else "")
    finally:
        sess.close()
    await message.answer("Привет! Я твой ассистент по задачам.", reply_markup=main_menu())

@router.message(F.text == "📅 Сегодня")
async def today(m: Message):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        expand_repeats_for_date(sess, uid, now_local().date())
        rows = tasks_for_date(sess, uid, now_local().date())
        if not rows:
            await m.answer(f"📅 Задачи на {dstr(now_local().date())}\n\nЗадач нет.", reply_markup=main_menu()); return
        items = [(short_line(t, i), t.id) for i,t in enumerate(rows, start=1)]
        total = (len(items)+PAGE-1)//PAGE
        kb = page_kb(items[:PAGE], 1, total, "open")
        header = f"📅 Задачи на {dstr(now_local().date())}\n\n" + format_grouped(rows, header_date=dstr(now_local().date())) + "\n\nОткрой карточку:"
        await m.answer(header, reply_markup=kb)
    finally:
        sess.close()

@router.message(F.text == "📆 Неделя")
async def week(m: Message):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        base = now_local().date()
        # развернём повторяемость на каждый из 7 дней
        for i in range(7):
            expand_repeats_for_date(sess, uid, base + timedelta(days=i))
        rows = tasks_for_week(sess, uid, base)
        if not rows:
            await m.answer("На неделю задач нет.", reply_markup=main_menu()); return
        by = {}
        for t in rows: by.setdefault(dstr(t.date), []).append(t)
        parts = []
        for ds in sorted(by.keys(), key=lambda s: parse_date(s)):
            parts.append(format_grouped(by[ds], header_date=ds)); parts.append("")
        await m.answer("\n".join(parts), reply_markup=main_menu())
    finally:
        sess.close()

@router.message(F.text == "➕ Добавить")
async def add(m: Message):
    WAITING[m.chat.id] = {"mode":"add"}
    await m.answer("Опиши задачу одним сообщением (дату/время/категорию распознаю).")

@router.message(F.text == "✅ Я сделал…")
async def done_free(m: Message):
    WAITING[m.chat.id] = {"mode":"done_text"}
    await m.answer("Напиши что именно сделал (например: сделал заказы к-экспро центр).")

# --- Supplies menu ---
@router.message(F.text == "🚚 Поставки")
async def sup_menu(m: Message):
    kb = ReplyKeyboardBuilder()
    kb.button(text="📦 Заказы сегодня"); kb.button(text="🆕 Добавить поставщика")
    kb.button(text="📜 Правила поставщиков"); kb.button(text="⬅️ Назад")
    kb.adjust(2,2)
    await m.answer("Меню «Поставки»:", reply_markup=kb.as_markup(resize_keyboard=True))

@router.message(F.text == "⬅️ Назад")
async def back_main(m: Message):
    await m.answer("Главное меню:", reply_markup=main_menu())

@router.message(F.text == "📦 Заказы сегодня")
async def orders_today(m: Message):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        expand_repeats_for_date(sess, uid, now_local().date())
        rows = tasks_for_date(sess, uid, now_local().date())
        orders = [t for t in rows if "заказ" in (t.text or "").lower()]
        if not orders:
            await m.answer("На сегодня нет задач типа «заказ»."); return
        items = [(short_line(t, i), t.id) for i,t in enumerate(orders, start=1)]
        total = (len(items)+PAGE-1)//PAGE
        kb = page_kb(items[:PAGE], 1, total, "open")
        await m.answer("Сегодняшние заказы — выбери задачу:", reply_markup=kb)
    finally:
        sess.close()

@router.message(F.text == "🆕 Добавить поставщика")
async def sup_add_prompt(m: Message):
    WAITING[m.chat.id] = {"mode":"add_supplier"}
    await m.answer("Введи поставщика одной строкой:\n"
                   "Название; правило; дедлайн(HH:MM); emoji; delivery_offset; shelf_days; auto(1/0); active(1/0)\n\n"
                   "Пример:\nК-Экспро; каждые 2 дня; 14:00; 📦; 1; 0; 1; 1")

@router.message(F.text == "📜 Правила поставщиков")
async def sup_list(m: Message):
    sess = SessionLocal()
    try:
        rows = sess.query(Supplier).order_by(func.lower(Supplier.name).asc()).all()
        if not rows:
            await m.answer("Поставщики пока не заведены. Добавь через «🆕 Добавить поставщика»."); return
        lines = []
        for s in rows:
            lines.append(f"{'✅' if s.active else '⛔️'} <b>{s.name}</b> — {s.rule or '—'}, дедлайн {s.order_deadline or '—'}, "
                         f"offset {s.delivery_offset_days}, shelf {s.shelf_days}, {s.emoji}")
        await m.answer("\n".join(lines))
    finally:
        sess.close()

# --- Assistant ---
@router.message(F.text == "🧠 Ассистент")
async def assistant_prompt(m: Message):
    WAITING[m.chat.id] = {"mode":"assistant"}
    await m.answer("Что подсказать? (например: «расставь приоритеты на завтра», «что критично сегодня?»)")

# --- generic text (FSM-lite) ---
@router.message(F.text)
async def generic_text(m: Message):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        state = WAITING.get(uid)
        if not state:
            await m.answer("Я тебя понял. Используй меню ниже 👇", reply_markup=main_menu())
            return

        mode = state.get("mode")

        if mode == "add":
            items = ai_parse_items(m.text.strip(), uid)
            created = 0
            for it in items:
                dt = parse_date(it["date"]) if it["date"] else now_local().date()
                tm = parse_time(it["time"]) if it["time"] else None
                if it["repeat"]:
                    # шаблон повторяемости
                    t = Task(user_id=uid, date=dt, category=it["category"], subcategory=it["subcategory"],
                             text=it["task"], deadline=tm, repeat_rule=it["repeat"], source=it["supplier"],
                             is_repeating=True)
                else:
                    t = Task(user_id=uid, date=dt, category=it["category"], subcategory=it["subcategory"],
                             text=it["task"], deadline=tm, repeat_rule="", source=it["supplier"],
                             is_repeating=False)
                sess.add(t); created += 1
            sess.commit()
            WAITING.pop(uid, None)
            await m.answer(f"✅ Добавлено объектов: {created}", reply_markup=main_menu())
            return

        if mode == "done_text":
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
            WAITING.pop(uid, None)
            await m.answer(msg, reply_markup=main_menu())
            return

        if mode == "add_supplier":
            try:
                parts = [p.strip() for p in m.text.split(";")]
                if len(parts) != 8:
                    await m.answer("Нужно 8 полей через «;». Пример:\nК-Экспро; каждые 2 дня; 14:00; 📦; 1; 0; 1; 1")
                    return
                name, rule, deadline, emoji, off, shelf, auto, active = parts
                off = int(off); shelf = int(shelf); auto = bool(int(auto)); active = bool(int(active))
                sess.merge(Supplier(
                    name=name,
                    rule=rule,
                    order_deadline=deadline,
                    emoji=emoji,
                    delivery_offset_days=off,
                    shelf_days=shelf,
                    auto=auto,
                    active=active
                ))
                sess.commit()
                WAITING.pop(uid, None)
                await m.answer(f"✅ Поставщик «{name}» сохранён.", reply_markup=main_menu())
            except Exception as e:
                await m.answer(f"Ошибка: {e}")
            return

        if mode == "assistant":
            question = m.text.strip()
            sess2 = SessionLocal()
            try:
                ctx = build_week_context(sess2, uid)
            finally:
                sess2.close()

            if openai_client:
                try:
                    sys = ("Ты — краткий помощник по планированию. "
                           "Дай компактный план/приоритеты и риски на русском. "
                           "Стиль: маркированные пункты, короткие фразы, без воды.")
                    user_msg = f"Вопрос: {question}\n\nКонтекст ближайшей недели:\n{ctx}"
                    resp = openai_client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role":"system","content":sys},
                                  {"role":"user","content":user_msg}],
                        temperature=0.2,
                    )
                    ans = resp.choices[0].message.content.strip()
                    await m.answer(ans or "Не удалось получить ответ.")
                except Exception as e:
                    await m.answer(f"Ассистент недоступен ({e}).")
            else:
                await m.answer("OPENAI_API_KEY не задан — простая эвристика:\n"
                               "• Сначала срочные дедлайны (сегодня/завтра)\n"
                               "• Далее заказы поставщиков до их дедлайна (обычно 14:00)\n"
                               "• Затем важные категории/объёмные задачи")
            WAITING.pop(uid, None)
            return

    finally:
        sess.close()

# --- callbacks ---
@router.callback_query(F.data)
async def cb(c: CallbackQuery):
    if not c.data or c.data == "noop":
        await c.answer(); return
    data = parse_cb(c.data)
    if not data:
        await c.answer(); return
    a = data.get("a")
    uid = c.message.chat.id
    sess = SessionLocal()
    try:
        if a == "page":
            rows = tasks_for_date(sess, uid, now_local().date())
            items = [(short_line(t, i), t.id) for i,t in enumerate(rows, start=1)]
            page = int(data.get("p",1))
            total = max(1, (len(items)+PAGE-1)//PAGE)
            page = max(1, min(page, total))
            slice_items = items[(page-1)*PAGE:page*PAGE]
            kb = page_kb(slice_items, page, total, "open")
            try:
                await c.message.edit_reply_markup(reply_markup=kb)
            except Exception:
                pass
            await c.answer()
            return

        if a == "open":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t:
                await c.answer("Не найдено", show_alert=True); return
            dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
            text = (f"<b>{t.text}</b>\n"
                    f"📅 {weekday_ru(t.date)} — {dstr(t.date)}\n"
                    f"📁 {t.category}/{t.subcategory or '—'}\n"
                    f"⏰ Дедлайн: {dl}\n"
                    f"📝 Статус: {t.status or '—'}")
            await c.message.answer(text, reply_markup=task_card_kb(t.id))
            await c.answer()
            return

        if a == "done":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t:
                await c.answer("Не найдено", show_alert=True); return
            t.status = "выполнено"; sess.commit()
            sup = ""
            low = (t.text or "").lower()
            if any(x in low for x in ["к-экспро","k-exp","к экспро"]): sup="К-Экспро"
            if "вылегжан" in low: sup="ИП Вылегжанина"
            msg = "✅ Готово."
            if sup:
                created = plan_next(sess, uid, sup, t.category, t.subcategory)
                if created: msg += " Запланирована приёмка/следующий заказ."
            await c.answer(msg, show_alert=True)
            return

        if a == "del":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t:
                await c.answer("Не найдено", show_alert=True); return
            sess.delete(t); sess.commit()
            await c.answer("Удалено", show_alert=True)
            return
    finally:
        sess.close()

# ========== Schedulers ==========
def job_daily_digest():
    sess = SessionLocal()
    try:
        today = now_local().date()
        users = sess.query(User).all()
        for u in users:
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

def job_reminders():
    sess = SessionLocal()
    try:
        now = now_local()
        today = now.date()
        tt = now.time().replace(second=0, microsecond=0)
        rows = (sess.query(Reminder)
                .filter(Reminder.fired==False,
                        or_(Reminder.date < today,
                            and_(Reminder.date==today, Reminder.time<=tt)))
                .all())
        for r in rows:
            try:
                bot.send_message(r.user_id, f"⏰ Напоминание по задаче #{r.task_id} — {dstr(r.date)} {r.time.strftime('%H:%M')}")
            except Exception:
                pass
            r.fired = True
        sess.commit()
    finally:
        sess.close()

def scheduler_loop():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)
    schedule.every(1).minutes.do(job_reminders)
    while True:
        schedule.run_pending()
        time.sleep(1)

# ========== bootstrap ==========
dp.include_router(router)

def start_schedulers_once():
    th = threading.Thread(target=scheduler_loop, daemon=True)
    th.start()

start_schedulers_once()
