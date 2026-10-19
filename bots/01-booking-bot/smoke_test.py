"""Смоук-тест без Telegram: подменяем HTTP-сессию бота и прогоняем апдейты через диспетчер.

Запуск: python smoke_test.py   (токен не нужен, данные пишутся во временную БД)
"""
import asyncio
import os
import tempfile
import time
from datetime import timedelta

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Update

os.environ.setdefault("ADMIN_ID", "999")
import bot as app
import keyboards as kb
from db import DB


class MockSession(BaseSession):

    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if getattr(method, "__returning__", None) is Message:
            return Message.model_validate({"message_id": len(self.calls), "date": int(time.time()),
                                           "chat": {"id": getattr(method, "chat_id", 1), "type": "private"},
                                           "text": "ok"}, context={"bot": bot})
        return True

    async def stream_content(self, *a, **kw):
        yield b""

    async def close(self):
        pass


USER = {"id": 42, "is_bot": False, "first_name": "Тест"}
_uid = 0


def _next():
    global _uid
    _uid += 1
    return _uid


def msg(bot, text=None, contact=None):
    m = {"message_id": _next(), "date": int(time.time()), "chat": {"id": 42, "type": "private"}, "from": USER}
    if text:
        m["text"] = text
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if contact:
        m["contact"] = {"phone_number": contact, "first_name": "Тест", "user_id": 42}
    return Update.model_validate({"update_id": _next(), "message": m}, context={"bot": bot})


def cb(bot, data):
    return Update.model_validate({"update_id": _next(), "callback_query": {
        "id": str(_next()), "from": USER, "chat_instance": "ci", "data": data,
        "message": {"message_id": 1, "date": int(time.time()), "chat": {"id": 42, "type": "private"}, "text": "x"}}},
        context={"bot": bot})


def last_text(session):
    for m in reversed(session.calls):
        if hasattr(m, "text") and m.text:
            return m.text
    return ""


async def run():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = DB(os.path.join(tmp, "t.db"))
        session = MockSession()
        bot = Bot("123456:TEST-TOKEN", session=session, default=app.DefaultBotProperties(parse_mode="HTML"))
        dp = app.build_dispatcher(db)

        async def feed(update):
            await dp.feed_update(bot, update)
            return last_text(session)

        assert "Кирпич" in await feed(msg(bot, "/start"))
        assert "Шаг 1" in await feed(msg(bot, "/book"))
        assert "Шаг 2" in await feed(cb(bot, kb.SvcCb(id="cut").pack()))
        assert "Шаг 3" in await feed(cb(bot, kb.MasterCb(id="tim").pack()))

        day = app.now_local().date() + timedelta(days=1)
        while day.weekday() in kb.MASTERS["tim"][2]:
            day += timedelta(days=1)
        assert "Шаг 4" in await feed(cb(bot, kb.DayCb(d=day.strftime("%Y%m%d")).pack()))
        await feed(cb(bot, kb.TimeCb(t="1200").pack()))
        assert "номер" in last_text(session).lower()
        assert "Не похоже" in await feed(msg(bot, "abc"))
        await feed(msg(bot, contact="+79001112233"))
        assert "Проверьте запись" in last_text(session)
        await feed(cb(bot, kb.ConfirmCb(ok=1).pack()))
        admin_msgs = [c for c in session.calls if getattr(c, "chat_id", None) == 999]
        assert admin_msgs and "Новая запись" in admin_msgs[-1].text, "админ не уведомлён"

        busy = db.busy_intervals("tim", day)
        free = kb.free_slots(day, 60, busy, app.now_local())
        assert all(s.hour != 12 for s in free) and any(s.hour == 13 and s.minute == 0 for s in free)

        assert "Ваши записи" in await feed(msg(bot, "/my"))
        booking_id = db.user_upcoming(42, app.now_local())[0]["id"]
        await feed(cb(bot, kb.CancelCb(id=booking_id).pack()))
        assert any("Запись отменена" in (getattr(c, "text", "") or "") for c in session.calls[-3:])
        assert db.get(booking_id)["status"] == "cancelled"
        assert "Отмена записи" in [c for c in session.calls if getattr(c, "chat_id", None) == 999][-1].text

        soon = app.now_local() + timedelta(minutes=90)
        rid = db.add(user_id=42, user_name="Тест", phone="+7900", service="cut", master="ars",
                     start=soon, duration=60)
        task = asyncio.create_task(app.reminder_loop(bot, db, interval=3600))
        await asyncio.sleep(0.2)
        task.cancel()
        assert db.get(rid)["reminded"] == 1, "напоминание не отправлено"
        assert "Напоминаю" in last_text(session)

        markup = kb.calendar_kb(day.year, day.month, app.now_local().date(), {2})
        assert len(markup.inline_keyboard) >= 6
        await feed(msg(bot, "просто текст"))
        db.close()
        print(f"OK: {len(session.calls)} вызовов Bot API, сценарий записи/отмены/напоминания пройден")


if __name__ == "__main__":
    asyncio.run(run())
