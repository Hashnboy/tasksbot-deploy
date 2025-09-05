# -*- coding: utf-8 -*-
"""
OrgExt — расширение для TasksBot:
- Пользователи/роли и приглашения (бариста/старший бариста, продавец/старший продавец, админы по направлениям)
- Локации (ЦЕНТР/ПОЛЕТ/КЛИМОВО) с часами, гео и радиусом
- Чек-ин/чек-аут: геолокация + фото (анти-дубль), автоштраф за опоздание, рейтинг
- Отчетность: чек-листы по точкам/дням недели (кофейня) и по этапам (табачка), штрафы за пропуски
- Админ-кабинет: приглашения, роли/привязка, настройки автоштрафов/очков, статистика

Зависимости: pyTelegramBotAPI, SQLAlchemy
Хранение: свои таблицы с префиксом org_ (не конфликтуют с твоими)
"""

import os, uuid
from math import radians, sin, cos, asin, sqrt
from datetime import datetime, timedelta, time
from collections import defaultdict

from telebot import TeleBot
from telebot.types import (
    Message,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, ForeignKey, Boolean, Text, Numeric, Time, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# -----------------------
# ENV / конфиг
# -----------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///org_ext.db")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}  # можно пусто на MVP
GEODIST_RADIUS_DEFAULT_M = int(os.getenv("GEOFENCE_M", "150"))
ANTI_DUPLICATE_DAYS = int(os.getenv("PHOTO_DEDUP_DAYS", "14"))

# -----------------------
# БД
# -----------------------
Base = declarative_base()

class OrgUser(Base):
    __tablename__ = "org_users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, index=True, nullable=False)
    full_name = Column(String, default="")
    phone = Column(String, default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class OrgInvite(Base):
    __tablename__ = "org_invites"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    direction = Column(String, nullable=False)  # coffee|tobacco
    role = Column(String, nullable=False)       # barista|senior_barista|admin_coffee|seller|senior_seller|admin_tobacco
    location = Column(String, nullable=True)    # ЦЕНТР/ПОЛЕТ/КЛИМОВО или None (все)
    created_by_tg = Column(Integer, nullable=False)
    used_by_tg = Column(Integer, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class OrgUserRole(Base):
    __tablename__ = "org_user_roles"
    id = Column(Integer, primary_key=True)
    user_tg = Column(Integer, ForeignKey("org_users.tg_id"), index=True, nullable=False)
    direction = Column(String, nullable=False)         # coffee|tobacco
    role = Column(String, nullable=False)              # barista|senior_barista|admin_coffee|seller|senior_seller|admin_tobacco
    location = Column(String, nullable=True)           # точка или None
    created_at = Column(DateTime, default=datetime.utcnow)

class OrgLocation(Base):
    __tablename__ = "org_locations"
    id = Column(Integer, primary_key=True)
    title = Column(String, unique=True, nullable=False) # ЦЕНТР|ПОЛЕТ|КЛИМОВО
    direction = Column(String, nullable=False)          # coffee|tobacco
    address = Column(String, default="")
    lat = Column(Numeric(10,6), nullable=True)
    lon = Column(Numeric(10,6), nullable=True)
    open_time = Column(Time, nullable=False)
    close_time = Column(Time, nullable=False)
    geo_radius_m = Column(Integer, default=GEODIST_RADIUS_DEFAULT_M)
    created_at = Column(DateTime, default=datetime.utcnow)

class OrgSetting(Base):
    __tablename__ = "org_settings"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)

class OrgCheckin(Base):
    __tablename__ = "org_checkins"
    id = Column(Integer, primary_key=True)
    user_tg = Column(Integer, index=True, nullable=False)
    direction = Column(String, nullable=False)
    location = Column(String, nullable=False)
    stage = Column(String, nullable=False)  # checkin|checkout
    lat = Column(Numeric(10,6), nullable=True)
    lon = Column(Numeric(10,6), nullable=True)
    dist_m = Column(Integer, default=0)
    photo_file_id = Column(String, nullable=False)
    photo_unique_id = Column(String, nullable=False)
    at = Column(DateTime, default=datetime.utcnow)
    lateness_min = Column(Integer, default=0)
    is_on_time = Column(Boolean, default=False)

class OrgPenalty(Base):
    __tablename__ = "org_penalties"
    id = Column(Integer, primary_key=True)
    user_tg = Column(Integer, index=True, nullable=False)
    kind = Column(String, nullable=False)  # late|report_miss|abuse|task|other
    amount_rub = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class OrgRating(Base):
    __tablename__ = "org_rating"
    id = Column(Integer, primary_key=True)
    user_tg = Column(Integer, index=True, nullable=False)
    delta_points = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class OrgReportTemplate(Base):
    __tablename__ = "org_report_templates"
    id = Column(Integer, primary_key=True)
    direction = Column(String, nullable=False)   # coffee|tobacco
    location = Column(String, nullable=False)    # ЦЕНТР/ПОЛЕТ/КЛИМОВО
    title = Column(String, nullable=False)       # "Утро" / "До 12:00" / "Закрытие" / "Понедельник" и т.д.
    dow = Column(Integer, nullable=True)         # 0..6 для кофейни по дням, иначе NULL
    active = Column(Boolean, default=True)
    order_num = Column(Integer, default=0)

class OrgReportItem(Base):
    __tablename__ = "org_report_items"
    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("org_report_templates.id"), index=True, nullable=False)
    label = Column(String, nullable=False)
    kind = Column(String, default="photo")  # photo|text|number|checkbox
    required = Column(Boolean, default=True)
    penalty_rub = Column(Integer, default=300)
    order_num = Column(Integer, default=0)

class OrgReportSubmission(Base):
    __tablename__ = "org_report_submissions"
    id = Column(Integer, primary_key=True)
    user_tg = Column(Integer, index=True, nullable=False)
    direction = Column(String, nullable=False)
    location = Column(String, nullable=False)
    template_id = Column(Integer, nullable=False)
    submitted_at = Column(DateTime, default=datetime.utcnow)
    is_complete = Column(Boolean, default=False)

class OrgReportItemSubmission(Base):
    __tablename__ = "org_report_item_submissions"
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("org_report_submissions.id"), index=True, nullable=False)
    item_id = Column(Integer, ForeignKey("org_report_items.id"), index=True, nullable=False)
    ok = Column(Boolean, default=False)
    payload = Column(String, nullable=True)  # file_id/текст/число
    created_at = Column(DateTime, default=datetime.utcnow)

