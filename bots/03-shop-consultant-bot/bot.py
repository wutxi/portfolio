"""Бот-консультант чайной лавки «Лист и пар» (концепт для портфолио, бренд и данные вымышлены).

Каталог кнопками, FAQ, подбор товара по 3 вопросам, корзина, оформление заявки админу.
Опционально — «умный ответ» через Claude API (если задан ANTHROPIC_API_KEY).
"""
import asyncio
import html
import logging
import os
import re
import sqlite3
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ErrorEvent, KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import catalog as cat
from smart import SmartAdvisor

load_dotenv()
log = logging.getLogger("shop-bot")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
FREE_DELIVERY_FROM = 3000
DELIVERY = {"pickup": ("Самовывоз из лавки", 0), "courier": ("Курьер по городу", 300), "pvz": ("Пункт выдачи", 250)}


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS cart (user_id INTEGER, product_id TEXT, qty INTEGER,
                                         PRIMARY KEY (user_id, product_id));
        CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT,
            phone TEXT, delivery TEXT, address TEXT, items TEXT, total INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)

    def add(self, user_id: int, pid: str, delta: int = 1) -> int:
        row = self.conn.execute("SELECT qty FROM cart WHERE user_id=? AND product_id=?", (user_id, pid)).fetchone()
        qty = max(0, min(20, (row["qty"] if row else 0) + delta))
        if qty:
            self.conn.execute("INSERT INTO cart VALUES (?,?,?) ON CONFLICT(user_id, product_id) DO UPDATE SET qty=?",
                              (user_id, pid, qty, qty))
        else:
            self.conn.execute("DELETE FROM cart WHERE user_id=? AND product_id=?", (user_id, pid))
        self.conn.commit()
        return qty

    def cart(self, user_id: int) -> list[tuple[cat.Product, int]]:
        rows = self.conn.execute("SELECT product_id, qty FROM cart WHERE user_id=? ORDER BY rowid", (user_id,)).fetchall()
        return [(cat.BY_ID[r["product_id"]], r["qty"]) for r in rows if r["product_id"] in cat.BY_ID]

    def clear(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
        self.conn.commit()

    def save_order(self, user_id, name, phone, delivery, address, items, total) -> int:
        cur = self.conn.execute(
            "INSERT INTO orders(user_id, name, phone, delivery, address, items, total) VALUES (?,?,?,?,?,?,?)",
            (user_id, name, phone, delivery, address, items, total))
        self.conn.commit()
        return cur.lastrowid

    def close(self):
        self.conn.close()


class Nav(CallbackData, prefix="nav"):
    to: str


class Cat(CallbackData, prefix="cat"):
    id: str


class Item(CallbackData, prefix="it"):
    id: str


class CartOp(CallbackData, prefix="co"):
    id: str
    d: int


class Faq(CallbackData, prefix="faq"):
    id: str


class Pick(CallbackData, prefix="pk"):
    q: int
    a: int


class Deliv(CallbackData, prefix="dl"):
    id: str


class Confirm(CallbackData, prefix="ok"):
    yes: int


class Checkout(StatesGroup):
    name = State()
    phone = State()
    delivery = State()
    address = State()
    confirm = State()


class Ask(StatesGroup):
    waiting = State()


def rub(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


def menu_kb(smart_on: bool):
    b = InlineKeyboardBuilder()
    b.button(text="Каталог", callback_data=Nav(to="cats"))
    b.button(text="Подобрать чай", callback_data=Nav(to="pick"))
    b.button(text="Корзина", callback_data=Nav(to="cart"))
    b.button(text="Вопросы", callback_data=Nav(to="faq"))
    if smart_on:
        b.button(text="Спросить консультанта", callback_data=Nav(to="ask"))
    b.adjust(2, 2, 1)
    return b.as_markup()


def cart_summary(items) -> tuple[str, int]:
    total = sum(p.price * q for p, q in items)
    lines = [f"• {p.name}, {p.pack} × {q} = {rub(p.price * q)}" for p, q in items]
    return "\n".join(lines), total


def build_router(store: Store, smart: SmartAdvisor) -> Router:
    r = Router()

    async def show_menu(message: Message, text: str = "Чем помочь?"):
        await message.answer(text, reply_markup=menu_kb(smart.enabled))

    @r.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await show_menu(message,
                        f"Здравствуйте! Это бот чайной лавки «{cat.SHOP}».\n"
                        "Покажу каталог, помогу подобрать чай за 3 вопроса и соберу заказ.\n"
                        + ("Можно просто написать вопрос своими словами — отвечу.\n" if smart.enabled else "")
                        + "\n<i>Концепт для портфолио, бренд и данные вымышлены.</i>")

    @r.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
        await show_menu(message)

    @r.message(Command("cart"))
    async def cart_cmd(message: Message):
        await send_cart(message, message.from_user.id)

    @r.callback_query(Nav.filter(F.to == "menu"))
    async def nav_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await show_menu(cb.message)
        await cb.answer()

    @r.callback_query(Nav.filter(F.to == "cats"))
    async def cats(cb: CallbackQuery):
        b = InlineKeyboardBuilder()
        for cid, name in cat.CATEGORIES.items():
            b.button(text=name, callback_data=Cat(id=cid))
        b.button(text="← Меню", callback_data=Nav(to="menu"))
        b.adjust(2)
        await cb.message.answer("Выберите раздел:", reply_markup=b.as_markup())
        await cb.answer()

    @r.callback_query(Cat.filter())
    async def category(cb: CallbackQuery, callback_data: Cat):
        b = InlineKeyboardBuilder()
        for p in cat.PRODUCTS:
            if p.cat == callback_data.id:
                b.button(text=f"{p.name} · {rub(p.price)}", callback_data=Item(id=p.id))
        b.button(text="← Разделы", callback_data=Nav(to="cats"))
        b.adjust(1)
        await cb.message.answer(f"<b>{cat.CATEGORIES.get(callback_data.id, 'Раздел')}</b>", reply_markup=b.as_markup())
        await cb.answer()

    def item_kb(p: cat.Product):
        b = InlineKeyboardBuilder()
        b.button(text="В корзину", callback_data=CartOp(id=p.id, d=1))
        b.button(text="Корзина", callback_data=Nav(to="cart"))
        b.button(text="← Назад", callback_data=Cat(id=p.cat))
        b.adjust(1, 2)
        return b.as_markup()

    @r.callback_query(Item.filter())
    async def item(cb: CallbackQuery, callback_data: Item):
        p = cat.BY_ID.get(callback_data.id)
        if not p:
            return await cb.answer("Товар закончился", show_alert=True)
        await cb.message.answer(f"<b>{p.name}</b>, {p.pack} — {rub(p.price)}\n\n{p.desc}", reply_markup=item_kb(p))
        await cb.answer()

    async def send_cart(message: Message, user_id: int, edit: bool = False):
        items = store.cart(user_id)
        if not items:
            text, markup = "Корзина пуста. Загляните в каталог или подберите чай.", menu_kb(smart.enabled)
        else:
            lines, total = cart_summary(items)
            note = "" if total >= FREE_DELIVERY_FROM else f"\nДо бесплатной доставки курьером: {rub(FREE_DELIVERY_FROM - total)}"
            text = f"<b>Корзина</b>\n{lines}\n\nИтого: <b>{rub(total)}</b>{note}"
            b = InlineKeyboardBuilder()
            for p, q in items:
                b.button(text=f"− {p.name}", callback_data=CartOp(id=p.id, d=-1))
                b.button(text=f"+ {q + 1}", callback_data=CartOp(id=p.id, d=1))
            b.button(text="Оформить заказ", callback_data=Nav(to="checkout"))
            b.button(text="Очистить", callback_data=Nav(to="clear"))
            b.button(text="← Каталог", callback_data=Nav(to="cats"))
            b.adjust(*([2] * len(items)), 1, 2)
            markup = b.as_markup()
        if edit:
            await message.edit_text(text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)

    @r.callback_query(Nav.filter(F.to == "cart"))
    async def cart(cb: CallbackQuery):
        await send_cart(cb.message, cb.from_user.id)
        await cb.answer()

    @r.callback_query(CartOp.filter())
    async def cart_op(cb: CallbackQuery, callback_data: CartOp):
        if callback_data.id not in cat.BY_ID or callback_data.d not in (1, -1):
            return await cb.answer("Нет такого товара", show_alert=True)
        qty = store.add(cb.from_user.id, callback_data.id, callback_data.d)
        name = cat.BY_ID[callback_data.id].name
        if (cb.message.text or "").startswith("Корзина"):
            await send_cart(cb.message, cb.from_user.id, edit=True)
        await cb.answer(f"{name}: {qty} шт." if qty else f"{name} убран из корзины")

    @r.callback_query(Nav.filter(F.to == "clear"))
    async def clear(cb: CallbackQuery):
        store.clear(cb.from_user.id)
        await send_cart(cb.message, cb.from_user.id, edit=True)
        await cb.answer("Корзина очищена")

    @r.callback_query(Nav.filter(F.to == "faq"))
    async def faq(cb: CallbackQuery):
        b = InlineKeyboardBuilder()
        for fid, (title, _a) in cat.FAQ.items():
            b.button(text=title, callback_data=Faq(id=fid))
        b.button(text="← Меню", callback_data=Nav(to="menu"))
        b.adjust(2)
        await cb.message.answer("Частые вопросы:", reply_markup=b.as_markup())
        await cb.answer()

    @r.callback_query(Faq.filter())
    async def faq_item(cb: CallbackQuery, callback_data: Faq):
        title, answer = cat.FAQ.get(callback_data.id, ("Вопрос", "Ответ не найден."))
        await cb.message.answer(f"<b>{title}</b>\n{answer}")
        await cb.answer()

    def pick_kb(qi: int):
        b = InlineKeyboardBuilder()
        for ai, (text, _v) in enumerate(cat.PICK_QUESTIONS[qi][1]):
            b.button(text=text, callback_data=Pick(q=qi, a=ai))
        b.adjust(1)
        return b.as_markup()

    @r.callback_query(Nav.filter(F.to == "pick"))
    async def pick_start(cb: CallbackQuery, state: FSMContext):
        await state.update_data(pick=[])
        await cb.message.answer(f"Подбор, вопрос 1 из 3\n<b>{cat.PICK_QUESTIONS[0][0]}</b>", reply_markup=pick_kb(0))
        await cb.answer()

    @r.callback_query(Pick.filter())
    async def pick_answer(cb: CallbackQuery, callback_data: Pick, state: FSMContext):
        answers = (await state.get_data()).get("pick", [])
        if callback_data.q != len(answers):
            return await cb.answer("Начните подбор заново из меню")
        answers.append(cat.PICK_QUESTIONS[callback_data.q][1][callback_data.a][1])
        await state.update_data(pick=answers)
        if len(answers) < len(cat.PICK_QUESTIONS):
            qi = len(answers)
            await cb.message.edit_text(f"Подбор, вопрос {qi + 1} из 3\n<b>{cat.PICK_QUESTIONS[qi][0]}</b>",
                                       reply_markup=pick_kb(qi))
            return await cb.answer()
        recs = cat.recommend(*answers)
        b = InlineKeyboardBuilder()
        for p in recs:
            b.button(text=f"+ {p.name} · {rub(p.price)}", callback_data=CartOp(id=p.id, d=1))
        b.button(text="Корзина", callback_data=Nav(to="cart"))
        b.button(text="Подобрать заново", callback_data=Nav(to="pick"))
        b.adjust(1)
        text = "Вот что подойдёт:\n\n" + "\n\n".join(f"<b>{p.name}</b>, {p.pack} — {rub(p.price)}\n{p.desc}" for p in recs)
        await cb.message.edit_text(text, reply_markup=b.as_markup())
        await cb.answer()

    @r.callback_query(Nav.filter(F.to == "checkout"))
    async def checkout(cb: CallbackQuery, state: FSMContext):
        if not store.cart(cb.from_user.id):
            return await cb.answer("Корзина пуста", show_alert=True)
        await state.set_state(Checkout.name)
        await cb.message.answer("Оформляем заказ. Как вас зовут? (отменить: /cancel)")
        await cb.answer()

    @r.message(Checkout.name, F.text & ~F.text.startswith("/"))
    async def co_name(message: Message, state: FSMContext):
        if len(message.text.strip()) < 2:
            return await message.answer("Напишите имя полностью.")
        await state.update_data(name=message.text.strip()[:60])
        await state.set_state(Checkout.phone)
        await message.answer("Телефон для связи:", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Отправить номер", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True))

    @r.message(Checkout.phone, F.contact | (F.text & ~F.text.startswith("/")))
    async def co_phone(message: Message, state: FSMContext):
        digits = re.sub(r"\D", "", message.contact.phone_number if message.contact else message.text)
        if not 10 <= len(digits) <= 15:
            return await message.answer("Нужен номер из 10–15 цифр, например +7 900 123-45-67.")
        await state.update_data(phone="+" + digits)
        await state.set_state(Checkout.delivery)
        b = InlineKeyboardBuilder()
        for did, (name, price) in DELIVERY.items():
            b.button(text=f"{name} · {rub(price) if price else 'бесплатно'}", callback_data=Deliv(id=did))
        b.adjust(1)
        await message.answer("Номер сохранён.", reply_markup=ReplyKeyboardRemove())
        await message.answer("Как получить заказ?", reply_markup=b.as_markup())

    async def ask_confirm(message: Message, state: FSMContext, user_id: int):
        data = await state.get_data()
        items = store.cart(user_id)
        lines, total = cart_summary(items)
        name, price = DELIVERY[data["delivery"]]
        fee = 0 if data["delivery"] == "courier" and total >= FREE_DELIVERY_FROM else price
        await state.update_data(total=total + fee, fee=fee)
        await state.set_state(Checkout.confirm)
        b = InlineKeyboardBuilder()
        b.button(text="Подтвердить", callback_data=Confirm(yes=1))
        b.button(text="Отменить", callback_data=Confirm(yes=0))
        await message.answer(
            f"<b>Проверьте заказ</b>\n{lines}\n\nПолучение: {name}"
            + (f", {html.escape(data['address'])}" if data.get("address") else "")
            + f"\nДоставка: {rub(fee)}\nИтого: <b>{rub(total + fee)}</b>\n"
            f"Получатель: {html.escape(data['name'])}, {data['phone']}",
            reply_markup=b.as_markup())

    @r.callback_query(Checkout.delivery, Deliv.filter())
    async def co_delivery(cb: CallbackQuery, callback_data: Deliv, state: FSMContext):
        if callback_data.id not in DELIVERY:
            return await cb.answer()
        await state.update_data(delivery=callback_data.id, address="")
        if callback_data.id == "pickup":
            await ask_confirm(cb.message, state, cb.from_user.id)
        else:
            await state.set_state(Checkout.address)
            await cb.message.answer("Адрес доставки (город, улица, дом, квартира) или адрес пункта выдачи:")
        await cb.answer()

    @r.message(Checkout.address, F.text & ~F.text.startswith("/"))
    async def co_address(message: Message, state: FSMContext):
        if len(message.text.strip()) < 8:
            return await message.answer("Слишком коротко. Укажите город, улицу и дом.")
        await state.update_data(address=message.text.strip()[:200])
        await ask_confirm(message, state, message.from_user.id)

    @r.callback_query(Checkout.confirm, Confirm.filter())
    async def co_confirm(cb: CallbackQuery, callback_data: Confirm, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await state.clear()
        if not callback_data.yes:
            await cb.message.edit_text("Оформление отменено, корзина сохранена.")
            return await cb.answer()
        items = store.cart(cb.from_user.id)
        if not items:
            await cb.message.edit_text("Корзина пуста — нечего оформлять.")
            return await cb.answer()
        lines, _ = cart_summary(items)
        order_id = store.save_order(cb.from_user.id, data["name"], data["phone"], DELIVERY[data["delivery"]][0],
                                    data.get("address", ""), lines, data["total"])
        store.clear(cb.from_user.id)
        log.info("Заказ #%s на %s", order_id, data["total"])
        await cb.message.edit_text(f"Заявка №{order_id} принята. Менеджер подтвердит наличие и пришлёт ссылку на "
                                   f"оплату в течение часа (11:00–21:00).")
        text = (f"Новая заявка №{order_id}\n{lines}\nДоставка: {DELIVERY[data['delivery']][0]} {rub(data['fee'])}"
                + (f"\nАдрес: {html.escape(data['address'])}" if data.get("address") else "")
                + f"\nИтого: {rub(data['total'])}\nКлиент: {html.escape(data['name'])}, {data['phone']}, "
                f"@{cb.from_user.username or '—'}")
        if ADMIN_ID:
            try:
                await bot.send_message(ADMIN_ID, text)
            except TelegramAPIError as e:
                log.error("Не удалось отправить заявку админу: %s", e)
        else:
            log.warning("ADMIN_ID не задан, заявка №%s только в БД", order_id)
        await cb.answer("Заявка отправлена")

    @r.message(Checkout.delivery)
    @r.message(Checkout.confirm)
    async def co_use_buttons(message: Message):
        await message.answer("Выберите вариант кнопкой выше. Отменить оформление: /cancel")

    @r.callback_query(Nav.filter(F.to == "ask"))
    async def ask(cb: CallbackQuery, state: FSMContext):
        if not smart.enabled:
            return await cb.answer("Умный режим выключен", show_alert=True)
        await state.set_state(Ask.waiting)
        await cb.message.answer("Напишите вопрос своими словами: например, «что взять в подарок маме, она любит "
                                "цветочный чай».")
        await cb.answer()

    @r.message(F.text & ~F.text.startswith("/"))
    async def free_text(message: Message, state: FSMContext, bot: Bot):
        if not smart.enabled:
            return await show_menu(message, "Я отвечаю кнопками. Выберите раздел:")
        await bot.send_chat_action(message.chat.id, "typing")
        reply = await smart.answer(message.from_user.id, message.text)
        await state.set_state(None)
        await message.answer(html.escape(reply), reply_markup=menu_kb(True))

    @r.message()
    async def fallback(message: Message):
        await show_menu(message, "Не понял сообщение. Выберите раздел:")

    return r


def build_dispatcher(store: Store, smart: SmartAdvisor) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(store, smart))

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
    store = Store(os.getenv("DB_PATH", "shop.db"))
    smart = SmartAdvisor()
    log.info("Умный режим: %s", f"включён ({smart.model})" if smart.enabled else "выключен (нет ANTHROPIC_API_KEY)")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await build_dispatcher(store, smart).start_polling(bot)
    finally:
        store.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
