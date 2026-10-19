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
        store = app.Store(os.path.join(tmp, "t.db"))
        session = MockSession()
        bot = Bot("123456:TEST-TOKEN", session=session, default=app.DefaultBotProperties(parse_mode="HTML"))
        dp = app.build_dispatcher(store)

        async def feed(update):
            await dp.feed_update(bot, update)
            return last_text(session)

        def texts(n=4):
            return " ".join((getattr(c, "text", None) or getattr(c, "caption", None) or "") for c in session.calls[-n:])

        assert "чек-лист" in (await feed(msg(bot, "/start"))).lower()
        await feed(cb(bot, app.Act(name="magnet").pack()))
        assert any(type(c).__name__ == "SendDocument" for c in session.calls), "PDF не отправлен"
        assert store.stats()["magnet"] == 1
        await feed(cb(bot, app.Act(name="lead").pack()))
        assert "Сначала" in (getattr(session.calls[-1], "text", "") or "")

        assert "Вопрос 1 из 4" in await feed(cb(bot, app.Act(name="quiz").pack()))
        for q, a in enumerate([2, 2, 1, 2]):
            await feed(cb(bot, app.Ans(q=q, a=a).pack()))
        assert "Система" in last_text(session), last_text(session)
        await feed(cb(bot, app.Ans(q=0, a=0).pack()))

        await feed(cb(bot, app.Act(name="lead").pack()))
        assert "обращаться" in last_text(session)
        await feed(msg(bot, "Ирина"))
        assert "Нужен телефон" in await feed(msg(bot, "12"))
        await feed(msg(bot, "@irina_test"))
        assert "Заявка принята" in texts(), texts()
        admin = [c for c in session.calls if getattr(c, "chat_id", None) == 999]
        assert admin and "Аудит" in admin[-1].text and "@irina_test" in admin[-1].text
        s = store.stats()
        assert s["leads"] == 1 and s["by_segment"] == {"system": 1}, s

        assert app.c.segment_for(0)[1] == "start" and app.c.segment_for(3)[1] == "growth"             and app.c.segment_for(5)[1] == "growth" and app.c.segment_for(8)[1] == "system"
        await feed(cb(bot, app.Act(name="quiz").pack()))
        for q in range(4):
            await feed(cb(bot, app.Ans(q=q, a=0).pack()))
        assert "Старт" in last_text(session)
        await feed(cb(bot, app.Act(name="lead").pack()))
        assert "Отменено" in await feed(msg(bot, "/cancel"))
        store.close()
        print(f"OK: {len(session.calls)} вызовов Bot API, воронка магнит → квиз → сегмент → заявка пройдена")


if __name__ == "__main__":
    asyncio.run(run())
