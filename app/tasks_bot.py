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

Новые возможности:
  • Directions/Rules: направления + правила (daily/weekdays/every_n_days) c notify_time и ранним уведомлением
  • Расширение задач: priority, task_type, direction_id, supplier_id, auto_key (анти‑дубли)
  • Безопасные callback’и (CALLBACK_SECRET), /health, сид направлений
Env:
  TELEGRAM_TOKEN, DATABASE_URL, TZ (default Europe/Moscow), OPENAI_API_KEY (optional),
  CALLBACK_SECRET (обязательно для подписи), ADMIN_IDS (опц., кому доступен /health)
"""

import os, re, json, time, hmac, hashlib, logging, threading
from datetime import datetime, timedelta, date, time as dtime
from typing import Optional

import pytz
import schedule

from telebot import TeleBot, types, apihelper
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Date, Time, DateTime, Boolean,
    ForeignKey, func, UniqueConstraint, Index, and_, or_
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session, relationship

# --------- ENV / LOG ---------
API_TOKEN   = os.getenv("TELEGRAM_TOKEN")
DB_URL      = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TZ_NAME     = os.getenv("TZ", "Europe/Moscow")
CALLBACK_SECRET = os.getenv("CALLBACK_SECRET", "change-me").encode("utf-8")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()}

if not API_TOKEN or not DB_URL:
    raise RuntimeError("Need TELEGRAM_TOKEN and DATABASE_URL envs")

LOCAL_TZ = pytz.timezone(TZ_NAME)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tasksbot")
log.info("Boot TasksBot v2025-08-21-rules-ui")

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

class Direction(Base):
    __tablename__ = "directions"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    emoji = Column(String(8), default="📂")
    sort_order = Column(Integer, default=100)

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
    direction_id = Column(Integer, ForeignKey("directions.id", ondelete="SET NULL"), nullable=True)
    direction = relationship("Direction", lazy="joined")
    created_at = Column(DateTime, server_default=func.now())

class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint('user_id','date','text','category','subcategory','is_repeating', name='uq_task_day'),
        Index("idx_tasks_user_date_status", "user_id", "date", "status"),
        Index("idx_tasks_user_type", "user_id", "task_type"),
        UniqueConstraint('user_id','date','auto_key', name='uq_tasks_auto_key_date_user'),
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
    # NEW:
    priority   = Column(String(20), default="medium")   # high|medium|low|future
    task_type  = Column(String(20), default="todo")     # todo|purchase
    direction_id = Column(Integer, ForeignKey("directions.id", ondelete="SET NULL"), nullable=True)
    supplier_id  = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    auto_key  = Column(String(160), nullable=True)
    direction = relationship("Direction", lazy="joined")
    supplier  = relationship("Supplier", lazy="joined")
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

class Rule(Base):
    __tablename__ = "rules"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    direction_id = Column(Integer, ForeignKey("directions.id", ondelete="SET NULL"))
    type = Column(String(20), nullable=False)  # 'todo'|'purchase'
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"))
    periodicity = Column(String(20), nullable=False)  # 'daily'|'weekdays'|'every_n_days'
    every_n = Column(Integer, nullable=True)
    weekdays = Column(String(20), nullable=True)      # "пн,ср,пт"
    notify_time = Column(Time, nullable=True)
    notify_before_min = Column(Integer, default=0)
    auto_create = Column(Boolean, default=True)
    title = Column(Text, default="")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    __table_args__ = (Index("idx_rules_user_active", "user_id", "active"),)

Base.metadata.create_all(bind=engine)

def migrate_db():
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE users  ADD COLUMN IF NOT EXISTS tz VARCHAR(64) NOT NULL DEFAULT 'Europe/Moscow';"
        )
        conn.exec_driver_sql(
            "ALTER TABLE users  ADD COLUMN IF NOT EXISTS digest_08 BOOLEAN NOT NULL DEFAULT true;"
        )
        conn.exec_driver_sql(
            "ALTER TABLE tasks  ADD COLUMN IF NOT EXISTS assignee_id INTEGER;"
        )
        # NEW columns/indexes (idempotent)
        conn.exec_driver_sql(
            "ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS direction_id INT NULL REFERENCES directions(id) ON DELETE SET NULL;"
        )
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority VARCHAR(20) DEFAULT 'medium';"
        )
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_type VARCHAR(20) DEFAULT 'todo';"
        )
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS direction_id INT NULL REFERENCES directions(id) ON DELETE SET NULL;"
        )
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS supplier_id INT NULL REFERENCES suppliers(id) ON DELETE SET NULL;"
        )
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS auto_key VARCHAR(160) NULL;"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_tasks_user_date_status ON tasks(user_id, date, status);"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_tasks_user_type ON tasks(user_id, task_type);"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_rules_user_active ON rules(user_id, active);"
        )
        # уникальность авто‑ключа
        conn.exec_driver_sql("""
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'uq_tasks_auto_key_date_user'
  ) THEN
    ALTER TABLE tasks
    ADD CONSTRAINT uq_tasks_auto_key_date_user UNIQUE (user_id, date, auto_key);
  END IF;
