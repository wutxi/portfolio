"""Бот-воронка для эксперта (концепт для портфолио, бренд и данные вымышлены).

Приветствие → лид-магнит (PDF-чек-лист) → квиз из 4 вопросов → сегментация →
предложение подходящего продукта → заявка админу.
"""
import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ErrorEvent, FSInputFile, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import content as c

load_dotenv()
log = logging.getLogger("funnel-bot")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
CHECKLIST = Path(os.getenv("CHECKLIST_PATH", Path(__file__).parent / "assets" / "checklist.pdf"))


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            got_magnet INTEGER DEFAULT 0, segment TEXT, score INTEGER,
            answers TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, contact TEXT,
            segment TEXT, product TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)

    def touch(self, user) -> None:
        self.conn.execute("INSERT INTO users(user_id, username, first_name) VALUES (?,?,?) "
                          "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
                          (user.id, user.username, user.first_name))
        self.conn.commit()

    def magnet(self, user_id: int) -> None:
        self.conn.execute("UPDATE users SET got_magnet=1 WHERE user_id=?", (user_id,))
        self.conn.commit()

    def quiz_result(self, user_id: int, answers: list[int], score: int, segment: str) -> None:
        self.conn.execute("UPDATE users SET answers=?, score=?, segment=? WHERE user_id=?",
                          (json.dumps(answers), score, segment, user_id))
        self.conn.commit()

    def add_lead(self, user_id: int, name: str, contact: str, segment: str, product: str) -> int:
        cur = self.conn.execute("INSERT INTO leads(user_id, name, contact, segment, product) VALUES (?,?,?,?,?)",
                                (user_id, name, contact, segment, product))
        self.conn.commit()
        return cur.lastrowid

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "users": q("SELECT COUNT(*) FROM users"),
            "magnet": q("SELECT COUNT(*) FROM users WHERE got_magnet=1"),
            "quiz": q("SELECT COUNT(*) FROM users WHERE segment IS NOT NULL"),
            "leads": q("SELECT COUNT(*) FROM leads"),
            "by_segment": dict(self.conn.execute("SELECT segment, COUNT(*) FROM leads GROUP BY segment").fetchall()),
        }

    def close(self):
        self.conn.close()


class Act(CallbackData, prefix="act"):
    name: str


class Ans(CallbackData, prefix="ans"):
    q: int
    a: int


def kb(*buttons: tuple[str, str], width: int = 1):
    b = InlineKeyboardBuilder()
    for text, act in buttons:
        b.button(text=text, callback_data=Act(name=act))
    b.adjust(width)
    return b.as_markup()


def question_kb(qi: int):
    b = InlineKeyboardBuilder()
    for ai, (text, _pts) in enumerate(c.QUIZ[qi][1]):
        b.button(text=text, callback_data=Ans(q=qi, a=ai))
    b.adjust(1)
    return b.as_markup()


class Quiz(StatesGroup):
    answering = State()


class Lead(StatesGroup):
    name = State()
    contact = State()


def question_text(qi: int) -> str:
    return f"Вопрос {qi + 1} из {len(c.QUIZ)}\n\n<b>{c.QUIZ[qi][0]}</b>"


