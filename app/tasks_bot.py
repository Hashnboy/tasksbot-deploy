# -*- coding: utf-8 -*-
import os, re, json, asyncio, time, hmac, hashlib, logging
from datetime import datetime, timedelta, date, time as dtime

import pytz
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from sqlalchemy import (create_engine, Column, Integer, String, Text, Date, Time,
                        DateTime, Boolean, func, and_, or_)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

# -------------------- ENV / Base --------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
DB_URL = os.getenv("DATABASE_URL")
TZ_NAME = os.getenv("TZ", "Europe/Moscow")
LOCAL_TZ = pytz.timezone(TZ_NAME)

if not TOKEN or not DB_URL:
    raise RuntimeError("Need TELEGRAM_TOKEN and DATABASE_URL envs")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")

bot = Bot(TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

Base = declarative_base()
engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))

# -------------------- Models --------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)  # Telegram chat id
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
    status = Column(String(40), default="")      # "", "выполнено"
    repeat_rule = Column(String(255), default="")
    source = Column(String(255), default="")
    is_repeating = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

class Subtask(Base):
    __tablename__ = "subtasks"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, index=True, nullable=False)
    text = Column(Text, nullable=False)
    status = Column(String(40), default="")      # "", "выполнено"

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

# -------------------- Helpers --------------------
PAGE = 8

def now_local():
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

def ensure_user(sess, uid: int, name=""):
    u = sess.query(User).filter_by(id=uid).first()
    if not u:
        u = User(id=uid, name=name or "")
        sess.add(u); sess.commit()
    return u

# inline payload with small HMAC
def mk_cb(action, **kwargs):
    payload = {"a": action, **kwargs}
    s = json.dumps(payload, ensure_ascii=False)
    sig = hmac.new(b"cb-key", s.encode(), hashlib.sha1).hexdigest()[:6]
    return f"{sig}|{s}"

def parse_cb(data):
    try:
        sig, s = data.split("|", 1)
        if hmac.new(b"cb-key", s.encode(), hashlib.sha1).hexdigest()[:6] != sig:
            return None
        return json.loads(s)
    except Exception:
        return None

def tasks_for_date(sess, uid:int, d:date):
    return (sess.query(Task)
            .filter(Task.user_id==uid, Task.date==d)
            .order_by(Task.category.asc(),
                      Task.subcategory.asc(),
                      Task.deadline.asc().nulls_last())
            ).all()

def tasks_for_week(sess, uid:int, base:date):
    days = [base + timedelta(days=i) for i in range(7)]
    return (sess.query(Task)
            .filter(Task.user_id==uid, Task.date.in_(days))
            .order_by(Task.date.asc(),
                      Task.category.asc(),
                      Task.subcategory.asc(),
                      Task.deadline.asc().nulls_last())
            ).all()

def short_line(t: Task, idx=None):
    dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
    p = f"{idx}. " if idx is not None else ""
    status = "✅" if t.status == "выполнено" else "⬜"
    return f"{p}{status} {t.category}/{t.subcategory or '—'}: {t.text[:40]}… (до {dl})"

def format_grouped(tasks, header_date=None):
    if not tasks: return "Задач нет."
    out = []
    if header_date:
        out.append(f"• {weekday_ru(tasks[0].date)} — {header_date}\n")
    cur_cat = cur_sub = None
    for t in sorted(tasks, key=lambda x: (x.category or "", x.subcategory or "",
                                          x.deadline or dtime.min, x.text)):
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
        kb.button(text=label, callback_data=mk_cb(action, id=tid))
    kb.adjust(1)
    nav_row = []
    if page > 1:
        nav_row.append(types.InlineKeyboardButton(
            text="⬅️", callback_data=mk_cb("page", p=page-1, pa=action)))
    nav_row.append(types.InlineKeyboardButton(
        text=f"{page}/{total}", callback_data="noop"))
    if page < total:
        nav_row.append(types.InlineKeyboardButton(
            text="➡️", callback_data=mk_cb("page", p=page+1, pa=action)))
    if nav_row:
        kb.row(*nav_row)
    return kb.as_markup()