END $$;
""")
migrate_db()

# --------- BOT ---------
bot = TeleBot(API_TOKEN, parse_mode="HTML")
PAGE = 8
from org_ext import OrgExt
org = OrgExt(bot)   # можно передать твои engine/SessionLocal, если уже есть
org.init_db()       # создаст таблицы, точки, и зальёт шаблоны отчётов
org.register()      # повесит хендлеры: приглашения/роли, чек-ин/аут, отчёты, настройки, статистика
LAST_TICK: Optional[datetime] = None

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

def _cb_sign(s:str)->str:
    return hashlib.sha1(CALLBACK_SECRET + s.encode("utf-8")).hexdigest()[:6]

def mk_cb(action, **kwargs):
    payload = {"v":1, "a": action, **kwargs}
    s = json.dumps(payload, ensure_ascii=False, separators=(",",":"))
    sig = _cb_sign(s)
    return f"{sig}|{s}"

def parse_cb(data):
    try:
        sig, s = data.split("|", 1)
        if _cb_sign(s) != sig:
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

# безопасная отправка (бэкофф)
def send_safe(text, chat_id, **kwargs):
    delay = 0.5
    for _ in range(6):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except apihelper.ApiTelegramException as e:
            sc = getattr(e.result, "status_code", None)
            if sc in (429, 500, 502, 503, 504):
                time.sleep(delay); delay *= 2; continue
            time.sleep(delay); delay *= 2
        except Exception:
            time.sleep(delay); delay *= 2
    return bot.send_message(chat_id, text[:4000], **kwargs)

# --------- Suppliers rules / plan (твоя логика оставлена) ---------
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
                        deadline=parse_time("10:00"), task_type="purchase", priority="high"))
        sess.merge(Task(user_id=user_id, date=next_order, category=category, subcategory=subcategory,
                        text=f"{rule['emoji']} Заказать {supplier} ({subcategory or '—'})",
                        deadline=parse_time(rule["deadline"]), task_type="purchase", priority="high"))
        sess.commit()
        out = [("delivery", delivery), ("order", next_order)]
    else:
        delivery = today + timedelta(days=rule["delivery_offset"])
        next_order = delivery + timedelta(days=max(1, (rule.get("shelf_days",3)-1)))
        sess.merge(Task(user_id=user_id, date=delivery, category=category, subcategory=subcategory,
                        text=f"{rule['emoji']} Принять поставку {supplier} ({subcategory or '—'})",
                        deadline=parse_time("11:00"), task_type="purchase", priority="high"))
        sess.merge(Task(user_id=user_id, date=next_order, category=category, subcategory=subcategory,
                        text=f"{rule['emoji']} Заказать {supplier} ({subcategory or '—'})",
                        deadline=parse_time(rule["deadline"]), task_type="purchase", priority="high"))
        sess.commit()
        out = [("delivery", delivery), ("order", next_order)]
    return out

# --------- Repeats (твоя логика оставлена) ---------
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
    pr_emoji = {"high":"🔴","medium":"🟡","low":"🟢","future":"⏳"}.get(t.priority or "medium","🟡")
    return f"{p}{pr_emoji} {t.category}/{t.subcategory or '—'}: {t.text[:40]}… (до {tstr(t.deadline)}){assignee}"

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
        pr_emoji = {"high":"🔴","medium":"🟡","low":"🟢","future":"⏳"}.get(t.priority or "medium","🟡")
        line = f"    └ {icon} {pr_emoji} {t.text}"
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
    kb.row("⚙️ Правила","👤 Профиль","🧩 Зависимости","🤝 Делегирование")
    return kb

# --------- NLP add (твоя логика) ---------
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
        # сид направлений (если пусто)
        if sess.query(Direction).count() == 0:
            for name,emoji,sort in [("Кофейня","☕",10),("WB","📦",20),("Табачка","🚬",30),("Личное","🏠",40)]:
                sess.add(Direction(name=name, emoji=emoji, sort_order=sort))
            sess.commit()
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
            # NEW: дефолты для новых полей
            t = Task(user_id=uid, date=dt, category=it["category"], subcategory=it["subcategory"],
                     text=it["task"], deadline=tm, repeat_rule=it["repeat"], source=it["supplier"],
                     is_repeating=is_rep, task_type=("purchase" if it.get("supplier") else "todo"),
                     priority=("high" if it.get("supplier") else "medium"))
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
            pr_emoji = {"high":"🔴","medium":"🟡","low":"🟢","future":"⏳"}.get(t.priority or "medium","🟡")
            text = (f"<b>{t.text}</b>\n"
                    f"📅 {weekday_ru(t.date)} — {dstr(t.date)}\n"
                    f"📁 {t.category}/{t.subcategory or '—'}\n"
                    f"⚑ Тип: {t.task_type or 'todo'}  • Приоритет: {pr_emoji}\n"
                    f"⏰ Дедлайн: {dl}\n"
                    f"📝 Статус: {t.status or '—'}{dep_text}")
            kb = types.InlineKeyboardMarkup()
            kb.row(types.InlineKeyboardButton("✅ Выполнить", callback_data=mk_cb("done", id=tid)),
                   types.InlineKeyboardButton("🗑 Удалить", callback_data=mk_cb("del", id=tid)))
            kb.row(types.InlineKeyboardButton("✏️ Дедлайн", callback_data=mk_cb("setdl", id=tid)),
                   types.InlineKeyboardButton("⏰ Напоминание", callback_data=mk_cb("rem", id=tid)))
            # быстрый перенос (минимум)
            kb.row(types.InlineKeyboardButton("📤 Сегодня", callback_data=mk_cb("mv", id=tid, to="today")),
                   types.InlineKeyboardButton("📤 Завтра",  callback_data=mk_cb("mv", id=tid, to="tomorrow")),
                   types.InlineKeyboardButton("📤 +1д",     callback_data=mk_cb("mv", id=tid, to="+1")))
            bot.answer_callback_query(c.id)
            bot.send_message(uid, text, reply_markup=kb)
            return
        if a=="mv":
            tid = int(data.get("id")); to = data.get("to")
            t = sess.query(Task).filter(Task.id==tid, Task.user_id==uid).first()
            if not t: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            base = now_local().date()
            if to=="today": t.date = base
            elif to=="tomorrow": t.date = base + timedelta(days=1)
            elif to=="+1": t.date = t.date + timedelta(days=1)
            sess.commit()
            bot.answer_callback_query(c.id, "Перенесено"); return
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
# ===== Rules UI (список/добавить/управление) =====
RULE_WIZ = {}      # uid -> {"step":..., "data":{...}}
RULE_EDIT = {}     # uid -> {"field":..., "rule_id":...}

def rule_brief(r: Rule) -> str:
    per = {"daily":"ежедневно","weekdays":f"по {r.weekdays or '—'}","every_n_days":f"каждые {r.every_n or '?'} дн."}.get(r.periodicity, r.periodicity)
    auto = "on" if r.auto_create else "off"
    act  = "✅" if r.active else "⛔"
    tpe  = "📌" if r.type=="todo" else "📦"
    dirn = "-"
    if r.direction and r.direction.name:
        dirn = f"{r.direction.emoji or '📂'} {r.direction.name}"
    sup  = "-"
    if r.supplier:
        sup = r.supplier.name
    nt   = tstr(r.notify_time) if r.notify_time else "—"
    nb   = r.notify_before_min or 0
    ttl  = (r.title or "").strip() or "(без названия)"
    return (f"{act} {tpe} <b>{ttl}</b>\n"
            f"• Напр.: {dirn}\n"
            f"• Поставщик: {sup}\n"
            f"• Периодичность: {per}\n"
            f"• Время: {nt}  • Ранний пинг: {nb} мин\n"
            f"• Автосоздание: {auto}")

def rules_list_kb(rules, page=1, per_page=6):
    total_pages = max(1, (len(rules)+per_page-1)//per_page)
    page = max(1, min(page, total_pages))
    start, end = (page-1)*per_page, min(len(rules), page*per_page)
    kb = types.InlineKeyboardMarkup()
    for r in rules[start:end]:
        row1 = [
            types.InlineKeyboardButton("✏️", callback_data=mk_cb("r_edit", id=r.id)),
            types.InlineKeyboardButton("🔔", callback_data=mk_cb("r_time", id=r.id)),
            types.InlineKeyboardButton("🔄", callback_data=mk_cb("r_auto", id=r.id)),
            types.InlineKeyboardButton("⏯", callback_data=mk_cb("r_active", id=r.id)),
            types.InlineKeyboardButton("🗑", callback_data=mk_cb("r_del", id=r.id)),
        ]
        kb.row(*row1)
        kb.row(types.InlineKeyboardButton(f"ℹ️ #{r.id}", callback_data=mk_cb("r_info", id=r.id)))
    nav = []
    if page>1:
        nav.append(types.InlineKeyboardButton("⬅️", callback_data=mk_cb("r_page", p=page-1)))
    nav.append(types.InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page<total_pages:
        nav.append(types.InlineKeyboardButton("➡️", callback_data=mk_cb("r_page", p=page+1)))
    if nav: kb.row(*nav)
    kb.row(types.InlineKeyboardButton("➕ Добавить правило", callback_data=mk_cb("r_add")))
    return kb, page, total_pages

@bot.message_handler(func=lambda msg: msg.text == "⚙️ Правила")
def rules_menu(m):
    sess = SessionLocal()
    try:
        uid = m.chat.id
        rules = (sess.query(Rule)
                 .filter(Rule.user_id==uid)
                 .order_by(Rule.active.desc(), Rule.id.desc())
                 .all())
        if not rules:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("➕ Добавить правило", callback_data=mk_cb("r_add")))
            bot.send_message(uid, "Правил пока нет.", reply_markup=main_menu())
            bot.send_message(uid, "Создать новое правило:", reply_markup=kb)
            return
        text = "⚙️ Твои правила:"
        kb, page, total = rules_list_kb(rules, 1)
        bot.send_message(uid, text, reply_markup=main_menu())
        # отправим краткие карточки пачкой
        for r in rules[:6]:
            bot.send_message(uid, rule_brief(r), reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Открыть действия", callback_data=mk_cb("r_info", id=r.id))
            ))
        bot.send_message(uid, "Навигация по правилам:", reply_markup=kb)
    finally:
        sess.close()

# ---- Wizard "add rule" ----
def r_wiz_reset(uid): RULE_WIZ.pop(uid, None)
def r_wiz(uid): return RULE_WIZ.setdefault(uid, {"step":"dir","data":{}})

def ask_direction(chat_id):
    sess = SessionLocal()
    try:
        dirs = sess.query(Direction).order_by(Direction.sort_order.asc()).all()
    finally:
        sess.close()
    kb = types.InlineKeyboardMarkup(row_width=2)
    for d in dirs:
        kb.add(types.InlineKeyboardButton(f"{d.emoji or '📂'} {d.name}", callback_data=mk_cb("r_dir", id=d.id)))
    kb.add(types.InlineKeyboardButton("— Без направления —", callback_data=mk_cb("r_dir", id=0)))
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data=mk_cb("r_cancel")))
    send_safe("Шаг 1/8: Выбери направление", chat_id, reply_markup=kb)

def ask_type(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("📌 Обычное дело", callback_data=mk_cb("r_type", v="todo")),
        types.InlineKeyboardButton("📦 Закупка", callback_data=mk_cb("r_type", v="purchase")),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=mk_cb("r_back")))
    send_safe("Шаг 2/8: Выбери тип", chat_id, reply_markup=kb)

def ask_supplier(chat_id, direction_id):
    sess = SessionLocal()
    try:
        q = sess.query(Supplier)
        if direction_id:
            q = q.filter(Supplier.direction_id==direction_id)
        sups = q.order_by(Supplier.name.asc()).limit(50).all()
    finally:
        sess.close()
    kb = types.InlineKeyboardMarkup(row_width=2)
    for s in sups:
        kb.add(types.InlineKeyboardButton(s.name, callback_data=mk_cb("r_sup", id=s.id)))
    kb.add(types.InlineKeyboardButton("— Без поставщика —", callback_data=mk_cb("r_sup", id=0)))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=mk_cb("r_back")))
    send_safe("Шаг 3/8: Выбери поставщика", chat_id, reply_markup=kb)

def ask_periodicity(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Ежедневно", callback_data=mk_cb("r_per", v="daily")),
        types.InlineKeyboardButton("По дням недели", callback_data=mk_cb("r_per", v="weekdays")),
        types.InlineKeyboardButton("Каждые N дней", callback_data=mk_cb("r_per", v="every_n_days")),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=mk_cb("r_back")))
    send_safe("Шаг 4/8: Выбери периодичность", chat_id, reply_markup=kb)

def ask_weekdays(chat_id, preselected:str=""):
    days = [("пн","Пн"),("вт","Вт"),("ср","Ср"),("чт","Чт"),("пт","Пт"),("сб","Сб"),("вс","Вс")]
    sel = {x.strip() for x in (preselected or "").split(",") if x.strip()}
    kb = types.InlineKeyboardMarkup(row_width=4)
    for code, label in days:
        mark = "✅" if code in sel else "⬜"
        kb.add(types.InlineKeyboardButton(f"{mark} {label}", callback_data=mk_cb("r_wd", d=code)))
    kb.add(types.InlineKeyboardButton("Готово", callback_data=mk_cb("r_wd_done")))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=mk_cb("r_back")))
    send_safe("Отметь дни недели (переключатели):", chat_id, reply_markup=kb)

def ask_every_n(chat_id):
    send_safe("Введи N (через сколько дней повторять), например: 2", chat_id)

def ask_notify_time(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Без времени", callback_data=mk_cb("r_time_set", v="none")),
        types.InlineKeyboardButton("Указать время", callback_data=mk_cb("r_time_set", v="ask")),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=mk_cb("r_back")))
    send_safe("Шаг 5/8: Время уведомления", chat_id, reply_markup=kb)

def ask_before(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=5)
    for m in [0,5,10,30,60]:
        kb.add(types.InlineKeyboardButton(f"{m} мин", callback_data=mk_cb("r_before", v=m)))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=mk_cb("r_back")))
    send_safe("Шаг 6/8: Ранний пинг (за сколько минут)", chat_id, reply_markup=kb)

def ask_auto(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Автосоздание: ON", callback_data=mk_cb("r_auto_set", v=1)),
        types.InlineKeyboardButton("Автосоздание: OFF", callback_data=mk_cb("r_auto_set", v=0)),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=mk_cb("r_back")))
    send_safe("Шаг 7/8: Автосоздание задач", chat_id, reply_markup=kb)

def ask_title(chat_id):
    send_safe("Шаг 8/8: Введи заголовок (текст) правила", chat_id)

def r_wiz_summary(data:dict)->str:
    per = data.get("periodicity")
    if per=="weekdays":
        per_h = f"по {data.get('weekdays') or '—'}"
    elif per=="every_n_days":
        per_h = f"каждые {data.get('every_n') or '?'} дн."
    else:
        per_h = "ежедневно"
    tpe  = "📌" if data.get("type")=="todo" else "📦"
    nt   = tstr(data.get("notify_time")) if isinstance(data.get("notify_time"), dtime) else "—"
    nb   = data.get("notify_before_min", 0)
    auto = "on" if data.get("auto_create") else "off"
    ttl  = (data.get("title") or "").strip() or "(без названия)"
    return (f"{tpe} <b>{ttl}</b>\n"
            f"• Периодичность: {per_h}\n"
            f"• Время: {nt}  • Ранний пинг: {nb} мин\n"
            f"• Автосоздание: {auto}")

def r_wiz_save(uid, chat_id):
    sess = SessionLocal()
    try:
        data = RULE_WIZ.get(uid, {}).get("data", {})
        r = Rule(
            user_id=uid,
            direction_id=(data.get("direction_id") or None),
            type=data.get("type","todo"),
            supplier_id=(data.get("supplier_id") or None),
            periodicity=data.get("periodicity","daily"),
            every_n=(data.get("every_n") or None),
            weekdays=(data.get("weekdays") or None),
            notify_time=(data.get("notify_time") if isinstance(data.get("notify_time"), dtime) else None),
            notify_before_min=int(data.get("notify_before_min") or 0),
            auto_create=bool(data.get("auto_create", True)),
            title=data.get("title") or "",
            active=True
        )
        sess.add(r); sess.commit()
        send_safe("✅ Правило сохранено:\n\n"+rule_brief(r), chat_id)
    finally:
        sess.close()
    r_wiz_reset(uid)

@bot.callback_query_handler(func=lambda c: c.data and parse_cb(c.data) and parse_cb(c.data).get("a","").startswith("r_") or c.data in ("noop",))
def rules_callbacks(c):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    data = parse_cb(c.data) if c.data!="noop" else None
    if not data:
        bot.answer_callback_query(c.id); return
    a = data.get("a")

    # отмена/назад
    if a == "r_cancel":
        r_wiz_reset(uid)
        bot.answer_callback_query(c.id, "Отменено")
        send_safe("Ок, отменил создание правила.", chat_id); return
    if a == "r_back":
        step = r_wiz(uid).get("step")
        # примитивный шаг назад по дереву
        if step in ("type","dir"): ask_direction(chat_id); r_wiz(uid)["step"]="dir"
        elif step in ("supplier","priority"): ask_type(chat_id); r_wiz(uid)["step"]="type"
        elif step in ("per","weekdays","everyn"): ask_periodicity(chat_id); r_wiz(uid)["step"]="per"
        elif step in ("time","before"): ask_notify_time(chat_id); r_wiz(uid)["step"]="time"
        elif step in ("auto","title"): ask_before(chat_id); r_wiz(uid)["step"]="before"
        else:
            send_safe("Вернулся в начало.", chat_id); r_wiz_reset(uid)
        bot.answer_callback_query(c.id); return

    # старт создания
    if a == "r_add":
        RULE_WIZ[uid] = {"step":"dir","data":{}}
        ask_direction(chat_id)
        bot.answer_callback_query(c.id); return

    # выбор направления
    if a == "r_dir":
        r_wiz(uid)["data"]["direction_id"] = int(data.get("id") or 0) or None
        r_wiz(uid)["step"] = "type"
        ask_type(chat_id)
        bot.answer_callback_query(c.id); return

    # выбор типа
    if a == "r_type":
        r_wiz(uid)["data"]["type"] = data.get("v")
        if data.get("v") == "purchase":
            r_wiz(uid)["step"] = "supplier"
            ask_supplier(chat_id, r_wiz(uid)["data"].get("direction_id"))
        else:
            r_wiz(uid)["step"] = "per"
            ask_periodicity(chat_id)
        bot.answer_callback_query(c.id); return

    # выбор поставщика
    if a == "r_sup":
        r_wiz(uid)["data"]["supplier_id"] = int(data.get("id") or 0) or None
        r_wiz(uid)["step"] = "per"
        ask_periodicity(chat_id)
        bot.answer_callback_query(c.id); return

    # периодичность
    if a == "r_per":
        v = data.get("v")
        r_wiz(uid)["data"]["periodicity"] = v
        if v == "weekdays":
            r_wiz(uid)["data"]["weekdays"] = ""
            r_wiz(uid)["step"] = "weekdays"
            ask_weekdays(chat_id, "")
        elif v == "every_n_days":
            r_wiz(uid)["step"] = "everyn"
            ask_every_n(chat_id)
        else:
            r_wiz(uid)["step"] = "time"
            ask_notify_time(chat_id)
        bot.answer_callback_query(c.id); return

    # выбор дней недели (переключатели)
    if a == "r_wd":
        cur = r_wiz(uid)["data"].get("weekdays","")
        parts = [x.strip() for x in cur.split(",") if x.strip()]
        d = data.get("d")
        if d in parts:
            parts.remove(d)
        else:
            parts.append(d)
        r_wiz(uid)["data"]["weekdays"] = ",".join([p for p in parts if p])
        ask_weekdays(chat_id, r_wiz(uid)["data"]["weekdays"])
        bot.answer_callback_query(c.id); return

    if a == "r_wd_done":
        if not r_wiz(uid)["data"].get("weekdays"):
            bot.answer_callback_query(c.id, "Выбери хотя бы один день", show_alert=True); return
        r_wiz(uid)["step"] = "time"
        ask_notify_time(chat_id)
        bot.answer_callback_query(c.id); return

    # время уведомления
    if a == "r_time_set":
        if data.get("v") == "none":
            r_wiz(uid)["data"]["notify_time"] = None
            r_wiz(uid)["step"] = "before"
            ask_before(chat_id)
        else:
            r_wiz(uid)["step"] = "time_ask"
            send_safe("Введи время в формате ЧЧ:ММ", chat_id)
        bot.answer_callback_query(c.id); return

    # ранний пинг (минуты)
    if a == "r_before":
        r_wiz(uid)["data"]["notify_before_min"] = int(data.get("v") or 0)
        r_wiz(uid)["step"] = "auto"
        ask_auto(chat_id)
        bot.answer_callback_query(c.id); return

    # авто‑создание
    if a == "r_auto_set":
        r_wiz(uid)["data"]["auto_create"] = bool(int(data.get("v")))
        r_wiz(uid)["step"] = "title"
        ask_title(chat_id)
        bot.answer_callback_query(c.id); return

    # карточка/действия по правилу
    if a == "r_info":
        rid = int(data.get("id"))
        sess = SessionLocal()
        try:
            r = sess.query(Rule).filter(Rule.id==rid, Rule.user_id==uid).first()
            if not r:
                bot.answer_callback_query(c.id, "Правило не найдено", show_alert=True); return
            kb = types.InlineKeyboardMarkup()
            kb.row(
                types.InlineKeyboardButton("✏️ Заголовок", callback_data=mk_cb("r_edit", id=rid)),
                types.InlineKeyboardButton("🔔 Время", callback_data=mk_cb("r_time", id=rid)),
            )
            kb.row(
                types.InlineKeyboardButton("🔄 Автосоздание", callback_data=mk_cb("r_auto", id=rid)),
                types.InlineKeyboardButton("⏯ Активно/Выкл", callback_data=mk_cb("r_active", id=rid)),
            )
            kb.row(types.InlineKeyboardButton("🗑 Удалить", callback_data=mk_cb("r_del", id=rid)))
            send_safe(rule_brief(r), chat_id, reply_markup=kb)
        finally:
            sess.close()
        bot.answer_callback_query(c.id); return

    if a == "r_auto":
        rid = int(data.get("id"))
        sess = SessionLocal()
        try:
            r = sess.query(Rule).filter(Rule.id==rid, Rule.user_id==uid).first()
            if not r: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            r.auto_create = not r.auto_create; sess.commit()
            bot.answer_callback_query(c.id, f"Автосоздание: {'on' if r.auto_create else 'off'}")
        finally:
            sess.close()
        return

    if a == "r_active":
        rid = int(data.get("id"))
        sess = SessionLocal()
        try:
            r = sess.query(Rule).filter(Rule.id==rid, Rule.user_id==uid).first()
            if not r: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            r.active = not r.active; sess.commit()
            bot.answer_callback_query(c.id, f"{'Включено' if r.active else 'Выключено'}")
        finally:
            sess.close()
        return

    if a == "r_del":
        rid = int(data.get("id"))
        sess = SessionLocal()
        try:
            r = sess.query(Rule).filter(Rule.id==rid, Rule.user_id==uid).first()
            if not r: bot.answer_callback_query(c.id, "Не найдено", show_alert=True); return
            sess.delete(r); sess.commit()
            bot.answer_callback_query(c.id, "Удалено", show_alert=True)
        finally:
            sess.close()
        return

    if a == "r_edit":
        rid = int(data.get("id"))
        RULE_EDIT[uid] = {"field":"title", "rule_id":rid}
        bot.answer_callback_query(c.id)
        send_safe("Введи новый заголовок правила:", chat_id)
        return

    if a == "r_time":
        rid = int(data.get("id"))
        RULE_EDIT[uid] = {"field":"time", "rule_id":rid}
        bot.answer_callback_query(c.id)
        send_safe("Введи время ЧЧ:ММ или «none»", chat_id)
        return

# ---- обработка текстовых шагов мастера/редактирования ----
@bot.message_handler(func=lambda m: RULE_WIZ.get(m.chat.id, {}).get("step") in ("everyn","time_ask","title") or RULE_EDIT.get(m.chat.id))
def rules_text_steps(m):
    uid = m.chat.id
    sess = None
    # приоритет редактирования существующего правила
    if RULE_EDIT.get(uid):
        rid = RULE_EDIT[uid]["rule_id"]; field = RULE_EDIT[uid]["field"]
        sess = SessionLocal()
        try:
            r = sess.query(Rule).filter(Rule.id==rid, Rule.user_id==uid).first()
            if not r:
                RULE_EDIT.pop(uid, None)
                send_safe("Правило не найдено.", uid); return
            txt = (m.text or "").strip()
            if field == "title":
                r.title = txt
                sess.commit()
                send_safe("✅ Обновил заголовок.\n\n"+rule_brief(r), uid)
            elif field == "time":
                if txt.lower() == "none":
                    r.notify_time = None
                else:
                    try:
                        r.notify_time = parse_time(txt)
                    except Exception:
                        send_safe("Формат времени: ЧЧ:ММ или «none»", uid); return
                sess.commit()
                send_safe("✅ Обновил время.\n\n"+rule_brief(r), uid)
        finally:
            sess.close()
        RULE_EDIT.pop(uid, None)
        return

    # шаги мастера создания
    step = RULE_WIZ[uid]["step"]
    if step == "everyn":
        try:
            n = int((m.text or "").strip())
            if n <= 0: raise ValueError()
        except Exception:
            send_safe("Нужно целое число > 0. Введи N ещё раз:", uid); return
        RULE_WIZ[uid]["data"]["every_n"] = n
        RULE_WIZ[uid]["step"] = "time"
        ask_notify_time(uid)
        return

    if step == "time_ask":
        txt = (m.text or "").strip()
        try:
            RULE_WIZ[uid]["data"]["notify_time"] = parse_time(txt)
        except Exception:
            send_safe("Формат времени: ЧЧ:ММ. Попробуй ещё раз:", uid); return
        RULE_WIZ[uid]["step"] = "before"
        ask_before(uid)
        return

    if step == "title":
        RULE_WIZ[uid]["data"]["title"] = (m.text or "").strip()
        send_safe("Проверь сводку и сохраняю:\n\n"+r_wiz_summary(RULE_WIZ[uid]["data"]), uid)
        r_wiz_save(uid, uid)
        return

# --------- Правила (джоб и утилиты) ---------
def make_auto_key(user_id:int, dt:date, title:str, direction_id:Optional[int], supplier_id:Optional[int], task_type:str)->str:
    raw = f"{user_id}|{dt.isoformat()}|{title}|{direction_id or 0}|{supplier_id or 0}|{task_type}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def rule_hits_today(r: Rule, target: date) -> bool:
    if r.periodicity == "daily":
        return True
    if r.periodicity == "every_n_days":
        base = (r.created_at.date() if r.created_at else date(2025,1,1))
        delta = (target - base).days
        return (delta >= 0 and r.every_n and r.every_n > 0 and delta % r.every_n == 0)
    if r.periodicity == "weekdays":
        days = {"пн":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6}
        wd_list = [x.strip() for x in (r.weekdays or "").split(",") if x.strip()]
        wanted = {days.get(x) for x in wd_list if x in days}
        return target.weekday() in wanted
    return False

def rule_human(r: Rule) -> str:
    base = "📌" if r.type=="todo" else "📦"
    return f"{base} {r.title or '(без названия)'}"

def job_rules_tick():
    sess = SessionLocal()
    try:
        now = now_local()
        today = now.date()
        rules = sess.query(Rule).filter(Rule.active==True).all()
        for r in rules:
            # раннее уведомление
            if r.notify_time:
                nt = datetime.combine(today, r.notify_time)
                if nt.tzinfo is None:
                    nt = LOCAL_TZ.localize(nt)
                before = nt - timedelta(minutes=(r.notify_before_min or 0))
                if before <= now < nt:
                    try:
                        send_safe(f"⏰ Скоро по правилу: {rule_human(r)}", r.user_id)
                    except Exception as e:
                        log.warning("rule notify_before send error: %s", e)

            # автосоздание задач
            if rule_hits_today(r, today) and r.auto_create:
                title = r.title or ("Задача по правилу" if r.type=="todo" else "Заказ по правилу")
                ak = make_auto_key(r.user_id, today, title, r.direction_id, r.supplier_id, r.type)
                dup = sess.query(Task).filter(Task.user_id==r.user_id, Task.date==today, Task.auto_key==ak).first()
                if not dup:
                    t = Task(
                        user_id=r.user_id, date=today, text=title,
                        task_type=r.type, direction_id=r.direction_id,
                        supplier_id=r.supplier_id,
                        priority=("medium" if r.type=="todo" else "high"),
                        auto_key=ak, category="Личное", subcategory=""
                    )
                    sess.add(t)
        sess.commit()
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
                send_safe(text, u.id)
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
                send_safe(f"🔔 Напоминание: {t.text} (до {tstr(t.deadline)})", r.user_id)
            except Exception as e:
                log.error("reminder send error: %s", e)
            r.fired = True
        sess.commit()
    finally:
        sess.close()

def job_orchestrator_minutely():
    global LAST_TICK
    LAST_TICK = datetime.utcnow()
    job_check_reminders()
    job_rules_tick()

def scheduler_loop():
    schedule.clear()
    schedule.every().day.at("08:00").do(job_daily_digest)
    schedule.every(1).minutes.do(job_orchestrator_minutely)
    while True:
        schedule.run_pending()
        time.sleep(1)

# --------- /health ---------
@bot.message_handler(commands=["health"])
def cmd_health(m):
    if ADMIN_IDS and m.from_user and m.from_user.id not in ADMIN_IDS:
        return
    sess = SessionLocal()
    try:
        rc = sess.query(Rule).filter(Rule.active==True).count()
    finally:
        sess.close()
    lt = LAST_TICK.isoformat() if LAST_TICK else "—"
    send_safe(f"✅ OK\nLast tick: {lt}\nActive rules: {rc}\nTZ: {TZ_NAME}", m.chat.id)

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
            bot.infinity_polling(timeout=60, long_polling_timeout=50, skip_pending=True, allowed_updates=["message","callback_query"])
        except Exception as e:
            log.error("polling error: %s — retry in 3s", e)
            time.sleep(3)
