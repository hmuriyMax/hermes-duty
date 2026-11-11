#!/bin/sh
# Фейковая сессия лида для теста настоящего спавна (`tests/test_lead_supervisor.py`).
#
# Зачем: `hermes chat` в тестах не поднимается (деньги и минуты), но проверять
# надо не решение сторожа, а сам заход — что ребёнок получил окружение, что поток
# лёг файлом, что `session_id` и расход взяты из `result`. Поэтому фейк говорит
# ровно то, что говорит настоящая сессия в `--format stream-json`: строку потока
# с окружением и строку `result`.
printf '{"type":"stream","hermes_home":"%s","duty_home":"%s"}\n' "$HERMES_HOME" "$HERMES_DUTY_HOME"
printf '{"type":"result","session_id":"sess-fake","tokens":{"total":7}}\n'