def main_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📅 Сегодня"); kb.button(text="📆 Неделя")
    kb.button(text="➕ Добавить"); kb.button(text="✅ Я сделал…")
    kb.button(text="🔎 Найти"); kb.button(text="🧠 Ассистент")
    kb.button(text="🚚 Поставки"); kb.button(text="⚙️ Настройки")
    kb.adjust(2,2,2,2)
    return kb.as_markup(resize_keyboard=True)

# -------------------- Repeat expansion --------------------
WD = {"пн":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6}
RUS_WEEK = ["пн","вт","ср","чт","пт","сб","вс"]

def expand_repeats_for_date(sess, uid:int, target:date):
    """Создаёт инстансы задач из повторяющихся шаблонов на конкретную дату (если их ещё нет)."""
    templates = (sess.query(Task)
                 .filter(Task.user_id==uid, Task.is_repeating==True)
                 .all())
    created = 0
    for t in templates:
        rule = (t.repeat_rule or "").strip().lower()
        if not rule: continue

        make = False
        new_deadline = t.deadline
        # каждые N дней
        m = re.search(r"каждые\s+(\d+)\s*д", rule)
        if m:
            n = int(m.group(1))
            start = (t.created_at or datetime(2025,1,1, tzinfo=None)).date()
            delta = (target - start).days
            make = (delta >= 0 and delta % n == 0)

        # каждый вторник 12:00
        if not make and rule.startswith("каждый"):
            # пример: "каждый вторник 12:00"
            mm = re.search(r"каждый\s+([а-я]+)(?:\s+(\d{1,2}:\d{2}))?", rule)
            if mm:
                wd = mm.group(1)[:2]
                hhmm = mm.group(2)
                if wd in WD and target.weekday() == WD[wd]:
                    make = True
                    if hhmm: new_deadline = parse_time(hhmm)

        # по пн,ср[,чт] (время из шаблона)
        if not make and rule.startswith("по "):
            days = [x.strip() for x in rule.replace("по","").split(",")]
            days = [x[:2] for x in days if x]
            if target.weekday() in [WD.get(x,-1) for x in days]:
                make = True

        if not make:
            continue

        # дубль?
        exists = (sess.query(Task)
                  .filter(Task.user_id==uid,
                          Task.date==target,
                          Task.category==t.category,
                          Task.subcategory==t.subcategory,
                          Task.text==t.text)
                  ).first()
        if exists:
            continue

        sess.add(Task(user_id=uid, date=target,
                      category=t.category, subcategory=t.subcategory,
                      text=t.text, deadline=new_deadline,
                      status="", repeat_rule="", source="repeat-instance",
                      is_repeating=False))
        created += 1
    if created:
        sess.commit()
    return created

# -------------------- Suppliers auto-planning --------------------
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

# -------------------- Simple NLP add (без OpenAI) --------------------
def ai_parse_items(text, uid):
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
    # repeat?
    rep = ""
    m = re.search(r"каждые\s+(\d+)\s*д", tl)
    if m: rep = f"каждые {m.group(1)} дней"
    m2 = re.search(r"каждый\s+(пн|вт|ср|чт|пт|сб|вс)(?:\s+(\d{1,2}:\d{2}))?", tl)
    if m2: rep = f"каждый {m2.group(1)} {m2.group(2) or ''}".strip()
    return [{
        "date": ds, "time": time_s, "category": cat, "subcategory": sub,
        "task": text.strip(), "repeat": rep, "supplier": supplier, "user_id": uid
    }]

# -------------------- In‑memory ожидания ввода --------------------
# { user_id: {"mode": "edit_text"/"edit_deadline"/"add_subtask"/"set_reminder", "task_id": <id>} }
WAITING = {}

