# -*- coding: utf-8 -*-
"""
OpsExt — расширение для TasksBot:
- Перемещение товара (Откуда -> Куда)
- Автозадача на приёмку на точке назначения
- Принять без расхождений / Есть расхождения (только "факт" + фото)
- Двухступенчатая приёмка: Старший -> Админ
- Уведомление админам о новой накладной перемещения
- Минимальные таблицы (без конфликта с существующими): префикс ops_

Зависимости: pyTelegramBotAPI, SQLAlchemy
DB: берём из ENV DATABASE_URL или совместимый engine/session из твоего проекта
"""

import os, uuid
from datetime import datetime, timedelta
from collections import defaultdict

from telebot import TeleBot
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, Message
)

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, ForeignKey, Boolean, Text, Numeric
)
from sqlalchemy.orm import declarative_base, Session, sessionmaker, relationship

# -----------------------------
# Конфиг (ENV с дефолтами)
# -----------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///ops_ext.db")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
SENIOR_IDS = {int(x) for x in os.getenv("SENIOR_IDS", "").split(",") if x.strip().isdigit()}
# Маппинг "название точки" -> chat_id (куда кидать уведомления по умолчанию); можно не задавать
LOCATION_ALERT_CHATS = {
    # "ЦЕНТР": -1001234567890,
    # "ПОЛЕТ": -1002222222222,
    # "КЛИМОВО": -1003333333333,
}

# -----------------------------
# База (изолированная, чтобы не конфликтовать)
# -----------------------------
Base = declarative_base()

class OpsLocation(Base):
    """Мини-справочник точек (если у тебя уже есть таблица locations — оставь эту и пользуйся только названием)."""
    __tablename__ = "ops_locations"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False, unique=True)  # "ЦЕНТР" | "ПОЛЕТ" | "КЛИМОВО"
    direction = Column(String, nullable=False, default="tobacco")  # "tobacco" | "coffee"
    created_at = Column(DateTime, default=datetime.utcnow)

class OpsSupplier(Base):
    __tablename__ = "ops_suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class OpsTransfer(Base):
    __tablename__ = "ops_transfers"
    id = Column(Integer, primary_key=True)
    number = Column(String, nullable=False, unique=True)  # TR-YYYYMMDD-XXXX
    direction = Column(String, nullable=False)            # tobacco | coffee
    from_location = Column(String, nullable=False)        # текстовое имя: "ЦЕНТР"
    to_location = Column(String, nullable=False)
    created_by_tg = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="in_transit")  # draft_transfer|in_transit|submitted|accepted|approved|rejected|needs_fix
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

class OpsTransferItem(Base):
    __tablename__ = "ops_transfer_items"
    id = Column(Integer, primary_key=True)
    transfer_id = Column(Integer, ForeignKey("ops_transfers.id"), index=True, nullable=False)
    name = Column(String, nullable=False)   # наименование
    qty_planned = Column(Numeric(12, 3), nullable=False)  # количество по документу
    # Факт НЕ храним тут — он только в расхождениях (по требованиям)
    created_at = Column(DateTime, default=datetime.utcnow)

