"""Направление "Кофейня"."""
from .base import Direction, MenuSection


class CoffeeDirection(Direction):
    key = "coffee"
    display_name_ru = "Кофейня"

    def get_menu_sections(self, user):
        return [
            MenuSection(text="✅ Чек-ин", callback="checkin"),
            MenuSection(text="🗂 Задачи", callback="tasks"),
            MenuSection(text="🍰 Поставки/списания", callback="supplies"),
            MenuSection(text="🔔 Напоминания", callback="reminders"),
            MenuSection(text="📊 Отчёты", callback="reports"),
            MenuSection(text="⚖️ Контроль", callback="control"),
        ]


direction = CoffeeDirection()
