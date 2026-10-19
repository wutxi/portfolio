"""Каталог услуг, мастера, inline-клавиатуры и календарь."""
import calendar
from datetime import date, datetime, timedelta

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

SERVICES = {
    "cut": ("Мужская стрижка", 60, 1800),
    "clip": ("Стрижка машинкой", 30, 1000),
    "beard": ("Оформление бороды", 30, 1300),
    "combo": ("Стрижка + борода", 90, 2800),
    "shave": ("Королевское бритьё", 60, 1600),
}
MASTERS = {
    "ars": ("Арсений", {"cut", "beard", "combo", "shave"}, {0}),
    "tim": ("Тимур", {"cut", "clip", "combo", "beard"}, {2}),
    "gle": ("Глеб", {"beard", "shave", "clip"}, {4}),
}
OPEN_HOUR, CLOSE_HOUR, STEP_MIN = 10, 21, 30
HORIZON_DAYS = 14

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
          "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class MenuCb(CallbackData, prefix="menu"):
    action: str


class SvcCb(CallbackData, prefix="svc"):
    id: str


class MasterCb(CallbackData, prefix="mst"):
    id: str


class NavCb(CallbackData, prefix="nav"):
    ym: str


class DayCb(CallbackData, prefix="day"):
    d: str


class TimeCb(CallbackData, prefix="tm"):
    t: str


class BackCb(CallbackData, prefix="back"):
    to: str


class ConfirmCb(CallbackData, prefix="cf"):
    ok: int


class CancelCb(CallbackData, prefix="cnl"):
    id: int


IGNORE = "ignore"


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Записаться", callback_data=MenuCb(action="book"))
    b.button(text="Мои записи", callback_data=MenuCb(action="my"))
    b.button(text="Прайс и адрес", callback_data=MenuCb(action="info"))
    b.adjust(1, 2)
    return b.as_markup()


def services_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for sid, (name, minutes, price) in SERVICES.items():
        b.button(text=f"{name} · {minutes} мин · {price} ₽", callback_data=SvcCb(id=sid))
    b.adjust(1)
    return b.as_markup()


def masters_kb(service_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for mid, (name, skills, _off) in MASTERS.items():
        if service_id in skills:
            b.button(text=name, callback_data=MasterCb(id=mid))
    b.button(text="← К услугам", callback_data=BackCb(to="service"))
    b.adjust(1)
    return b.as_markup()


def free_slots(day: date, duration: int, busy: list[tuple[datetime, datetime]],
               now: datetime) -> list[datetime]:
    slots = []
    t = datetime.combine(day, datetime.min.time()).replace(hour=OPEN_HOUR)
    close = t.replace(hour=CLOSE_HOUR)
    while t + timedelta(minutes=duration) <= close:
        end = t + timedelta(minutes=duration)
        if t >= now + timedelta(minutes=30) and all(end <= bs or t >= be for bs, be in busy):
            slots.append(t)
        t += timedelta(minutes=STEP_MIN)
    return slots


def calendar_kb(year: int, month: int, today: date, days_off: set[int]) -> InlineKeyboardMarkup:
    last = today + timedelta(days=HORIZON_DAYS - 1)
    rows: list[list[InlineKeyboardButton]] = []
    prev_ym = (year, month - 1) if month > 1 else (year - 1, 12)
    next_ym = (year, month + 1) if month < 12 else (year + 1, 1)
    has_prev = (year, month) > (today.year, today.month)
    has_next = (last.year, last.month) > (year, month)
    rows.append([
        InlineKeyboardButton(text="‹" if has_prev else " ",
                             callback_data=NavCb(ym=f"{prev_ym[0]}{prev_ym[1]:02d}").pack() if has_prev else IGNORE),
        InlineKeyboardButton(text=f"{MONTHS[month - 1]} {year}", callback_data=IGNORE),
        InlineKeyboardButton(text="›" if has_next else " ",
                             callback_data=NavCb(ym=f"{next_ym[0]}{next_ym[1]:02d}").pack() if has_next else IGNORE),
    ])
    rows.append([InlineKeyboardButton(text=w, callback_data=IGNORE) for w in WEEKDAYS])
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for d in week:
            active = d.month == month and today <= d <= last and d.weekday() not in days_off
            if d.month != month:
                row.append(InlineKeyboardButton(text=" ", callback_data=IGNORE))
            elif active:
                row.append(InlineKeyboardButton(text=str(d.day), callback_data=DayCb(d=d.strftime("%Y%m%d")).pack()))
            else:
                row.append(InlineKeyboardButton(text="·", callback_data=IGNORE))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="← К мастерам", callback_data=BackCb(to="master").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_kb(slots: list[datetime]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in slots:
        b.button(text=s.strftime("%H:%M"), callback_data=TimeCb(t=s.strftime("%H%M")))
    b.adjust(4)
    b.row(InlineKeyboardButton(text="← К календарю", callback_data=BackCb(to="date").pack()))
    return b.as_markup()


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
        input_field_placeholder="Или введите номер вручную",
    )


def confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Подтвердить", callback_data=ConfirmCb(ok=1))
    b.button(text="Отменить", callback_data=ConfirmCb(ok=0))
    return b.as_markup()


def my_bookings_kb(rows) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for r in rows:
        start = datetime.strptime(r["start"], "%Y-%m-%dT%H:%M")
        b.button(text=f"Отменить {start:%d.%m %H:%M}", callback_data=CancelCb(id=r["id"]))
    b.adjust(1)
    return b.as_markup()
