"""Синтаксис фронтовых артефактов: десктопный `plugin.js` и веб-`app.js`.

Зачем отдельный тест. Десктопная половина плагина грузится как **ES-модуль**:
загрузчик берёт `desktop/plugin.js`, подменяет импорты и делает `import(blob)`.
Обычный `node --check plugin.js` этот файл проверяет как CommonJS-скрипт и
пропускает ошибки, которые валит парсер модуля (например висячая запятая в
конце выражения перед `}`), — а плагин после такой ошибки не грузится вовсе:

    [plugins] runtime load failed (hermes-duty) SyntaxError: Unexpected token '}'

Так и случилось в 0.0.3 (`0c4d4e1`): десктопный плагин перестал добавляться в
приложение, потому что `desktop/plugin.js` разбирался как модуль с ошибкой, а
`node tests/model.test.mjs` проверяет только `model.js` и молчал. Поэтому здесь
файл копируется в `.mjs` и проверяется именно парсером модуля.

Node в тестах не обязателен: если его нет, тест помечается skip, а не врёт
зелёным.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_MODULES = (
    ROOT / "desktop" / "plugin.js",
    ROOT / "dashboard" / "web" / "app.js",
)

NODE = shutil.which("node")


def check_as_module(path: Path) -> subprocess.CompletedProcess[str]:
    """`node --check` по копии с расширением `.mjs` — парсер ES-модуля."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / f"{path.stem}.mjs"
        copy.write_bytes(path.read_bytes())
        return subprocess.run(
            [str(NODE), "--check", str(copy)],
            capture_output=True,
            text=True,
            timeout=60,
        )


@pytest.mark.parametrize("path", FRONTEND_MODULES, ids=lambda p: p.name)
def test_frontend_module_parses_as_esm(path: Path) -> None:
    assert path.is_file(), f"нет файла {path}"
    if NODE is None:
        pytest.skip("node не найден — синтаксис фронтовых модулей не проверить")
    result = check_as_module(path)
    assert result.returncode == 0, f"{path.name} не разбирается как ES-модуль:\n{result.stderr}"