def build_router(store: Store) -> Router:
    r = Router()

    @r.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        store.touch(message.from_user)
        await message.answer(c.WELCOME.format(expert=c.EXPERT),
                             reply_markup=kb(("Получить чек-лист", "magnet"), ("Сразу к квизу", "quiz")))

    @r.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Отменено. Начать сначала: /start", reply_markup=ReplyKeyboardRemove())

    @r.callback_query(Act.filter(F.name == "magnet"))
    async def magnet(cb: CallbackQuery):
        store.touch(cb.from_user)
        if CHECKLIST.exists():
            await cb.message.answer_document(FSInputFile(CHECKLIST, filename="checklist-12-proverok.pdf"),
                                             caption="Ваш чек-лист. Отметьте, что уже сделано.")
        else:
            log.warning("Файл лид-магнита не найден: %s", CHECKLIST)
            await cb.message.answer("Файл чек-листа временно недоступен, пришлю его чуть позже.")
        store.magnet(cb.from_user.id)
        await cb.message.answer(
            "А теперь 4 коротких вопроса — и я подскажу, какой шаг даст вам больше всего заявок именно сейчас.",
            reply_markup=kb(("Пройти квиз, 1 минута", "quiz")))
        await cb.answer()

    @r.callback_query(Act.filter(F.name == "quiz"))
    async def quiz_start(cb: CallbackQuery, state: FSMContext):
        store.touch(cb.from_user)
        await state.set_state(Quiz.answering)
        await state.update_data(answers=[])
        await cb.message.answer(question_text(0), reply_markup=question_kb(0))
        await cb.answer()

    @r.message(Command("quiz"))
    async def quiz_cmd(message: Message, state: FSMContext):
        store.touch(message.from_user)
        await state.set_state(Quiz.answering)
        await state.update_data(answers=[])
        await message.answer(question_text(0), reply_markup=question_kb(0))

    @r.callback_query(Quiz.answering, Ans.filter())
    async def on_answer(cb: CallbackQuery, callback_data: Ans, state: FSMContext):
        answers: list[int] = (await state.get_data()).get("answers", [])
        if callback_data.q != len(answers) or not 0 <= callback_data.a < len(c.QUIZ[callback_data.q][1]):
            return await cb.answer("Этот вопрос уже пройден", show_alert=False)
        answers.append(callback_data.a)
        await state.update_data(answers=answers)
        chosen = c.QUIZ[callback_data.q][1][callback_data.a][0]
        await cb.message.edit_text(question_text(callback_data.q) + f"\n\nВаш ответ: {html.escape(chosen)}")
        if len(answers) < len(c.QUIZ):
            await cb.message.answer(question_text(len(answers)), reply_markup=question_kb(len(answers)))
            return await cb.answer()

        score = sum(c.QUIZ[i][1][a][1] for i, a in enumerate(answers))
        _min, seg_id, seg_name, advice, product, price, desc = c.segment_for(score)
        store.quiz_result(cb.from_user.id, answers, score, seg_id)
        await state.set_state(None)
        await state.update_data(segment=seg_id, product=product, answers=answers, score=score)
        await cb.message.answer(
            f"Ваш этап: <b>{seg_name}</b>\n\n{advice}\n\n"
            f"Что подойдёт сейчас: <b>{product}</b> — {price}\n{desc}",
            reply_markup=kb(("Хочу, оставить заявку", "lead"), ("Подробнее", "details"),
                            ("Не сейчас", "later"), width=1))
        await cb.answer()

    @r.callback_query(Act.filter(F.name == "details"))
    async def details(cb: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if "segment" not in data:
            return await cb.answer("Сначала пройдите квиз", show_alert=True)
        await cb.message.answer(
            "Как проходит работа:\n"
            "1. Короткий созвон на 15 минут: уточняем задачу и сроки.\n"
            "2. Доступ к материалам или первая встреча в течение 3 дней.\n"
            "3. Оплата после созвона, можно частями.\n\n"
            "Остались вопросы — напишите их в заявке, ответим лично.",
            reply_markup=kb(("Оставить заявку", "lead")))
        await cb.answer()

    @r.callback_query(Act.filter(F.name == "later"))
    async def later(cb: CallbackQuery):
        await cb.message.answer("Хорошо. Чек-лист останется в этом чате, а пройти квиз заново можно командой /quiz.")
        await cb.answer()

    @r.callback_query(Act.filter(F.name == "lead"))
    async def lead_start(cb: CallbackQuery, state: FSMContext):
        if "segment" not in await state.get_data():
            return await cb.answer("Сначала пройдите квиз: /quiz", show_alert=True)
        await state.set_state(Lead.name)
        await cb.message.answer("Как к вам обращаться?")
        await cb.answer()

    @r.message(Lead.name, F.text)
    async def lead_name(message: Message, state: FSMContext):
        name = message.text.strip()[:60]
        if len(name) < 2 or name.startswith("/"):
            return await message.answer("Напишите, пожалуйста, имя. Отменить: /cancel")
        await state.update_data(name=name)
        await state.set_state(Lead.contact)
        await message.answer(
            "Оставьте телефон или ник в Telegram, куда удобно написать:",
            reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отправить номер", request_contact=True)]],
                                             resize_keyboard=True, one_time_keyboard=True))

    @r.message(Lead.contact, F.contact | F.text)
    async def lead_contact(message: Message, state: FSMContext, bot: Bot):
        raw = (message.contact.phone_number if message.contact else message.text or "").strip()
        if message.contact or len(re.sub(r"\D", "", raw)) >= 10:
            contact = "+" + re.sub(r"\D", "", raw)
        elif re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", raw):
            contact = "@" + raw.lstrip("@")
        else:
            return await message.answer("Нужен телефон (10+ цифр) или ник вида @username. Отменить: /cancel")
        data = await state.get_data()
        await state.clear()
        lead_id = store.add_lead(message.from_user.id, data["name"], contact, data["segment"], data["product"])
        log.info("Заявка #%s, сегмент %s", lead_id, data["segment"])
        await message.answer(f"Спасибо, {html.escape(data['name'])}! Заявка принята, напишем в течение рабочего дня.",
                             reply_markup=ReplyKeyboardRemove())
        answers = "\n".join(f"• {q[0]} — {q[1][a][0]}" for q, a in zip(c.QUIZ, data.get("answers", [])))
        text = (f"Новая заявка #{lead_id}\n"
                f"Имя: {html.escape(data['name'])}\nКонтакт: {html.escape(contact)}\n"
                f"Telegram: @{message.from_user.username or '—'} (id {message.from_user.id})\n"
                f"Сегмент: {data['segment']} ({data.get('score')} б.)\nПродукт: {data['product']}\n\nОтветы:\n{answers}")
        if ADMIN_ID:
            try:
                await bot.send_message(ADMIN_ID, text)
            except TelegramAPIError as e:
                log.error("Не удалось отправить заявку админу: %s", e)
        else:
            log.warning("ADMIN_ID не задан, заявка сохранена только в БД")

    @r.message(Command("stats"), F.from_user.id == ADMIN_ID)
    async def stats(message: Message):
        s = store.stats()
        seg = ", ".join(f"{k}: {v}" for k, v in s["by_segment"].items()) or "—"
        await message.answer(f"Пользователей: {s['users']}\nЗабрали чек-лист: {s['magnet']}\n"
                             f"Прошли квиз: {s['quiz']}\nЗаявок: {s['leads']} ({seg})")

    @r.message(Quiz.answering)
    async def quiz_text(message: Message):
        await message.answer("Выберите ответ кнопкой под вопросом. Начать заново: /quiz")

    @r.message()
    async def fallback(message: Message):
        await message.answer("Выберите действие:", reply_markup=kb(("Получить чек-лист", "magnet"), ("Пройти квиз", "quiz")))

    return r


def build_dispatcher(store: Store) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(store))

    @dp.errors()
    async def on_error(event: ErrorEvent):
        log.exception("Ошибка: %s", event.exception, exc_info=event.exception)
        upd = event.update
        target = upd.message or (upd.callback_query.message if upd.callback_query else None)
        if target:
            try:
                await target.answer("Что-то пошло не так. Начните заново: /start")
            except TelegramAPIError:
                pass
        return True

    return dp


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather.")
    store = Store(os.getenv("DB_PATH", "funnel.db"))
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await build_dispatcher(store).start_polling(bot)
    finally:
        store.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
