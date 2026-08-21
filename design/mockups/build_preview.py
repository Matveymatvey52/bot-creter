#!/usr/bin/env python3
"""Пересобирает design/mockups/PREVIEW_real_app.html из живых стилей приложения.

Превью — это статический снимок экрана «Список» реального мини-аппа: разметка
берётся один раз (она повторяет то, что рендерит ListScreen.tsx), а CSS всегда
подтягивается из miniapp/src/index.css + components/ui.css, чтобы превью не
расходилось с приложением. Запускать после любой правки этих двух файлов.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
PREVIEW = ROOT / "design/mockups/PREVIEW_real_app.html"

# Стили самой рамки-превью (телефонная «коробка»), в приложении их нет.
SHELL = """
.pv-wrap{max-width:400px;margin:0 auto;border:1px solid var(--border-subtle);border-radius:24px;overflow:hidden;
 background:var(--bg);display:flex;flex-direction:column;min-height:820px}
.pv-wrap .screen{flex:1 1 auto}
body{padding:26px 14px}
"""

css = "\n".join(
    (ROOT / p).read_text()
    for p in ("miniapp/src/index.css", "miniapp/src/components/ui.css")
)
# @import в index.css указывает на ui.css — он уже подклеен ниже, убираем.
css = re.sub(r'^\s*@import[^;]+;\s*$', '', css, flags=re.M)

html = PREVIEW.read_text()
new = re.sub(
    r"(?s)<style>.*?</style>",
    lambda _: "<style>" + css + SHELL + "</style>",
    html,
    count=1,
)
PREVIEW.write_text(new)
print(f"ok: {PREVIEW.relative_to(ROOT)} — CSS {len(css)} байт")
