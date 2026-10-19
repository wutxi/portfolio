"""Генерирует PDF-заглушку лид-магнита assets/checklist.pdf (нужен pymupdf и шрифт с кириллицей).

Запуск: pip install pymupdf && python make_checklist.py
Шрифт ищется в FONT_PATH, затем в стандартных путях Windows/macOS/Linux.
"""
import os
from pathlib import Path

import pymupdf as fitz

CANDIDATES = [
    os.getenv("FONT_PATH", ""),
    r"C:\Windows\Fonts\arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
TITLE = "Чек-лист: 12 проверок профиля перед запуском рекламы"
ITEMS = [
    "Имя профиля содержит то, что вы продаёте, а не только название бренда.",
    "В шапке за 3 секунды понятно: что, для кого, где и как заказать.",
    "Есть одна главная кнопка действия: записаться, написать, купить.",
    "Аватар читается в маленьком кружке: логотип или лицо крупно.",
    "В закреплённых постах — цены, отзывы клиентов и ответы на частые вопросы.",
    "Последние 9 публикаций не выглядят как разрозненный архив.",
    "Каждый пост заканчивается понятным следующим шагом для читателя.",
    "Ответ на сообщение приходит в течение рабочего часа, есть автоответ.",
    "Прайс или «цена от» указаны открыто, без «узнайте в директ».",
    "Ссылка в профиле ведёт на рабочую страницу, а не на ошибку 404.",
    "Вы знаете, какой пост приносит заявки, и можете повторить его формат.",
    "Посчитана цена заявки, на которую вы готовы выйти в рекламе.",
]


def main() -> None:
    font = next((p for p in CANDIDATES if p and Path(p).exists()), None)
    if not font:
        raise SystemExit("Не найден шрифт с кириллицей. Укажите путь в FONT_PATH.")
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="ru", fontfile=font)
    red = (0.85, 0.1, 0.12)
    page.draw_rect(fitz.Rect(0, 0, 595, 8), color=red, fill=red)
    page.insert_textbox(fitz.Rect(48, 48, 547, 130), TITLE, fontname="ru", fontsize=22, color=(0.1, 0.1, 0.1))
    page.insert_text((48, 150), "Лид-магнит из бота-воронки. Концепт для портфолио, бренд и данные вымышлены.",
                      fontname="ru", fontsize=10, color=(0.45, 0.45, 0.45))
    y = 185
    for i, item in enumerate(ITEMS, 1):
        page.draw_rect(fitz.Rect(48, y, 62, y + 14), color=red, width=1.2)
        page.insert_textbox(fitz.Rect(74, y - 1, 547, y + 40), f"{i}. {item}", fontname="ru", fontsize=12,
                            color=(0.12, 0.12, 0.12))
        y += 46
    page.insert_text((48, 800), "Отметьте пункты, которые уже сделаны. Меньше 8 галочек — рекламу запускать рано.",
                      fontname="ru", fontsize=10, color=red)
    out = Path(__file__).parent / "assets" / "checklist.pdf"
    out.parent.mkdir(exist_ok=True)
    doc.subset_fonts()
    doc.save(out, garbage=3, deflate=True)
    print("Сохранено:", out)


if __name__ == "__main__":
    main()
