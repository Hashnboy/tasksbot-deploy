"""Направление "Личное"."""
from .base import Direction, MenuSection


class PersonalDirection(Direction):
    key = "personal"
    display_name_ru = "Личное"

    def get_menu_sections(self, user):
        return [
            MenuSection(text="🗂 Мои задачи", callback="tasks"),
            MenuSection(text="🔔 Напоминания", callback="reminders"),
            MenuSection(text="📊 Личные отчёты", callback="reports"),
        ]


direction = PersonalDirection()