# -------------------- Handlers --------------------
@router.message(Command("start"))
async def cmd_start(m: types.Message):
    sess = SessionLocal()
    try:
        ensure_user(sess, m.chat.id, m.from_user.full_name if m.from_user else "")
    finally:
        sess.close()
    await m.answer("Привет! Я твой ассистент по задачам ✅", reply_markup=main_menu())

@router.message(F.text == "📅 Сегодня")
async def today(m: types.Message):
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
        await m.answer(header, reply_markup=main_menu(), reply_markup_inline=None)
        await m.answer("Навигация:", reply_markup=kb)
    finally:
        sess.close()

@router.message(F.text == "📆 Неделя")
async def week(m: types.Message):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        base = now_local().date()
        # расширяем повторы на каждый из 7 дней
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
async def add(m: types.Message):
    await m.answer("Опиши задачу одним сообщением (дату/время/категорию распознаю).")
    WAITING[m.chat.id] = {"mode":"add"}

@router.message(F.text == "✅ Я сделал…")
async def done_free(m: types.Message):
    await m.answer("Напиши что именно сделал (например: сделал заказы к-экспро центр).")
    WAITING[m.chat.id] = {"mode":"done_text"}

@router.message(F.text == "🔎 Найти")
async def search_prompt(m: types.Message):
    await m.answer("Что ищем? (текст/категория/подкатегория/ДД.ММ.ГГГГ)")
    WAITING[m.chat.id] = {"mode":"search"}

