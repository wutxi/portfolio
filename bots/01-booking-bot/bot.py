"""Бот записи в барбершоп «Кирпич» (концепт для портфолио, бренд и данные вымышлены).

Услуга → мастер → дата (inline-календарь) → время → телефон → подтверждение.
Уведомление админу, отмена записи, напоминание за 2 часа.
"""
import asyncio
import html
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ErrorEvent, Message, ReplyKeyboardRemove
from dotenv import load_dotenv

import keyboards as kb
from db import DB, parse_start

load_dotenv()
log = logging.getLogger("booking-bot")

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
REMIND_BEFORE = timedelta(hours=2)
INFO_TEXT = (
    "<b>Барбершоп «Кирпич»</b>\n"
    "ул. Вымышленная, 12 (адрес придуман для концепта)\n"
    "Ежедневно 10:00–21:00\n\n"
    + "\n".join(f"{n} — {p} ₽, {m} мин" for n, m, p in kb.SERVICES.values())
    + "\n\nОтменить запись без штрафа можно не позднее чем за 2 часа."
)


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


class Booking(StatesGroup):
    service = State()
    master = State()
    day = State()
    time = State()
    phone = State()
    confirm = State()


def describe(service_id: str, master_id: str, start: datetime) -> str:
    name, minutes, price = kb.SERVICES[service_id]
    return (f"Услуга: <b>{name}</b> ({minutes} мин, {price} ₽)\n"
            f"Мастер: <b>{kb.MASTERS[master_id][0]}</b>\n"
            f"Когда: <b>{start:%d.%m.%Y, %H:%M}</b>")


