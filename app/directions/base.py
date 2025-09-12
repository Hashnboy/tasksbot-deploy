from __future__ import annotations

"""Базовые классы и интерфейсы для направлений."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class MenuSection:
    """Описание секции меню.

    text: отображаемый текст на кнопке
    callback: суффикс callback'а (dir:<key>:<callback>)
    """

    text: str
    callback: str


class Direction(ABC):
    """Базовый класс направления.

    Каждое направление должно иметь уникальный ключ ``key`` и отображаемое
    название на русском языке ``display_name_ru``. Реализации могут
    переопределять различные точки расширения: меню, шаблоны, политики и
    отчётные хуки.
    """

    #: машинный ключ (``tobacco``/``coffee``/``wb``/``personal``)
    key: str
    #: отображаемое название для UI
    display_name_ru: str

    @abstractmethod
    def get_menu_sections(self, user: Any) -> List[MenuSection]:
        """Вернуть список секций меню для пользователя."""

    def get_templates(self, point: Optional[Any] = None) -> List[Dict[str, Any]]:
        """Шаблоны задач для указанной точки.

        Возвращает список JSON‑совместимых структур. По умолчанию — пусто.
        """

        return []

    def get_policies_overrides(self) -> Dict[str, Any]:
        """Частичные переопределения policies.json."""

        return {}

    def get_report_hooks(self) -> Dict[str, Any]:
        """Хуки/строители для отчётов по направлению."""

        return {}