@router.message(F.text)
async def generic_text(m: types.Message):
    """Обрабатываем ожидаемые шаги: add / done_text / edits / subtask / reminder / search."""
    state = WAITING.get(m.chat.id)
    sess = SessionLocal()

    try:
        if state is None:
            # свободный ввод = добавление задач
            items = ai_parse_items(m.text.strip(), m.chat.id)
            created = 0
            for it in items:
                dt = parse_date(it["date"]) if it["date"] else now_local().date()
                tm = parse_time(it["time"]) if it["time"] else None
                t = Task(user_id=m.chat.id, date=dt, category=it["category"], subcategory=it["subcategory"],
                         text=it["task"], deadline=tm, repeat_rule=it["repeat"],
                         source=it["supplier"], is_repeating=bool(it["repeat"]))
                sess.add(t); created += 1
            sess.commit()
            await m.answer(f"✅ Добавлено: {created}", reply_markup=main_menu())
            return

        mode = state.get("mode")

        if mode == "add":
            items = ai_parse_items(m.text.strip(), m.chat.id)
            created = 0
            for it in items:
                dt = parse_date(it["date"]) if it["date"] else now_local().date()
                tm = parse_time(it["time"]) if it["time"] else None
                sess.add(Task(user_id=m.chat.id, date=dt,
                              category=it["category"], subcategory=it["subcategory"],
                              text=it["task"], deadline=tm, repeat_rule=it["repeat"],
                              source=it["supplier"], is_repeating=bool(it["repeat"])))
                created += 1
            sess.commit()
            WAITING.pop(m.chat.id, None)
            await m.answer(f"✅ Добавлено: {created}", reply_markup=main_menu())
            return

        if mode == "done_text":
            txt = m.text.lower()
            supplier = ""
            if any(x in txt for x in ["к-экспро","k-exp","к экспро"]): supplier = "К-Экспро"
            if "вылегжан" in txt: supplier = "ИП Вылегжанина"
            rows = tasks_for_date(sess, m.chat.id, now_local().date())
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
                created = plan_next(sess, m.chat.id, supplier, last.category, last.subcategory)
                if created: msg += " Запланирована приемка/следующий заказ."
            WAITING.pop(m.chat.id, None)
            await m.answer(msg, reply_markup=main_menu())
            return

        if mode == "edit_text":
            tid = state["task_id"]
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==m.chat.id).first()
            if not t: await m.answer("Задача не найдена"); WAITING.pop(m.chat.id, None); return
            t.text = m.text.strip(); sess.commit()
            WAITING.pop(m.chat.id, None)
            await m.answer("✏️ Текст обновлён.")
            return

        if mode == "edit_category":
            tid = state["task_id"]
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==m.chat.id).first()
            if not t: await m.answer("Задача не найдена"); WAITING.pop(m.chat.id, None); return
            t.category = m.text.strip(); sess.commit()
            WAITING.pop(m.chat.id, None)
            await m.answer("📂 Категория обновлена.")
            return

        if mode == "edit_subcategory":
            tid = state["task_id"]
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==m.chat.id).first()
            if not t: await m.answer("Задача не найдена"); WAITING.pop(m.chat.id, None); return
            t.subcategory = m.text.strip(); sess.commit()
            WAITING.pop(m.chat.id, None)
            await m.answer("📁 Подкатегория обновлена.")
            return

        if mode == "edit_deadline":
            tid = state["task_id"]
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==m.chat.id).first()
            if not t: await m.answer("Задача не найдена"); WAITING.pop(m.chat.id, None); return
            try:
                t.deadline = parse_time(m.text.strip())
            except Exception:
                await m.answer("Формат времени: ЧЧ:ММ"); return
            sess.commit()
            WAITING.pop(m.chat.id, None)
            await m.answer("⏰ Дедлайн обновлён.")
            return

        if mode == "add_subtask":
            tid = state["task_id"]
            if not m.text.strip():
                await m.answer("Пустой текст подзадачи.")
                return
            sess.add(Subtask(task_id=tid, text=m.text.strip(), status=""))
            sess.commit()
            WAITING.pop(m.chat.id, None)
            await m.answer("➕ Подзадача добавлена.")
            return

        if mode == "set_reminder":
            tid = state["task_id"]
            try:
                s = m.text.strip()
                dd, tt = s.split()
                rdate = parse_date(dd); rtime = parse_time(tt)
            except Exception:
                await m.answer("Формат напоминания: ДД.ММ.ГГГГ ЧЧ:ММ"); return
            sess.add(Reminder(user_id=m.chat.id, task_id=tid, date=rdate, time=rtime, fired=False))
            sess.commit()
            WAITING.pop(m.chat.id, None)
            await m.answer("⏰ Напоминание установлено.")
            return

        if mode == "search":
            q = m.text.strip().lower()
            filters = [Task.user_id==m.chat.id]
            # если дата в запросе
            md = re.search(r"(\d{2}\.\d{2}\.\d{4})", q)
            if md:
                filters.append(Task.date==parse_date(md.group(1)))
            # текстовые фильтры
            filters.append(or_(func.lower(Task.text).contains(q),
                               func.lower(Task.category).contains(q),
                               func.lower(Task.subcategory).contains(q)))
            rows = (sess.query(Task).filter(and_(*filters))
                    .order_by(Task.date.asc(),
                              Task.category.asc(),
                              Task.subcategory.asc()).limit(40).all())
            if not rows:
                await m.answer("Ничего не найдено.")
            else:
                parts = []
                for i, t in enumerate(rows, 1):
                    dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
                    parts.append(f"{i}. {dstr(t.date)} • {t.category}/{t.subcategory or '—'} • {t.text} (до {dl})")
                await m.answer("Результаты:\n\n" + "\n".join(parts))
            WAITING.pop(m.chat.id, None)
            return

    finally:
        sess.close()