def build_router(db: DB) -> Router:
    r = Router()

    async def notify_admin(bot: Bot, text: str) -> None:
        if not ADMIN_ID:
            log.warning("ADMIN_ID не задан, уведомление не отправлено: %s", text)
            return
        try:
            await bot.send_message(ADMIN_ID, text)
        except TelegramAPIError as e:
            log.error("Не удалось уведомить админа: %s", e)

    @r.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(
            f"Здравствуйте, {html.escape(message.from_user.first_name or 'гость')}!\n"
            "Я запишу вас в барбершоп «Кирпич» за минуту: выберите услугу, мастера и время.\n\n"
            "<i>Концепт для портфолио, бренд и данные вымышлены.</i>",
            reply_markup=kb.main_menu(),
        )

    @r.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Запись прервана. Начать заново: /book", reply_markup=ReplyKeyboardRemove())

    @r.callback_query(kb.MenuCb.filter(F.action == "info"))
    async def info(cb: CallbackQuery):
        await cb.message.answer(INFO_TEXT)
        await cb.answer()

    async def ask_service(message: Message, state: FSMContext, edit: bool = False):
        await state.set_state(Booking.service)
        text = "Шаг 1 из 4. Выберите услугу:"
        if edit:
            await message.edit_text(text, reply_markup=kb.services_kb())
        else:
            await message.answer(text, reply_markup=kb.services_kb())

    @r.message(Command("book"))
    async def cmd_book(message: Message, state: FSMContext):
        await state.clear()
        await ask_service(message, state)

    @r.callback_query(kb.MenuCb.filter(F.action == "book"))
    async def menu_book(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await ask_service(cb.message, state)
        await cb.answer()

    @r.callback_query(Booking.service, kb.SvcCb.filter())
    async def on_service(cb: CallbackQuery, callback_data: kb.SvcCb, state: FSMContext):
        if callback_data.id not in kb.SERVICES:
            return await cb.answer("Такой услуги нет", show_alert=True)
        await state.update_data(service=callback_data.id)
        await state.set_state(Booking.master)
        await cb.message.edit_text(
            f"Шаг 2 из 4. {kb.SERVICES[callback_data.id][0]}. Кто будет работать?",
            reply_markup=kb.masters_kb(callback_data.id))
        await cb.answer()

    async def show_calendar(cb: CallbackQuery, state: FSMContext, year: int, month: int):
        data = await state.get_data()
        days_off = kb.MASTERS[data["master"]][2]
        await state.set_state(Booking.day)
        await cb.message.edit_text(
            f"Шаг 3 из 4. Мастер {kb.MASTERS[data['master']][0]}. Выберите день "
            f"(«·» — выходной или недоступно):",
            reply_markup=kb.calendar_kb(year, month, now_local().date(), days_off))

    @r.callback_query(Booking.master, kb.MasterCb.filter())
    async def on_master(cb: CallbackQuery, callback_data: kb.MasterCb, state: FSMContext):
        data = await state.get_data()
        master = kb.MASTERS.get(callback_data.id)
        if not master or data.get("service") not in master[1]:
            return await cb.answer("Мастер не делает эту услугу", show_alert=True)
        await state.update_data(master=callback_data.id)
        today = now_local().date()
        await show_calendar(cb, state, today.year, today.month)
        await cb.answer()

    @r.callback_query(Booking.day, kb.NavCb.filter())
    async def on_nav(cb: CallbackQuery, callback_data: kb.NavCb, state: FSMContext):
        await show_calendar(cb, state, int(callback_data.ym[:4]), int(callback_data.ym[4:]))
        await cb.answer()

    def slots_for(data: dict, day: date) -> list[datetime]:
        duration = kb.SERVICES[data["service"]][1]
        return kb.free_slots(day, duration, db.busy_intervals(data["master"], day), now_local())

    @r.callback_query(Booking.day, kb.DayCb.filter())
    async def on_day(cb: CallbackQuery, callback_data: kb.DayCb, state: FSMContext):
        day = datetime.strptime(callback_data.d, "%Y%m%d").date()
        data = await state.get_data()
        today = now_local().date()
        if not today <= day < today + timedelta(days=kb.HORIZON_DAYS) or day.weekday() in kb.MASTERS[data["master"]][2]:
            return await cb.answer("Этот день недоступен", show_alert=True)
        slots = slots_for(data, day)
        if not slots:
            return await cb.answer("На этот день свободного времени нет, выберите другой.", show_alert=True)
        await state.update_data(day=callback_data.d)
        await state.set_state(Booking.time)
        await cb.message.edit_text(f"Шаг 4 из 4. {day:%d.%m}, свободное время:", reply_markup=kb.slots_kb(slots))
        await cb.answer()

    @r.callback_query(Booking.time, kb.TimeCb.filter())
    async def on_time(cb: CallbackQuery, callback_data: kb.TimeCb, state: FSMContext):
        data = await state.get_data()
        day = datetime.strptime(data["day"], "%Y%m%d").date()
        start = datetime.combine(day, datetime.strptime(callback_data.t, "%H%M").time())
        if start not in slots_for(data, day):
            await cb.answer("Это время уже заняли, выберите другое.", show_alert=True)
            return await cb.message.edit_reply_markup(reply_markup=kb.slots_kb(slots_for(data, day)))
        await state.update_data(start=start.isoformat())
        await state.set_state(Booking.phone)
        await cb.message.edit_text(describe(data["service"], data["master"], start))
        await cb.message.answer("Оставьте номер телефона, чтобы мастер мог связаться при переносе:",
                                reply_markup=kb.phone_kb())
        await cb.answer()

    @r.message(Booking.phone, F.contact | (F.text & ~F.text.startswith("/")))
    async def on_phone(message: Message, state: FSMContext):
        raw = message.contact.phone_number if message.contact else message.text
        digits = re.sub(r"\D", "", raw or "")
        if not 10 <= len(digits) <= 15:
            return await message.answer("Не похоже на номер. Пример: +7 900 123-45-67. Прервать запись: /cancel")
        await state.update_data(phone="+" + digits)
        await state.set_state(Booking.confirm)
        data = await state.get_data()
        await message.answer("Номер сохранён.", reply_markup=ReplyKeyboardRemove())
        await message.answer(
            "Проверьте запись:\n\n" + describe(data["service"], data["master"], datetime.fromisoformat(data["start"]))
            + f"\nТелефон: <b>+{digits}</b>",
            reply_markup=kb.confirm_kb())

    @r.callback_query(Booking.confirm, kb.ConfirmCb.filter())
    async def on_confirm(cb: CallbackQuery, callback_data: kb.ConfirmCb, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await state.clear()
        if not callback_data.ok:
            await cb.message.edit_text("Запись отменена. Начать заново: /book")
            return await cb.answer()
        start = datetime.fromisoformat(data["start"])
        if start not in slots_for(data, start.date()):
            await cb.message.edit_text("Пока вы подтверждали, это время заняли. Выберите другое: /book")
            return await cb.answer()
        user = cb.from_user
        booking_id = db.add(user_id=user.id, user_name=user.full_name, phone=data["phone"],
                            service=data["service"], master=data["master"], start=start,
                            duration=kb.SERVICES[data["service"]][1])
        log.info("Новая запись #%s от %s", booking_id, user.id)
        await cb.message.edit_text(
            "Готово, вы записаны!\n\n" + describe(data["service"], data["master"], start)
            + "\n\nНапомню за 2 часа. Посмотреть или отменить: /my")
        await notify_admin(bot, f"Новая запись #{booking_id}\n" + describe(data["service"], data["master"], start)
                           + f"\nКлиент: {html.escape(user.full_name)}, {data['phone']}")
        await cb.answer("Записали")

    @r.callback_query(kb.BackCb.filter())
    async def on_back(cb: CallbackQuery, callback_data: kb.BackCb, state: FSMContext):
        data = await state.get_data()
        if callback_data.to == "service" or "service" not in data:
            await ask_service(cb.message, state, edit=True)
        elif callback_data.to == "master" or "master" not in data:
            await state.set_state(Booking.master)
            await cb.message.edit_text("Шаг 2 из 4. Выберите мастера:", reply_markup=kb.masters_kb(data["service"]))
        else:
            today = now_local().date()
            await show_calendar(cb, state, today.year, today.month)
        await cb.answer()

    @r.callback_query(F.data == kb.IGNORE)
    async def ignore(cb: CallbackQuery):
        await cb.answer()

    async def send_my(message: Message, user_id: int):
        rows = db.user_upcoming(user_id, now_local())
        if not rows:
            return await message.answer("Активных записей нет. Записаться: /book")
        lines = [describe(x["service"], x["master"], parse_start(x["start"])) for x in rows]
        await message.answer("Ваши записи:\n\n" + "\n\n".join(lines), reply_markup=kb.my_bookings_kb(rows))

    @r.message(Command("my"))
    async def cmd_my(message: Message):
        await send_my(message, message.from_user.id)

    @r.callback_query(kb.MenuCb.filter(F.action == "my"))
    async def menu_my(cb: CallbackQuery):
        await send_my(cb.message, cb.from_user.id)
        await cb.answer()

    @r.callback_query(kb.CancelCb.filter())
    async def on_cancel_booking(cb: CallbackQuery, callback_data: kb.CancelCb, bot: Bot):
        row = db.get(callback_data.id)
        if not row or not db.cancel(callback_data.id, cb.from_user.id):
            return await cb.answer("Запись не найдена или уже отменена", show_alert=True)
        text = describe(row["service"], row["master"], parse_start(row["start"]))
        await cb.message.answer("Запись отменена:\n" + text)
        await notify_admin(bot, f"Отмена записи #{row['id']}\n{text}\nКлиент: {html.escape(row['user_name'] or '')}, {row['phone']}")
        await cb.answer("Отменено")

    @r.message(Command("today"), F.from_user.id == ADMIN_ID)
    async def cmd_today(message: Message):
        rows = db.for_day(now_local().date())
        if not rows:
            return await message.answer("Сегодня записей нет.")
        await message.answer("Записи на сегодня:\n" + "\n".join(
            f"{parse_start(x['start']):%H:%M} {kb.MASTERS[x['master']][0]} — {kb.SERVICES[x['service']][0]}, "
            f"{html.escape(x['user_name'] or '')} {x['phone']}" for x in rows))

    @r.message(Booking.service)
    @r.message(Booking.master)
    @r.message(Booking.day)
    @r.message(Booking.time)
    @r.message(Booking.confirm)
    async def use_buttons(message: Message):
        await message.answer("Пожалуйста, выберите вариант кнопкой выше. Прервать запись: /cancel")

    @r.message()
    async def fallback(message: Message):
        await message.answer("Я умею записывать на стрижку. Выберите действие:", reply_markup=kb.main_menu())

    return r


async def reminder_loop(bot: Bot, db: DB, interval: int = 60) -> None:
    while True:
        try:
            for row in db.due_reminders(now_local(), REMIND_BEFORE):
                start = parse_start(row["start"])
                try:
                    await bot.send_message(
                        row["user_id"],
                        f"Напоминаю: сегодня в {start:%H:%M} — {kb.SERVICES[row['service']][0]} "
                        f"у мастера {kb.MASTERS[row['master']][0]}. Если планы поменялись, отмените запись: /my")
                except TelegramAPIError as e:
                    log.warning("Напоминание #%s не доставлено: %s", row["id"], e)
                db.mark_reminded(row["id"])
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(interval)


def build_dispatcher(db: DB) -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(build_router(db))

    @dp.errors()
    async def on_error(event: ErrorEvent):
        log.exception("Ошибка при обработке апдейта: %s", event.exception, exc_info=event.exception)
        upd = event.update
        target = upd.message or (upd.callback_query.message if upd.callback_query else None)
        if target:
            try:
                await target.answer("Что-то пошло не так. Попробуйте ещё раз: /start")
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
    db = DB(os.getenv("DB_PATH", "bookings.db"))
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(db)
    reminder = asyncio.create_task(reminder_loop(bot, db))
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        reminder.cancel()
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