# -----------------------
# Утилиты
# -----------------------
def now_utc(): return datetime.utcnow()

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = radians(float(lat2) - float(lat1))
    dlon = radians(float(lon2) - float(lon1))
    a = sin(dlat/2)**2 + cos(radians(float(lat1))) * cos(radians(float(lat2))) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return int(R * c)

def is_admin(tg_id:int)->bool:
    return bool(ADMIN_IDS) and tg_id in ADMIN_IDS

# -----------------------
# Сессии шагов (in-memory)
# -----------------------
SESS = defaultdict(dict)

# -----------------------
# Дефолт-настройки + сиды
# -----------------------
DEFAULT_SETTINGS = {
    # штрафы
    "lateness_penalty_rub_per_min": "200",
    "report_item_penalty_default": "300",
    # рейтинг
    "rating_points_on_time": "10",
    "rating_points_per_minute_late": "-2",
    "rating_points_per_report_ok": "3",
    "rating_points_per_report_miss": "-5",
    # окна
    "checkin_early_allow_min": "60",
    "checkin_late_allow_min": "0",       # опоздание нельзя (0), но мы считаем и штрафуем
    "checkin_after_open_grace_min": "60" # окно приёма после открытия (для чек-ина)
}

TOBACCO_LOCATIONS = [
    # title, direction, address, open, close
    ("КЛИМОВО", "tobacco", "Климова 37 А", time(8,0),  time(22,0)),
    ("ПОЛЕТ",   "tobacco", "Дмитрия Михайлова", time(10,0), time(22,0)),
    ("ЦЕНТР",   "tobacco", "3-го Интернационала 68", time(10,0), time(23,0)),
]

COFFEE_LOCATIONS = [
    ("ЦЕНТР", "coffee", "3-го Интернационала 68", time(9,0),  time(23,0)),
    ("ПОЛЕТ", "coffee", "Дмитрия Михайлова 3",    time(8,0),  time(22,0)),
]

# Часть чек-листов (сжатая): табачка (утро/до12/перед закрытием/закрытие), кофейня — по дням + ежедневка
TOBACCO_TEMPLATES = {
    # location: [ (title, items[]) ]
    "ЦЕНТР": [
        ("Утро", [
            ("Фото себя на месте (чек-ин времени)", "photo", True, 300),
            ("Фото товара на полках БЕЗ ДЫРОК", "photo", True, 300),
        ]),
        ("До 12:00", [
            ("Фото ЧИСТОГО заднего и переднего двора (включая проход)", "photo", True, 300),
            ("Фото ЧИСТЫХ стекол", "photo", True, 300),
            ("Фото порядка за прилавком", "photo", True, 300),
        ]),
        ("16:00–17:00", [
            ("Фото ЧИСТОГО переднего двора", "photo", True, 300),
        ]),
        ("Перед закрытием", [
            ("Фото графика уборки туалета (актуальные отметки)", "photo", True, 300),
            ("Фото состояния туалета после уборки", "photo", True, 300),
        ]),
        ("Закрытие", [
            ("Фото чистых полов", "photo", True, 300),
            ("Фото закрытия смены (касса/сейф/печати)", "photo", True, 300),
            ("Нал по факту (сумма, ₽)", "number", True, 300),
        ]),
    ],
    "ПОЛЕТ": [
        ("Утро", [
            ("Фото себя на месте (чек-ин времени)", "photo", True, 300),
            ("Фото товара на полках БЕЗ ДЫРОК", "photo", True, 300),
            ("Фото холодильника с товаром БЕЗ ДЫРОК", "photo", True, 300),
            ("Включены тумблеры света + вывеска (подтвердить)", "checkbox", True, 300),
        ]),
        ("До 12:00", [
            ("Фото ЧИСТЫХ стекол", "photo", True, 300),
            ("Фото порядка за прилавком", "photo", True, 300),
            ("Фото порядка в chill-зоне", "photo", True, 300),
        ]),
        ("Перед закрытием", [
            ("Фото графика уборки туалета (актуальные отметки)", "photo", True, 300),
            ("Фото состояния туалета и расходников", "photo", True, 300),
        ]),
        ("Закрытие", [
            ("Фото чистых полов и ковра", "photo", True, 300),
            ("Фото закрытия смены (касса/сейф/печати)", "photo", True, 300),
            ("Нал по факту (сумма, ₽)", "number", True, 300),
        ]),
    ],
    "КЛИМОВО": [
        ("Утро", [
            ("Фото себя на месте (чек-ин времени)", "photo", True, 300),
            ("Фото товара на полках БЕЗ ДЫРОК", "photo", True, 300),
            ("Фото холодильника с товаром БЕЗ ДЫРОК", "photo", True, 300),
            ("Включена вывеска (подтвердить)", "checkbox", True, 300),
        ]),
        ("До 12:00", [
            ("Фото ЧИСТЫХ стекол", "photo", True, 300),
            ("Фото порядка на стеллаже", "photo", True, 300),
        ]),
        ("Перед закрытием", [
            ("Фото графика уборки туалета (актуальные отметки)", "photo", True, 300),
            ("Фото состояния туалета и расходников", "photo", True, 300),
        ]),
        ("Закрытие", [
            ("Фото чистых полов", "photo", True, 300),
            ("Фото закрытия смены (касса/сейф/печати)", "photo", True, 300),
            ("Нал по факту (сумма, ₽)", "number", True, 300),
        ]),
    ],
}