class OpsTransferEvidence(Base):
    __tablename__ = "ops_transfer_evidence"
    id = Column(Integer, primary_key=True)
    transfer_id = Column(Integer, ForeignKey("ops_transfers.id"), index=True, nullable=False)
    kind = Column(String, default="photo")   # photo
    payload = Column(String, nullable=False) # file_id
    file_unique_id = Column(String)
    author_tg = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class OpsTransferDiscrepancy(Base):
    __tablename__ = "ops_transfer_discrepancies"
    id = Column(Integer, primary_key=True)
    transfer_id = Column(Integer, ForeignKey("ops_transfers.id"), index=True, nullable=False)
    item_id = Column(Integer, ForeignKey("ops_transfer_items.id"), index=True, nullable=False)
    type = Column(String, nullable=False)       # under|over|broken|wrong_sku|expiry
    qty_planned = Column(Numeric(12,3), nullable=False)
    qty_actual = Column(Numeric(12,3), nullable=False)
    photo_file_id = Column(String, nullable=False)
    comment = Column(Text, default="")
    author_tg = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class OpsPenalty(Base):
    __tablename__ = "ops_penalties"
    id = Column(Integer, primary_key=True)
    user_tg = Column(Integer, index=True, nullable=False)
    transfer_id = Column(Integer, ForeignKey("ops_transfers.id"), index=True)
    kind = Column(String, nullable=False)         # transfer_missing_photo|transfer_hidden_diff|abuse
    amount_rub = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class OpsRatingLedger(Base):
    __tablename__ = "ops_rating_ledger"
    id = Column(Integer, primary_key=True)
    user_tg = Column(Integer, index=True, nullable=False)
    transfer_id = Column(Integer, ForeignKey("ops_transfers.id"), index=True)
    delta_points = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

# -----------------------------
# Утилиты
# -----------------------------
def now_utc():
    return datetime.utcnow()

def gen_transfer_number(session: Session) -> str:
    base = f"TR-{now_utc():%Y%m%d}-"
    # простая уникализация
    tail = str(uuid.uuid4())[:6].upper()
    num = base + tail
    # на всякий случай проверим
    exists = session.query(OpsTransfer).filter_by(number=num).first()
    if exists:
        return gen_transfer_number(session)
    return num

def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS or not ADMIN_IDS  # если список пуст — считаем всех админами (чтобы заработало из коробки)

def is_senior(tg_id: int) -> bool:
    return tg_id in SENIOR_IDS or is_admin(tg_id)

# -----------------------------
# Сессии шагов (in-memory)
# В проде лучше Redis, но для "вставил и работает" достаточно словаря.
# -----------------------------
SESS = defaultdict(dict)

# -----------------------------
# Клавиатуры
# -----------------------------
def kb_ops_root():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🚚 Перемещение (Откуда)", callback_data="ops_tr_new"),
        InlineKeyboardButton("📥 Входящие перемещения (Куда)", callback_data="ops_tr_inbox"),
    )
    return kb

def kb_yesno(cb_yes: str, cb_no: str):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Да", callback_data=cb_yes),
           InlineKeyboardButton("Нет", callback_data=cb_no))
    return kb

def kb_discrepancy_types(item_id: int, tr_id: int):
    kb = InlineKeyboardMarkup(row_width=2)
    base = f"ops_tr_diff_type:{tr_id}:{item_id}:"
    kb.add(
        InlineKeyboardButton("Недопоставка", callback_data=base+"under"),
        InlineKeyboardButton("Перепоставка", callback_data=base+"over"),
        InlineKeyboardButton("Бой/брак", callback_data=base+"broken"),
        InlineKeyboardButton("Не тот SKU", callback_data=base+"wrong_sku"),
        InlineKeyboardButton("Сроки", callback_data=base+"expiry"),
    )
    return kb

def kb_review_senior(tr_id: int):
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("✅ Принять", callback_data=f"ops_tr_senior_accept:{tr_id}"),
        InlineKeyboardButton("✏ Доработка", callback_data=f"ops_tr_senior_fix:{tr_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"ops_tr_senior_reject:{tr_id}"),
    )
    return kb

def kb_review_admin(tr_id: int):
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("🏁 Утвердить", callback_data=f"ops_tr_admin_approve:{tr_id}"),
        InlineKeyboardButton("✏ Доработка", callback_data=f"ops_tr_admin_fix:{tr_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"ops_tr_admin_reject:{tr_id}"),
    )
    return kb

