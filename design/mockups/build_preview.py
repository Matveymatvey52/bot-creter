#!/usr/bin/env python3
"""Пересобирает design/mockups/PREVIEW_real_*.html из живых стилей приложения.

Превью — это статический снимок экрана «Список» реального мини-аппа: разметка
берётся один раз (она повторяет то, что рендерит ListScreen.tsx), а CSS всегда
подтягивается из miniapp/src/index.css + components/ui.css, чтобы превью не
расходилось с приложением. Запускать после любой правки этих двух файлов.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
PREVIEWS = sorted((ROOT / "design/mockups").glob("PREVIEW_real_*.html"))

# Стили самой рамки-превью (телефонная «коробка»), в приложении их нет.
SHELL = """
/* Рамка-телефон повторяет .device из эталона-мокапа один в один (390x844,
   скругление 40px, та же обводка и тень) — иначе превью и мокап нельзя
   сравнивать рядом: разная ширина корпуса меняет и ширину карточек. */
.pv-wrap{width:390px;max-width:100%;min-height:844px;margin:0 auto;
 border-radius:40px;overflow:hidden;background:var(--bg);
 border:1px solid rgba(255,255,255,0.055);
 box-shadow:0 40px 90px rgba(0,0,0,0.65);
 display:flex;flex-direction:column}
.pv-wrap .screen{flex:1 1 auto}
body{padding:26px 14px}

/* Внутри рамки — всегда мобильная раскладка. Медиазапрос в index.css смотрит
   на ширину ОКНА, а не контейнера, поэтому в широком браузере он включал
   десктопную сетку внутри узкого корпуса: карточка получала колонку 340px и
   прижималась влево, оставляя половину рамки пустой. Телефон так себя не
   ведёт — гасим сетку явно. */
@media (min-width:1024px){
 /* align-items:start из десктопных правил в flex-колонке ужимает блок по
    содержимому — из-за него спец-лист был уже остальных блоков и жался
    влево. Растягиваем обратно. */
 .pv-wrap .screen{display:flex;flex-direction:column;gap:16px;align-items:stretch;
  max-width:none;padding:16px}
 .pv-wrap .sol-list{display:flex;flex-direction:column;gap:8px}
}
"""
css = "\n".join(
    (ROOT / p).read_text()
    for p in ("miniapp/src/index.css", "miniapp/src/components/ui.css")
)
# @import в index.css указывает на ui.css — он уже подклеен ниже, убираем.
css = re.sub(r'^\s*@import[^;]+;\s*$', '', css, flags=re.M)

for preview in PREVIEWS:
    new = re.sub(
        r"(?s)<style>.*?</style>",
        lambda _: "<style>" + css + SHELL + "</style>",
        preview.read_text(),
        count=1,
    )
    preview.write_text(new)
    print(f"ok: {preview.relative_to(ROOT)}")
print(f"CSS {len(css)} байт из index.css + ui.css")