COFFEE_DOW_TEMPLATES = {
    # dow: [ (title, items[]) ]  — title дублируем в "Понедельник"/... для наглядности
    0: ("Понедельник", [
        ("Фото куба (снаружи и внутри)", "photo", True, 300),
        ("Фото чистки кофемолки", "photo", True, 300),
        ("Фото подоконников, двери и поверхностей (без пыли)", "photo", True, 300),
    ]),
    1: ("Вторник", [
        ("Фото витрины после мытья", "photo", True, 300),
        ("Фото подсобки после разбора (порядок/чистый пол/убрано загрязнение)", "photo", True, 300),
        ("Фото прикассовой зоны (конфеты/органайзер/терминал/сроки)", "photo", True, 300),
    ]),
    2: ("Среда", [
        ("Фото полок (с посыпками и сиропами)", "photo", True, 300),
        ("Фото подоконников, двери и поверхностей (без пыли)", "photo", True, 300),
    ]),
    3: ("Четверг", [
        ("Фото чистки ноктюба", "photo", True, 300),
        ("Фото куба (снаружи и внутри)", "photo", True, 300),
        ("Фото чистки кофемолки", "photo", True, 300),
    ]),
    4: ("Пятница", [
        ("Фото подоконников, двери и поверхностей (без пыли)", "photo", True, 300),
        ("Фото холодильника в баре и морозилок (порядок десертов/чистота)", "photo", True, 300),
    ]),
    5: ("Суббота", [
        ("Фото витрины после мытья", "photo", True, 300),
    ]),
    6: ("Воскресенье", [
        ("Фото швов на полу после протирки", "photo", True, 300),
        ("Фото прикассовой зоны (конфеты/органайзер/терминал/сроки)", "photo", True, 300),
    ]),
}
COFFEE_DAILY_TEMPLATE = ("Ежедневная уборка (после смены)", [
    ("Фото раковины в баре (чисто, без загрязнений)", "photo", True, 300),
    ("Фото полов в баре по периметру (чисто)", "photo", True, 300),
])

# -----------------------
# Клавиатуры
# -----------------------
def kb_main():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("☕ Кофейня", callback_data="org_dir:coffee"),
        InlineKeyboardButton("🚬 Табачка", callback_data="org_dir:tobacco"),
    )
    return kb

def kb_user_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👣 Начать смену", callback_data="org_checkin"),
        InlineKeyboardButton("🏁 Закончить смену", callback_data="org_checkout"),
        InlineKeyboardButton("🧾 Отчётность", callback_data="org_reports"),
        InlineKeyboardButton("⭐ Мой рейтинг/штрафы", callback_data="org_mystats"),
    )
    return kb

def kb_admin_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👥 Пригласить/Роли", callback_data="org_admin_invites"),
        InlineKeyboardButton("📍 Локации", callback_data="org_admin_locations"),
        InlineKeyboardButton("⚙️ Настройки штрафов/очков", callback_data="org_admin_settings"),
        InlineKeyboardButton("📊 Статистика по сотрудникам", callback_data="org_admin_stats"),
        InlineKeyboardButton("🧾 Шаблоны отчётов", callback_data="org_admin_templates"),
    )
    return kb

def kb_locations(session: Session, direction: str):
    kb = InlineKeyboardMarkup(row_width=2)
    locs = session.query(OrgLocation).filter_by(direction=direction).order_by(OrgLocation.title.asc()).all()
    for l in locs:
        kb.add(InlineKeyboardButton(l.title, callback_data=f"org_loc:{l.title}"))
    return kb

