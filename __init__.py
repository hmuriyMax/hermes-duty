"""Hermes Duty — агентная половина плагина.

С 0.2.0 плагин отдаёт инструменты лиду (`docs/ENGINE-2.0.md`, раздел 4): тулсет
`duty` — `duty_plan`, `duty_start`, `duty_wait`, `duty_answer`, `duty_ask`,
`duty_ask_human`, `duty_cancel`, `duty_cancel_deal`, `duty_close`. Шедулером дела
становится лид, поэтому инструменты живут здесь, а обработчики — в
`dashboard/tools.py`: они работают с журналом дел и спекой, а не с карточками
kanban.

Профилю `lead` тулсет включается ключом `duty` в `platform_toolsets`: у роли
whitelist, и без этого ключа инструменты не поднимутся.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

__version__ = "0.2.0"

# Пакета у `dashboard/` нет (плагин грузит модули по пути), поэтому обработчики
# берём тем же способом и под стабильным именем: второй загрузки в процессе не
# будет.
TOOLS_MODULE_NAME = "hermes_duty_tools"

# Инструменты тулсета: имя, схема, обработчик, значок. Порядок — порядок турна
# лида (`docs/ENGINE-2.0.md`, разделы 5 и 6): план, этап, ожидание, ответ воркеру,
# вопрос профилю, вопрос человеку, отмена, закрытие.
_TOOLS = (
    ("duty_plan", "PLAN_SCHEMA", "handle_plan", "🗺"),
    ("duty_start", "START_SCHEMA", "handle_start", "▶"),
    ("duty_wait", "WAIT_SCHEMA", "handle_wait", "⏳"),
    ("duty_answer", "ANSWER_SCHEMA", "handle_answer", "💬"),
    ("duty_ask", "ASK_SCHEMA", "handle_ask", "🙋"),
    ("duty_ask_human", "ASK_HUMAN_SCHEMA", "handle_ask_human", "🧑"),
    ("duty_cancel", "CANCEL_SCHEMA", "handle_cancel", "⛔"),
    ("duty_cancel_deal", "CANCEL_DEAL_SCHEMA", "handle_cancel_deal", "🚫"),
    ("duty_close", "CLOSE_SCHEMA", "handle_close", "🏁"),
)


def tools_module() -> Any:
    """`dashboard/tools.py` — модуль обработчиков, один на процесс."""
    loaded = sys.modules.get(TOOLS_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "dashboard" / "tools.py"
    spec = importlib.util.spec_from_file_location(TOOLS_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"tools module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[TOOLS_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def register(ctx) -> None:
    """Точка входа плагина: девять инструментов лида в тулсете `duty`.

    `ctx.register_tool` — единственный путь: список `provides_tools` в
    `plugin.yaml` объявляет ровно то, что регистрируется здесь, и `hermes plugins
    validate` сверяет одно с другим.
    """
    module = tools_module()
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=module.TOOLSET,
            schema=getattr(module, schema),
            handler=getattr(module, handler),
            emoji=emoji,
        )


__all__ = ["register", "tools_module"]