# -------------------- Inline callbacks (карточка задачи) --------------------
def task_card_kb(tid:int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнить", callback_data=mk_cb("done", id=tid))
    kb.button(text="✏️ Текст", callback_data=mk_cb("edit_text", id=tid))
    kb.button(text="📂 Категория", callback_data=mk_cb("edit_category", id=tid))
    kb.button(text="📁 Подкатегория", callback_data=mk_cb("edit_subcategory", id=tid))
    kb.button(text="⏰ Дедлайн", callback_data=mk_cb("edit_deadline", id=tid))
    kb.button(text="🔔 Напоминание", callback_data=mk_cb("set_reminder", id=tid))
    kb.button(text="➕ Подзадача", callback_data=mk_cb("add_subtask", id=tid))
    kb.button(text="🗑 Удалить", callback_data=mk_cb("del", id=tid))
    kb.adjust(2,2,2,2)
    return kb.as_markup()

@router.callback_query(F.data)
async def cb(c: types.CallbackQuery):
    data = parse_cb(c.data) if c.data and c.data!="noop" else None
    if not data:
        await c.answer()
        return
    a = data.get("a")
    uid = c.message.chat.id
    sess = SessionLocal()
    try:
        if a in ("open","page"):
            # в этом потоке открываем/перелистываем список "сегодня"
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

        if a == "open":  # (оставлено для совместимости)
            tid = int(data.get("id"))

        if a == "done":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: await c.answer("Не найдено", show_alert=True); return
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
            if not t: await c.answer("Не найдено", show_alert=True); return
            sess.delete(t); sess.commit()
            await c.answer("Удалено", show_alert=True)
            return

        # --- проваливание: открыть карточку ---
        if a == "open_task":
            tid = int(data.get("id"))
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: await c.answer("Не найдено", show_alert=True); return
            dl = t.deadline.strftime("%H:%M") if t.deadline else "—"
            text = (f"<b>{t.text}</b>\n"
                    f"📅 {weekday_ru(t.date)} — {dstr(t.date)}\n"
                    f"📁 {t.category}/{t.subcategory or '—'}\n"
                    f"⏰ Дедлайн: {dl}\n"
                    f"📝 Статус: {t.status or '—'}")
            await c.message.answer(text, reply_markup=task_card_kb(t.id))
            await c.answer()
            return

        # Кнопки правок
        if a in ("edit_text","edit_category","edit_subcategory","edit_deadline","add_subtask","set_reminder"):
            WAITING[uid] = {"mode": a, "task_id": int(data.get("id"))}
            prompts = {
                "edit_text": "Введи новый текст задачи:",
                "edit_category": "Новая категория:",
                "edit_subcategory": "Новая подкатегория:",
                "edit_deadline": "Новое время (ЧЧ:ММ):",
                "add_subtask": "Текст подзадачи:",
                "set_reminder": "Напоминание в формате: ДД.ММ.ГГГГ ЧЧ:ММ",
            }
            await c.message.answer(prompts[a])
            await c.answer()
            return

    finally:
        sess.close()

# -------------------- Background jobs: reminders + digest --------------------
async def reminders_loop():
    await bot.delete_webhook(drop_pending_updates=True)  # на всякий случай
    while True:
        try:
            sess = SessionLocal()
            now = now_local()
            today = now.date(); cur_t = now.time()
            rows = (sess.query(Reminder)
                    .filter(Reminder.fired==False,
                            or_(Reminder.date < today,
                                and_(Reminder.date==today, Reminder.time <= cur_t)))
                    ).all()
            for r in rows:
                t = sess.query(Task).filter(Task.id==r.task_id, Task.user_id==r.user_id).first()
                txt = f"🔔 Напоминание: {t.text if t else 'задача'} ({dstr(r.date)} {r.time.strftime('%H:%M')})"
                try:
                    await bot.send_message(r.user_id, txt)
                except Exception as e:
                    log.warning("remind send failed: %s", e)
                r.fired = True
            if rows:
                sess.commit()
        except Exception as e:
            log.error("reminders_loop error: %s", e)
        finally:
            sess.close()
        await asyncio.sleep(30)

async def daily_digest_loop():
    """Отправляет ежедневный дайджест в 08:00 локального времени."""
    while True:
        now = now_local()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)

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
                    await bot.send_message(u.id, text)
                except Exception as e:
                    log.error("digest send error: %s", e)
        finally:
            sess.close()

# -------------------- Hook routers --------------------
dp.include_router(router)