# -----------------------
# Класс расширения
# -----------------------
class OrgExt:
    def __init__(self, bot: TeleBot, engine=None, SessionLocal=None):
        self.bot = bot
        self.engine = engine or create_engine(DATABASE_URL, pool_pre_ping=True)
        self.SessionLocal = SessionLocal or sessionmaker(bind=self.engine, expire_on_commit=False)

    def init_db(self):
        Base.metadata.create_all(self.engine)
        with self.SessionLocal() as s:
            # settings
            for k, v in DEFAULT_SETTINGS.items():
                if not s.get(OrgSetting, k):
                    s.add(OrgSetting(key=k, value=v))
            # locations
            if s.query(OrgLocation).count() == 0:
                for (t,d,a,o,c) in COFFEE_LOCATIONS:
                    s.add(OrgLocation(title=t, direction=d, address=a, open_time=o, close_time=c, geo_radius_m=GEODIST_RADIUS_DEFAULT_M))
                for (t,d,a,o,c) in TOBACCO_LOCATIONS:
                    s.add(OrgLocation(title=t, direction=d, address=a, open_time=o, close_time=c, geo_radius_m=GEODIST_RADIUS_DEFAULT_M))
            s.commit()

            # seed templates if empty
            if s.query(OrgReportTemplate).count() == 0:
                # tobacco
                for loc, blocks in TOBACCO_TEMPLATES.items():
                    for (title, items) in blocks:
                        tmpl = OrgReportTemplate(direction="tobacco", location=loc, title=title, dow=None, active=True, order_num=0)
                        s.add(tmpl); s.flush()
                        for (label, kind, required, pen) in items:
                            s.add(OrgReportItem(template_id=tmpl.id, label=label, kind=kind, required=required, penalty_rub=pen, order_num=0))
                # coffee DOW
                for dow, (title, items) in COFFEE_DOW_TEMPLATES.items():
                    for loc in ("ЦЕНТР","ПОЛЕТ"):
                        tmpl = OrgReportTemplate(direction="coffee", location=loc, title=title, dow=dow, active=True, order_num=0)
                        s.add(tmpl); s.flush()
                        for (label, kind, required, pen) in items:
                            s.add(OrgReportItem(template_id=tmpl.id, label=label, kind=kind, required=required, penalty_rub=pen, order_num=0))
                # coffee daily
                (title, items) = COFFEE_DAILY_TEMPLATE
                for loc in ("ЦЕНТР","ПОЛЕТ"):
                    tmpl = OrgReportTemplate(direction="coffee", location=loc, title=title, dow=None, active=True, order_num=100)
                    s.add(tmpl); s.flush()
                    for (label, kind, required, pen) in items:
                        s.add(OrgReportItem(template_id=tmpl.id, label=label, kind=kind, required=required, penalty_rub=pen, order_num=0))
                s.commit()

    def register(self):
        bot = self.bot

        # ----------- Старт/меню ----------
        @bot.message_handler(commands=["start"])
        def start(m: Message):
            with self.SessionLocal() as s:
                self.ensure_user(s, m.from_user)
            bot.send_message(m.chat.id, "Выберите направление:", reply_markup=kb_main())

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_dir:"))
        def choose_dir(c):
            direction = c.data.split(":")[1]
            SESS[c.from_user.id]["direction"] = direction
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, f"Направление: {'Кофейня' if direction=='coffee' else 'Табачка'}", reply_markup=kb_user_menu())
            if is_admin(c.from_user.id):
                bot.send_message(c.message.chat.id, "Админ-панель:", reply_markup=kb_admin_menu())

        # ----------- Приглашения / роли (админ) ----------
        @bot.callback_query_handler(func=lambda c: c.data=="org_admin_invites")
        def admin_invites(c):
            if not is_admin(c.from_user.id): 
                bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton("➕ Сгенерировать приглашение", callback_data="org_inv_new"),
                InlineKeyboardButton("👤 Выдать роль", callback_data="org_role_grant"),
                InlineKeyboardButton("🔗 Привязка к локации", callback_data="org_role_bindloc"),
            )
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Приглашения/Роли:", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data=="org_inv_new")
        def inv_new(c):
            if not is_admin(c.from_user.id): 
                bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            SESS[c.from_user.id] = {"stage":"inv_dir"}
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("☕ Кофейня", callback_data="org_inv_dir:coffee"),
                   InlineKeyboardButton("🚬 Табачка", callback_data="org_inv_dir:tobacco"))
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Для какого направления?", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_inv_dir:"))
        def inv_dir(c):
            d = c.data.split(":")[1]
            SESS[c.from_user.id] = {"stage":"inv_role", "direction": d}
            kb = InlineKeyboardMarkup(row_width=2)
            if d=="coffee":
                kb.add(
                    InlineKeyboardButton("Бариста", callback_data="org_inv_role:barista"),
                    InlineKeyboardButton("Старший бариста", callback_data="org_inv_role:senior_barista"),
                    InlineKeyboardButton("Админ кофейни", callback_data="org_inv_role:admin_coffee"),
                )
            else:
                kb.add(
                    InlineKeyboardButton("Продавец", callback_data="org_inv_role:seller"),
                    InlineKeyboardButton("Старший продавец", callback_data="org_inv_role:senior_seller"),
                    InlineKeyboardButton("Админ табачки", callback_data="org_inv_role:admin_tobacco"),
                )
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Какая роль?", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_inv_role:"))
        def inv_role(c):
            role = c.data.split(":")[1]
            SESS[c.from_user.id]["role"] = role
            SESS[c.from_user.id]["stage"] = "inv_loc"
            with self.SessionLocal() as s:
                kb = kb_locations(s, "coffee" if "barista" in role or "admin_coffee"==role else "tobacco")
            kb.add(InlineKeyboardButton("Без привязки к точке", callback_data="org_inv_loc:*"))
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Привязать к точке?", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_inv_loc:"))
        def inv_loc(c):
            loc = c.data.split(":")[1]
            data = SESS[c.from_user.id]
            code = str(uuid.uuid4())[:8].upper()
            expires = now_utc() + timedelta(days=7)
            with self.SessionLocal() as s:
                inv = OrgInvite(
                    code=code, direction=data["direction"], role=data["role"],
                    location=None if loc=="*" else loc, created_by_tg=c.from_user.id,
                    expires_at=expires
                )
                s.add(inv); s.commit()
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, f"Код приглашения: `{code}`\nДействует до {expires:%d.%m %H:%M}\nПусть пользователь отправит команду: `/join {code}`", parse_mode="Markdown")
            SESS[c.from_user.id].clear()

        @bot.message_handler(commands=["join"])
        def cmd_join(m: Message):
            parts = (m.text or "").split()
            if len(parts) != 2:
                bot.reply_to(m, "Использование: /join КОД")
                return
            code = parts[1].strip().upper()
            with self.SessionLocal() as s:
                inv = s.query(OrgInvite).filter_by(code=code).first()
                if not inv or (inv.expires_at and inv.expires_at < now_utc()) or inv.used_by_tg:
                    bot.reply_to(m, "Приглашение недействительно.")
                    return
                self.ensure_user(s, m.from_user)
                inv.used_by_tg = m.from_user.id
                s.add(OrgUserRole(user_tg=m.from_user.id, direction=inv.direction, role=inv.role, location=inv.location))
                s.commit()
            bot.reply_to(m, f"Готово. Назначена роль {inv.role} ({'кофейня' if inv.direction=='coffee' else 'табачка'})" + (f", локация {inv.location}" if inv.location else ""))

        # ----------- Настройки (админ) ----------
        @bot.callback_query_handler(func=lambda c: c.data=="org_admin_settings")
        def admin_settings(c):
            if not is_admin(c.from_user.id): bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            with self.SessionLocal() as s:
                rows = s.query(OrgSetting).all()
            text = "Текущие настройки штрафов/очков:\n" + "\n".join([f"• {r.key} = {r.value}" for r in rows])
            kb = InlineKeyboardMarkup(row_width=1)
            for key in DEFAULT_SETTINGS.keys():
                kb.add(InlineKeyboardButton(f"✏ Изменить {key}", callback_data=f"org_set:{key}"))
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, text, reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_set:"))
        def set_key(c):
            if not is_admin(c.from_user.id): bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            key = c.data.split(":")[1]
            SESS[c.from_user.id] = {"stage":"org_set_val", "key": key}
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, f"Введи новое значение для `{key}`:", parse_mode="Markdown")

        @bot.message_handler(func=lambda m: SESS.get(m.from_user.id,{}).get("stage")=="org_set_val")
        def set_val(m: Message):
            key = SESS[m.from_user.id]["key"]
            val = m.text.strip()
            with self.SessionLocal() as s:
                row = s.get(OrgSetting, key)
                if not row: row = OrgSetting(key=key, value=val)
                else: row.value = val; row.updated_at = now_utc()
                s.add(row); s.commit()
            bot.reply_to(m, f"Сохранено: {key} = {val}")
            SESS[m.from_user.id].clear()

        # ----------- Локации (админ) ----------
        @bot.callback_query_handler(func=lambda c: c.data=="org_admin_locations")
        def admin_locations(c):
            if not is_admin(c.from_user.id): bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            with self.SessionLocal() as s:
                locs = s.query(OrgLocation).order_by(OrgLocation.direction, OrgLocation.title).all()
            text = "Локации:\n" + "\n".join([f"• [{l.direction}] {l.title} — {l.address} ({l.open_time.strftime('%H:%M')}-{l.close_time.strftime('%H:%M')})" for l in locs])
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, text)

        # ----------- ЧЕК-ИН/АУТ -----------
        @bot.callback_query_handler(func=lambda c: c.data in ("org_checkin","org_checkout"))
        def on_check(c):
            stage = "checkin" if c.data=="org_checkin" else "checkout"
            direction = SESS[c.from_user.id].get("direction") or "tobacco"
            SESS[c.from_user.id] = {"stage": f"geo_{stage}", "direction": direction}
            with self.SessionLocal() as s:
                kb = kb_locations(s, direction)
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Выбери точку:", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_loc:"))
        def on_loc(c):
            loc = c.data.split(":")[1]
            sess = SESS[c.from_user.id]; sess["location"] = loc
            bot.answer_callback_query(c.id)
            kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add(KeyboardButton("📍 Отправить геопозицию", request_location=True))
            bot.send_message(c.message.chat.id, "Отправьте геолокацию для проверки…", reply_markup=kb)

        @bot.message_handler(content_types=['location'])
        def on_geo(m: Message):
            sess = SESS.get(m.from_user.id, {})
            if not sess or not sess.get("stage","").startswith("geo_"):
                return
            stage = "checkin" if "checkin" in sess["stage"] else "checkout"
            with self.SessionLocal() as s:
                loc = s.query(OrgLocation).filter_by(title=sess["location"], direction=sess["direction"]).first()
                if not loc or not loc.lat or not loc.lon:
                    self.bot.reply_to(m, "Для точки не заданы координаты. Сообщите администратору.")
                    return
                dist = haversine_m(m.location.latitude, m.location.longitude, float(loc.lat), float(loc.lon))
                if dist > (loc.geo_radius_m or GEODIST_RADIUS_DEFAULT_M):
                    self.bot.reply_to(m, f"Вы вне радиуса точки (~{dist} м). Подойдите ближе к адресу: {loc.address}.")
                    return
                sess["stage"] = f"photo_{stage}"
                sess["dist_m"] = dist
                SESS[m.from_user.id] = sess
            self.bot.send_message(m.chat.id, "Пришлите фото (селфи/на месте).")

        @bot.message_handler(content_types=['photo'])
        def on_photo(m: Message):
            sess = SESS.get(m.from_user.id, {})
            if not sess or not sess.get("stage","").startswith("photo_"):
                return
            stage = "checkin" if "checkin" in sess["stage"] else "checkout"
            ph = m.photo[-1]
            with self.SessionLocal() as s:
                # анти-дубль фото за N дней
                since = now_utc() - timedelta(days=ANTI_DUPLICATE_DAYS)
                dup = s.query(OrgCheckin).filter(OrgCheckin.photo_unique_id==ph.file_unique_id, OrgCheckin.at>=since).first()
                if dup:
                    self.bot.reply_to(m, "Похоже, это фото уже использовалось ранее. Пришлите свежее.")
                    return
                # штраф/рейтинг при CHECKIN
                lateness_min = 0; is_on_time = False
                if stage=="checkin":
                    loc = s.query(OrgLocation).filter_by(title=sess["location"], direction=sess["direction"]).first()
                    open_dt = datetime.combine(datetime.now().date(), loc.open_time)
                    conf = self.get_settings(s)
                    late = max(0, int((now_utc() - open_dt).total_seconds() // 60))
                    grace = int(conf.get("checkin_after_open_grace_min", "60"))
                    if now_utc() > open_dt:
                        lateness_min = late
                        is_on_time = (late == 0)
                        if late > 0:
                            fine = late * int(conf.get("lateness_penalty_rub_per_min","200"))
                            s.add(OrgPenalty(user_tg=m.from_user.id, kind="late", amount_rub=fine, reason=f"Опоздание на {late} мин"))
                            pts = late * int(conf.get("rating_points_per_minute_late","-2"))
                            s.add(OrgRating(user_tg=m.from_user.id, delta_points=pts, reason=f"Опоздание {late} мин"))
                        else:
                            s.add(OrgRating(user_tg=m.from_user.id, delta_points=int(conf.get("rating_points_on_time","10")), reason="Пришёл вовремя"))
                # записываем чек
                s.add(OrgCheckin(
                    user_tg=m.from_user.id, direction=sess["direction"], location=sess["location"],
                    stage=stage, lat=m.location.latitude if hasattr(m, "location") else None,
                    lon=m.location.longitude if hasattr(m, "location") else None,
                    dist_m=sess.get("dist_m",0),
                    photo_file_id=ph.file_id, photo_unique_id=ph.file_unique_id,
                    at=now_utc(), lateness_min=lateness_min, is_on_time=is_on_time
                ))
                s.commit()
            self.bot.reply_to(m, ("Чек-ин выполнен ✅ Хорошей смены!" if stage=="checkin" else "Чек-аут выполнен ✅ Спасибо!"))
            SESS[m.from_user.id].clear()

        # ----------- ОТЧЕТНОСТЬ -----------
        @bot.callback_query_handler(func=lambda c: c.data=="org_reports")
        def reports_menu(c):
            direction = SESS[c.from_user.id].get("direction") or "tobacco"
            with self.SessionLocal() as s:
                kb = kb_locations(s, direction)
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Выбери точку для отчёта:", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_loc:"))
        def choose_report_loc(c):
            # этот хендлер уже есть выше для чек-ина; здесь просто игнор если не в режиме отчётов
            pass

        @bot.message_handler(commands=["report"])
        def cmd_report(m: Message):
            # быстрый вход: /report
            direction = SESS[m.from_user.id].get("direction") or "tobacco"
            with self.SessionLocal() as s:
                kb = kb_locations(s, direction)
            bot.send_message(m.chat.id, "Выбери точку для отчёта:", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_locrep:"))
        def open_report(c):
            # не используется; оставлено для расширения
            pass

        # Упрощённо: /rtoday — показать актуальные шаблоны по выбранному направлению/локации на сегодня
        @bot.message_handler(commands=["rtoday"])
        def cmd_rtoday(m: Message):
            direction = SESS[m.from_user.id].get("direction") or "tobacco"
            with self.SessionLocal() as s:
                kb = kb_locations(s, direction)
            bot.send_message(m.chat.id, "Выбери точку:", reply_markup=kb)
            SESS[m.from_user.id]["stage"] = "rtoday_loc"

        @bot.callback_query_handler(func=lambda c: SESS.get(c.from_user.id,{}).get("stage")=="rtoday_loc" and c.data.startswith("org_loc:"))
        def rtoday_loc(c):
            loc = c.data.split(":")[1]
            direction = SESS[c.from_user.id].get("direction") or "tobacco"
            with self.SessionLocal() as s:
                today = datetime.now().weekday()
                q = s.query(OrgReportTemplate).filter_by(direction=direction, location=loc, active=True)
                if direction=="coffee":
                    tmpls = q.filter((OrgReportTemplate.dow==today) | (OrgReportTemplate.dow==None)).order_by(OrgReportTemplate.order_num).all()
                else:
                    tmpls = q.order_by(OrgReportTemplate.order_num).all()
            SESS[c.from_user.id].clear()
            if not tmpls:
                bot.answer_callback_query(c.id)
                bot.send_message(c.message.chat.id, "На сегодня нет чек-листов.")
                return
            for t in tmpls:
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("Открыть чек-лист", callback_data=f"org_open_tmpl:{t.id}"))
                bot.send_message(c.message.chat.id, f"🧾 {t.location} — {t.title}", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_open_tmpl:"))
        def open_tmpl(c):
            tmpl_id = int(c.data.split(":")[1])
            with self.SessionLocal() as s:
                items = s.query(OrgReportItem).filter_by(template_id=tmpl_id).order_by(OrgReportItem.order_num, OrgReportItem.id).all()
            SESS[c.from_user.id] = {"stage":"rep_fill", "tmpl_id": tmpl_id, "answers": {}}
            kb = InlineKeyboardMarkup(row_width=1)
            for it in items:
                kb.add(InlineKeyboardButton(f"➕ {it.label}", callback_data=f"org_fill:{it.id}"))
            kb.add(InlineKeyboardButton("✔️ Отправить на проверку", callback_data="org_rep_submit"))
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Заполните пункты чек-листа:", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("org_fill:"))
        def fill_item(c):
            item_id = int(c.data.split(":")[1])
            SESS[c.from_user.id]["cur_item"] = item_id
            # узнаем тип
            with self.SessionLocal() as s:
                it = s.get(OrgReportItem, item_id)
            bot.answer_callback_query(c.id)
            if it.kind == "photo":
                bot.send_message(c.message.chat.id, f"Пришлите фото: {it.label}")
                SESS[c.from_user.id]["stage"]="rep_photo"
            elif it.kind == "text":
                bot.send_message(c.message.chat.id, f"Введите текст: {it.label}")
                SESS[c.from_user.id]["stage"]="rep_text"
            elif it.kind == "number":
                bot.send_message(c.message.chat.id, f"Введите число: {it.label}")
                SESS[c.from_user.id]["stage"]="rep_number"
            elif it.kind == "checkbox":
                bot.send_message(c.message.chat.id, f"Подтвердите выполнение: {it.label}\nОтветьте: да/нет")
                SESS[c.from_user.id]["stage"]="rep_checkbox"

        @bot.message_handler(content_types=['photo'])
        def rep_photo(m: Message):
            sess = SESS.get(m.from_user.id, {})
            if sess.get("stage") != "rep_photo": return
            ph = m.photo[-1]
            # анти-дубль фото
            with self.SessionLocal() as s:
                since = now_utc() - timedelta(days=ANTI_DUPLICATE_DAYS)
                dup = s.query(OrgReportSubmission).join(OrgReportItemSubmission, OrgReportItemSubmission.submission_id==OrgReportSubmission.id)\
                      .filter(OrgReportItemSubmission.payload==ph.file_id, OrgReportSubmission.submitted_at>=since).first()
                if dup:
                    self.bot.reply_to(m, "Это фото уже использовалось ранее. Пришлите новое.")
                    return
            item_id = sess["cur_item"]
            sess["answers"][item_id] = ("photo", ph.file_id)
            sess["stage"]="rep_fill"
            SESS[m.from_user.id]=sess
            self.bot.reply_to(m, "Фото сохранено ✅")

        @bot.message_handler(func=lambda m: SESS.get(m.from_user.id,{}).get("stage") in ("rep_text","rep_number","rep_checkbox"))
        def rep_text_num_chk(m: Message):
            sess = SESS.get(m.from_user.id, {})
            stage = sess["stage"]; item_id = sess["cur_item"]
            if stage == "rep_text":
                sess["answers"][item_id] = ("text", m.text.strip())
            elif stage == "rep_number":
                try:
                    val = float(m.text.replace(",","."))
                except: 
                    self.bot.reply_to(m, "Нужно число."); return
                sess["answers"][item_id] = ("number", str(val))
            elif stage == "rep_checkbox":
                ok = m.text.strip().lower() in ("да","yes","+","выполнено","true","1")
                sess["answers"][item_id] = ("checkbox", "ok" if ok else "no")
            sess["stage"]="rep_fill"
            SESS[m.from_user.id]=sess
            self.bot.reply_to(m, "Сохранено ✅")

        @bot.callback_query_handler(func=lambda c: c.data=="org_rep_submit")
        def rep_submit(c):
            sess = SESS.get(c.from_user.id, {})
            if sess.get("stage") != "rep_fill":
                bot.answer_callback_query(c.id, "Нет активного чек-листа.", show_alert=True); return
            tmpl_id = sess["tmpl_id"]; answers = sess.get("answers", {})
            with self.SessionLocal() as s:
                tmpl = s.get(OrgReportTemplate, tmpl_id)
                items = s.query(OrgReportItem).filter_by(template_id=tmpl_id).all()
                sub = OrgReportSubmission(user_tg=c.from_user.id, direction=tmpl.direction, location=tmpl.location, template_id=tmpl_id, submitted_at=now_utc())
                s.add(sub); s.flush()
                missed = []
                for it in items:
                    if it.id in answers:
                        kind, payload = answers[it.id]
                        ok = True if (kind!="checkbox" or payload=="ok") else True
                        s.add(OrgReportItemSubmission(submission_id=sub.id, item_id=it.id, ok=ok, payload=payload))
                        if ok and it.required and it.kind!="checkbox":
                            # плюс очки
                            pts = int(self.get_settings(s).get("rating_points_per_report_ok","3"))
                            s.add(OrgRating(user_tg=c.from_user.id, delta_points=pts, reason=f"Отчёт: {it.label}"))
                    else:
                        if it.required:
                            missed.append(it)
                # штрафы за пропуски
                for it in missed:
                    pen = it.penalty_rub or int(self.get_settings(s).get("report_item_penalty_default","300"))
                    s.add(OrgPenalty(user_tg=c.from_user.id, kind="report_miss", amount_rub=pen, reason=f"Не выполнен пункт: {it.label}"))
                    pts = int(self.get_settings(s).get("rating_points_per_report_miss","-5"))
                    s.add(OrgRating(user_tg=c.from_user.id, delta_points=pts, reason=f"Провал отчёта: {it.label}"))
                sub.is_complete = (len(missed)==0)
                s.commit()
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Отчёт отправлен на проверку старшему/админу. Спасибо!")
            SESS[c.from_user.id].clear()

        # ----------- МОЯ СТАТИСТИКА / АДМИН СТАТА -----------
        @bot.callback_query_handler(func=lambda c: c.data=="org_mystats")
        def my_stats(c):
            with self.SessionLocal() as s:
                month_start = datetime(now_utc().year, now_utc().month, 1)
                pts = s.query(func.coalesce(func.sum(OrgRating.delta_points),0)).filter(OrgRating.user_tg==c.from_user.id, OrgRating.created_at>=month_start).scalar()
                fines = s.query(func.coalesce(func.sum(OrgPenalty.amount_rub),0)).filter(OrgPenalty.user_tg==c.from_user.id, OrgPenalty.created_at>=month_start).scalar()
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, f"Ваш рейтинг за месяц: {pts} баллов\nШтрафы: {int(fines)} ₽")

        @bot.callback_query_handler(func=lambda c: c.data=="org_admin_stats")
        def admin_stats(c):
            if not is_admin(c.from_user.id): bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            SESS[c.from_user.id] = {"stage":"org_stats_user"}
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Пришлите @username или TG ID сотрудника:")

        @bot.message_handler(func=lambda m: SESS.get(m.from_user.id,{}).get("stage")=="org_stats_user")
        def admin_stats_user(m: Message):
            SESS[m.from_user.id].clear()
            try:
                user_tg = int(m.text.replace("@","").strip()) if m.text.strip().isdigit() else m.from_user.id
            except:
                user_tg = m.from_user.id
            with self.SessionLocal() as s:
                month_start = datetime(now_utc().year, now_utc().month, 1)
                pts = s.query(func.coalesce(func.sum(OrgRating.delta_points),0)).filter(OrgRating.user_tg==user_tg, OrgRating.created_at>=month_start).scalar()
                fines = s.query(func.coalesce(func.sum(OrgPenalty.amount_rub),0)).filter(OrgPenalty.user_tg==user_tg, OrgPenalty.created_at>=month_start).scalar()
            self.bot.reply_to(m, f"Статистика сотрудника {user_tg} за месяц:\nБаллы: {pts}\nШтрафы: {int(fines)} ₽")

    # --------- helpers ---------
    def ensure_user(self, s: Session, tguser):
        u = s.query(OrgUser).filter_by(tg_id=tguser.id).first()
        if not u:
            s.add(OrgUser(tg_id=tguser.id, full_name=(tguser.full_name or "").strip())); s.commit()

    def get_settings(self, s: Session) -> dict:
        rows = s.query(OrgSetting).all()
        return {r.key: r.value for r in rows}
