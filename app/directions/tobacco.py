"""Направление "Табачка"."""
from .base import Direction, MenuSection


class TobaccoDirection(Direction):
    key = "tobacco"
    display_name_ru = "Табачка"

    def get_menu_sections(self, user):
        return [
            MenuSection(text="✅ Чек-ин", callback="checkin"),
            MenuSection(text="🗂 Задачи", callback="tasks"),
            MenuSection(text="📦 Приёмки", callback="receivings"),
            MenuSection(text="🔔 Напоминания", callback="reminders"),
            MenuSection(text="📊 Отчёты", callback="reports"),
            MenuSection(text="⚖️ Контроль", callback="control"),
        ]


direction = TobaccoDirection()
