"""Направление "Wildberries"."""
from .base import Direction, MenuSection


class WBDirection(Direction):
    key = "wb"
    display_name_ru = "Wildberries"

    def get_menu_sections(self, user):
        return [
            MenuSection(text="🗂 Контент-задачи", callback="content"),
            MenuSection(text="📦 Остатки/цены", callback="stock"),
            MenuSection(text="📈 Мониторинг", callback="monitoring"),
            MenuSection(text="📊 Отчёты", callback="reports"),
        ]


direction = WBDirection()