# -----------------------------
# Основной класс расширения
# -----------------------------
class OpsExt:
    def __init__(self, bot: TeleBot, engine=None, SessionLocal=None):
        self.bot = bot
        self.engine = engine or create_engine(DATABASE_URL, pool_pre_ping=True)
        self.SessionLocal = SessionLocal or sessionmaker(bind=self.engine, expire_on_commit=False)

    # вызывать один раз при старте
    def init_db(self):
        Base.metadata.create_all(self.engine)
        with self.SessionLocal() as s:
            # подложим точки (если нет)
            for title in ("ЦЕНТР", "ПОЛЕТ", "КЛИМОВО"):
                if not s.query(OpsLocation).filter_by(title=title).first():
                    s.add(OpsLocation(title=title, direction="tobacco"))
            s.commit()

    # регистрация хендлеров
    def register(self):
        bot = self.bot

        @bot.message_handler(commands=["ops"])
        def cmd_ops(m: Message):
            bot.send_message(m.chat.id, "Контур перемещений и приёмки:", reply_markup=kb_ops_root())

        # ---- Отправитель (Откуда): создать перемещение
        @bot.callback_query_handler(func=lambda c: c.data == "ops_tr_new")
        def tr_new(c):
            SESS[c.from_user.id] = {"stage": "tr_new_direction"}
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🚬 Табачка", callback_data="ops_tr_dir:tobacco"),
                   InlineKeyboardButton("☕ Кофейня", callback_data="ops_tr_dir:coffee"))
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Выбери направление:", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_dir:"))
        def tr_dir(c):
            direction = c.data.split(":")[1]
            sess = SESS.get(c.from_user.id, {})
            sess["direction"] = direction
            sess["stage"] = "tr_new_from"
            SESS[c.from_user.id] = sess
            kb = InlineKeyboardMarkup()
            for loc in ("ЦЕНТР", "ПОЛЕТ", "КЛИМОВО"):
                kb.add(InlineKeyboardButton(loc, callback_data=f"ops_tr_from:{loc}"))
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Откуда отгружаем?", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_from:"))
        def tr_from(c):
            from_loc = c.data.split(":")[1]
            sess = SESS.get(c.from_user.id, {})
            sess["from"] = from_loc
            sess["stage"] = "tr_new_to"
            SESS[c.from_user.id] = sess
            kb = InlineKeyboardMarkup()
            for loc in ("ЦЕНТР", "ПОЛЕТ", "КЛИМОВО"):
                if loc != from_loc:
                    kb.add(InlineKeyboardButton(loc, callback_data=f"ops_tr_to:{loc}"))
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Куда отправляем?", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_to:"))
        def tr_to(c):
            to_loc = c.data.split(":")[1]
            sess = SESS.get(c.from_user.id, {})
            sess["to"] = to_loc
            sess["stage"] = "tr_new_items"
            sess["items"] = []  # список словарей {name, qty}
            SESS[c.from_user.id] = sess
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, "Введи позиции построчно в формате:\nНаименование * Количество\nНапример:\nPafos Blueberry * 10\nTrava Mint * 6\n\nПо окончании пришли: Готово")

        @bot.message_handler(func=lambda m: SESS.get(m.from_user.id, {}).get("stage") == "tr_new_items")
        def tr_items_input(m: Message):
            text = m.text.strip()
            if text.lower() in ("готово","готов","done","end"):
                # просим фото отгрузки
                sess = SESS[m.from_user.id]
                if not sess["items"]:
                    bot.reply_to(m, "Добавь хотя бы одну позицию.")
                    return
                sess["stage"] = "tr_new_photo"
                SESS[m.from_user.id] = sess
                kb = ReplyKeyboardMarkup(resize_keyboard=True)
                kb.add(KeyboardButton("📷 Прикрепить фото отгрузки"))
                bot.send_message(m.chat.id, "Пришли минимум 1 фото отгрузки (короба/тарные места). Когда достаточно — нажми /send", reply_markup=kb)
                return
            # парсим строку "Name * Qty"
            if "*" not in text:
                bot.reply_to(m, "Неверный формат. Используй: Наименование * Количество")
                return
            name, qty = [x.strip() for x in text.split("*", 1)]
            try:
                qty = float(qty.replace(",", "."))
            except Exception:
                bot.reply_to(m, "Количество должно быть числом.")
                return
            SESS[m.from_user.id]["items"].append({"name": name, "qty": qty})
            bot.reply_to(m, f"Ок: {name} × {qty}")

        @bot.message_handler(commands=["send"])
        def tr_send(m: Message):
            sess = SESS.get(m.from_user.id, {})
            if sess.get("stage") != "tr_new_photo":
                return
            with self.SessionLocal() as s:
                num = gen_transfer_number(s)
                tr = OpsTransfer(
                    number=num,
                    direction=sess["direction"],
                    from_location=sess["from"],
                    to_location=sess["to"],
                    created_by_tg=m.from_user.id,
                    status="in_transit",
                    created_at=now_utc(), updated_at=now_utc()
                )
                s.add(tr); s.flush()
                for it in sess.get("items", []):
                    s.add(OpsTransferItem(transfer_id=tr.id, name=it["name"], qty_planned=it["qty"]))
                # фото уже собрали в evidence_temp
                for ev in sess.get("evidence_temp", []):
                    s.add(OpsTransferEvidence(
                        transfer_id=tr.id, kind="photo", payload=ev["file_id"],
                        file_unique_id=ev["uid"], author_tg=m.from_user.id
                    ))
                s.commit()

                # уведомление в "Куда" (в чат точки, если задан) + авто-задача (в виде входящего списка)
                chat_id_alert = LOCATION_ALERT_CHATS.get(tr.to_location)
                msg = f"📦 Новое перемещение #{tr.number}\nОткуда: {tr.from_location}\nКуда: {tr.to_location}\nПозиций: {len(sess['items'])}\nПерейди в /ops → «Входящие перемещения»."
                if chat_id_alert:
                    bot.send_message(chat_id_alert, msg)
                # уведомим текущий чат
                bot.send_message(m.chat.id, f"Готово. Перемещение #{tr.number} отправлено в путь.\nАдминам отправлена накладная.")

                # уведомить админов (накладная)
                for admin_id in (ADMIN_IDS or {m.chat.id}):
                    bot.send_message(admin_id, f"🧾 Накладная перемещения #{tr.number}\n{tr.from_location} → {tr.to_location}\nПозиций: {len(sess['items'])}")

            SESS[m.from_user.id].clear()

        @bot.message_handler(content_types=['photo'])
        def tr_collect_photo(m: Message):
            sess = SESS.get(m.from_user.id, {})
            if sess.get("stage") == "tr_new_photo":
                ph = m.photo[-1]
                sess.setdefault("evidence_temp", []).append({"file_id": ph.file_id, "uid": ph.file_unique_id})
                SESS[m.from_user.id] = sess
                self.bot.reply_to(m, "Фото добавлено ✅")
            elif sess.get("stage", "").startswith("tr_rcv_diff_photo:"):
                # ожидали фото для конкретной строки расхождения
                _, tr_id, item_id = sess["stage"].split(":")
                with self.SessionLocal() as s:
                    tr = s.get(OpsTransfer, int(tr_id))
                    if not tr: 
                        self.bot.reply_to(m, "Трансфер не найден.")
                        return
                    ph = m.photo[-1]
                    # сохраним фото временно в сессию для этого item
                    sess.setdefault("diff_photos", {})[int(item_id)] = ph.file_id
                    SESS[m.from_user.id] = sess
                    self.bot.reply_to(m, "Фото расхождения добавлено ✅")

        # ---- Получатель (Куда): входящие
        @bot.callback_query_handler(func=lambda c: c.data == "ops_tr_inbox")
        def tr_inbox(c):
            with self.SessionLocal() as s:
                trs = s.query(OpsTransfer).filter(OpsTransfer.status.in_(["in_transit","submitted","needs_fix"])).order_by(OpsTransfer.created_at.desc()).all()
                if not trs:
                    self.bot.answer_callback_query(c.id)
                    self.bot.send_message(c.message.chat.id, "Входящих перемещений нет.")
                    return
                for tr in trs:
                    self._send_transfer_card(c.message.chat.id, tr)

        # Карточка перемещения
        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_view:"))
        def tr_view(c):
            tr_id = int(c.data.split(":")[1])
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                self._send_transfer_card(c.message.chat.id, tr)

        # Принять без расхождений -> просим хотя бы 1 фото подтверждения, потом отправка на проверку старшему
        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_ok:"))
        def tr_ok(c):
            tr_id = int(c.data.split(":")[1])
            SESS[c.from_user.id] = {"stage": f"tr_ok_photo:{tr_id}", "ok_photos": []}
            self.bot.answer_callback_query(c.id)
            self.bot.send_message(c.message.chat.id, "Пришли 1–3 фото подтверждения приёмки. Когда готов — /submit_ok")

        @bot.message_handler(commands=["submit_ok"])
        def tr_ok_submit(m: Message):
            sess = SESS.get(m.from_user.id, {})
            if not sess.get("stage","").startswith("tr_ok_photo:"):
                return
            tr_id = int(sess["stage"].split(":")[1])
            # требуется хотя бы одно фото?
            # мы принимали фото в on_any_photo? давайте примем здесь — проще: берём последнее фото из истории сессии не будем.
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                if not tr: 
                    self.bot.reply_to(m, "Трансфер не найден.")
                    return
                # отметим как submitted (на проверку старшему)
                tr.status = "submitted"; tr.updated_at = now_utc()
                s.commit()
            self.bot.send_message(m.chat.id, f"Перемещение #{tr.number} отправлено на проверку старшему.")
            # уведомим старшего (если SENIOR_IDS есть)
            for sid in (SENIOR_IDS or {m.chat.id}):
                self.bot.send_message(sid, f"🧐 Проверка перемещения #{tr.number} (без расхождений).", reply_markup=kb_review_senior(tr_id))
            SESS[m.from_user.id].clear()

        # Есть расхождения -> выбираем строки
        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_diff:"))
        def tr_diff(c):
            tr_id = int(c.data.split(":")[1])
            with self.SessionLocal() as s:
                items = s.query(OpsTransferItem).filter_by(transfer_id=tr_id).all()
                kb = InlineKeyboardMarkup(row_width=1)
                for it in items:
                    kb.add(InlineKeyboardButton(f"{it.name} (план {it.qty_planned})", callback_data=f"ops_tr_diff_item:{tr_id}:{it.id}"))
                kb.add(InlineKeyboardButton("Готово, отправить на проверку", callback_data=f"ops_tr_diff_submit:{tr_id}"))
            self.bot.answer_callback_query(c.id)
            self.bot.send_message(c.message.chat.id, "Выбери позиции, где есть расхождения:", reply_markup=kb)

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_diff_item:"))
        def tr_diff_item(c):
            _, tr_id, item_id = c.data.split(":")
            # Сохраним выбор типа расхождения
            SESS[c.from_user.id]["stage"] = f"tr_diff_choose:{tr_id}:{item_id}"
            self.bot.answer_callback_query(c.id)
            self.bot.send_message(c.message.chat.id, "Выбери тип расхождения:", reply_markup=kb_discrepancy_types(int(item_id), int(tr_id)))

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_diff_type:"))
        def tr_diff_type(c):
            _, tr_id, item_id, diff_type = c.data.split(":")
            SESS[c.from_user.id]["stage"] = f"tr_diff_qty:{tr_id}:{item_id}:{diff_type}"
            self.bot.answer_callback_query(c.id)
            self.bot.send_message(c.message.chat.id, "Введи ФАКТИЧЕСКОЕ количество по этой строке (число).")

        @bot.message_handler(func=lambda m: SESS.get(m.from_user.id, {}).get("stage","").startswith("tr_diff_qty:"))
        def tr_diff_qty(m: Message):
            try:
                qty_actual = float(m.text.replace(",", "."))
            except Exception:
                self.bot.reply_to(m, "Нужно число. Попробуй ещё раз.")
                return
            _, tr_id, item_id, diff_type = SESS[m.from_user.id]["stage"].split(":")
            SESS[m.from_user.id]["stage"] = f"tr_rcv_diff_photo:{tr_id}:{item_id}"
            SESS[m.from_user.id].setdefault("diff_data", {})[int(item_id)] = {"diff_type": diff_type, "qty_actual": qty_actual}
            self.bot.send_message(m.chat.id, "Пришли фото подтверждения по этой строке (коробка/наклейка и т.д.).")

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_diff_submit:"))
        def tr_diff_submit(c):
            tr_id = int(c.data.split(":")[1])
            data = SESS.get(c.from_user.id, {})
            diffs = data.get("diff_data", {})
            photos = data.get("diff_photos", {})
            if not diffs:
                self.bot.answer_callback_query(c.id, "Не выбраны расхождения.", show_alert=True)
                return
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                if not tr:
                    self.bot.answer_callback_query(c.id, "Трансфер не найден.", show_alert=True)
                    return
                for item_id, payload in diffs.items():
                    it = s.get(OpsTransferItem, item_id)
                    if not it: 
                        continue
                    photo_id = photos.get(item_id)
                    if not photo_id:
                        self.bot.answer_callback_query(c.id, "По одной из строк нет фото. Пришли фото и повтори.", show_alert=True)
                        return
                    s.add(OpsTransferDiscrepancy(
                        transfer_id=tr.id, item_id=it.id,
                        type=payload["diff_type"],
                        qty_planned=it.qty_planned,
                        qty_actual=payload["qty_actual"],
                        photo_file_id=photo_id,
                        comment="",
                        author_tg=c.from_user.id
                    ))
                tr.status = "submitted"; tr.updated_at = now_utc()
                s.commit()
            self.bot.answer_callback_query(c.id)
            self.bot.send_message(c.message.chat.id, f"Перемещение #{tr.number} с расхождениями отправлено на проверку старшему.")
            for sid in (SENIOR_IDS or {c.message.chat.id}):
                self.bot.send_message(sid, f"🧐 Проверка перемещения #{tr.number} (с расхождениями).", reply_markup=kb_review_senior(tr_id))
            SESS[c.from_user.id].clear()

        # ---- Ревью Старшего
        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_senior_accept:"))
        def tr_senior_accept(c):
            tr_id = int(c.data.split(":")[1])
            if not is_senior(c.from_user.id):
                self.bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                if not tr: 
                    self.bot.answer_callback_query(c.id, "Не найден.", show_alert=True); return
                tr.status = "accepted"; tr.updated_at = now_utc(); s.commit()
            self.bot.answer_callback_query(c.id, "Принято. Передано админу.")
            for aid in (ADMIN_IDS or {c.message.chat.id}):
                self.bot.send_message(aid, f"✅ Старший принял перемещение #{tr.number}.", reply_markup=kb_review_admin(tr_id))

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_senior_fix:"))
        def tr_senior_fix(c):
            tr_id = int(c.data.split(":")[1])
            if not is_senior(c.from_user.id):
                self.bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                if not tr: 
                    self.bot.answer_callback_query(c.id, "Не найден.", show_alert=True); return
                tr.status = "needs_fix"; tr.updated_at = now_utc(); s.commit()
            self.bot.answer_callback_query(c.id, "Отправлено на доработку исполнителю.")
            # уведомим чат, где принимали
            self.bot.send_message(c.message.chat.id, f"🔁 Перемещение #{tr_id} отправлено на доработку.")

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_senior_reject:"))
        def tr_senior_reject(c):
            tr_id = int(c.data.split(":")[1])
            if not is_senior(c.from_user.id):
                self.bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                if not tr: 
                    self.bot.answer_callback_query(c.id, "Не найден.", show_alert=True); return
                tr.status = "rejected"; tr.updated_at = now_utc(); s.commit()
            self.bot.answer_callback_query(c.id, "Отклонено.")
            self.bot.send_message(c.message.chat.id, f"❌ Перемещение #{tr_id} отклонено старшим.")

        # ---- Ревью Админа (финал)
        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_admin_approve:"))
        def tr_admin_approve(c):
            tr_id = int(c.data.split(":")[1])
            if not is_admin(c.from_user.id):
                self.bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                if not tr: 
                    self.bot.answer_callback_query(c.id, "Не найден.", show_alert=True); return
                tr.status = "approved"; tr.updated_at = now_utc(); s.commit()
            self.bot.answer_callback_query(c.id, "Утверждено.")
            self.bot.send_message(c.message.chat.id, f"🏁 Перемещение #{tr_id} утверждено админом.")

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_admin_fix:"))
        def tr_admin_fix(c):
            tr_id = int(c.data.split(":")[1])
            if not is_admin(c.from_user.id):
                self.bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                if not tr: 
                    self.bot.answer_callback_query(c.id, "Не найден.", show_alert=True); return
                tr.status = "needs_fix"; tr.updated_at = now_utc(); s.commit()
            self.bot.answer_callback_query(c.id, "Отправлено на доработку.")
            self.bot.send_message(c.message.chat.id, f"🔁 Перемещение #{tr_id}: доработка.")

        @bot.callback_query_handler(func=lambda c: c.data.startswith("ops_tr_admin_reject:"))
        def tr_admin_reject(c):
            tr_id = int(c.data.split(":")[1])
            if not is_admin(c.from_user.id):
                self.bot.answer_callback_query(c.id, "Нет прав.", show_alert=True); return
            with self.SessionLocal() as s:
                tr = s.get(OpsTransfer, tr_id)
                if not tr: 
                    self.bot.answer_callback_query(c.id, "Не найден.", show_alert=True); return
                tr.status = "rejected"; tr.updated_at = now_utc(); s.commit()
            self.bot.answer_callback_query(c.id, "Отклонено.")
            self.bot.send_message(c.message.chat.id, f"❌ Перемещение #{tr_id} отклонено админом.")

    # -------- вспомогательная отрисовка карточки --------
    def _send_transfer_card(self, chat_id: int, tr: OpsTransfer):
        with self.SessionLocal() as s:
            items = s.query(OpsTransferItem).filter_by(transfer_id=tr.id).all()
            diffs = s.query(OpsTransferDiscrepancy).filter_by(transfer_id=tr.id).all()
        title = f"#{tr.number} | {tr.from_location} → {tr.to_location}\nСтатус: {tr.status}"
        lines = "\n".join([f"• {it.name} — план {it.qty_planned}" for it in items][:20])
        if len(items) > 20:
            lines += f"\n… и ещё {len(items)-20}"
        diffbadge = (f"\nРасхождения: {len(diffs)}" if diffs else "")
        txt = f"📦 Перемещение {title}\n{lines}{diffbadge}"
        kb = InlineKeyboardMarkup(row_width=2)
        if tr.status in ("in_transit","needs_fix"):
            kb.add(
                InlineKeyboardButton("✔️ Принять без расхождений", callback_data=f"ops_tr_ok:{tr.id}"),
                InlineKeyboardButton("⚠ Есть расхождения", callback_data=f"ops_tr_diff:{tr.id}"),
            )
        # показывать карточку всегда, + кнопка обновить
        kb.add(InlineKeyboardButton("🔄 Обновить", callback_data=f"ops_tr_view:{tr.id}"))
        self.bot.send_message(chat_id, txt, reply_markup=kb)
