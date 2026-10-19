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
os.environ["ANTHROPIC_API_KEY"] = ""
import bot as app
from smart import SmartAdvisor
from types import SimpleNamespace as NS


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


class FakeMessages:

    def __init__(self, text="Возьмите Те Гуань Инь за 1 150 ₽.", stop="end_turn"):
        self.text, self.stop, self.kwargs = text, stop, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return NS(stop_reason=self.stop, content=[NS(type="thinking", thinking=""), NS(type="text", text=self.text)])


async def run():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = app.Store(os.path.join(tmp, "t.db"))
        session = MockSession()
        bot = Bot("123456:TEST-TOKEN", session=session, default=app.DefaultBotProperties(parse_mode="HTML"))
        smart = SmartAdvisor()
        assert not smart.enabled, "без ключа умный режим должен быть выключен"
        dp = app.build_dispatcher(store, smart)

        async def feed(update):
            await dp.feed_update(bot, update)
            return last_text(session)

        def texts(n=4):
            return " ".join((getattr(c, "text", None) or "") for c in session.calls[-n:])

        assert "Лист и пар" in await feed(msg(bot, "/start"))
        await feed(cb(bot, app.Nav(to="cats").pack()))
        await feed(cb(bot, app.Cat(id="oolong").pack()))
        assert "Улуны" in last_text(session)
        assert "Те Гуань Инь" in await feed(cb(bot, app.Item(id="tieguanyin").pack()))
        await feed(cb(bot, app.CartOp(id="tieguanyin", d=1).pack()))
        await feed(cb(bot, app.CartOp(id="tieguanyin", d=1).pack()))
        await feed(cb(bot, app.CartOp(id="gaiwan", d=1).pack()))
        assert store.cart(42)[0][1] == 2
        assert "3 790" in await feed(cb(bot, app.Nav(to="cart").pack()))

        await feed(cb(bot, app.Faq(id="delivery").pack()))
        assert "Курьером" in last_text(session)

        await feed(cb(bot, app.Nav(to="pick").pack()))
        for q, a in enumerate([1, 2, 0]):
            await feed(cb(bot, app.Pick(q=q, a=a).pack()))
        assert "Ромашка" in last_text(session), last_text(session)
        recs = app.cat.recommend("floral", "evening", "600")
        assert all(p.caffeine == 0 for p in recs)
        assert app.cat.recommend("deep", "morning", "any")[0].caffeine == 2

        await feed(cb(bot, app.Nav(to="checkout").pack()))
        await feed(msg(bot, "Ольга"))
        assert "10–15 цифр" in await feed(msg(bot, "123"))
        await feed(msg(bot, contact="+79001234567"))
        await feed(cb(bot, app.Deliv(id="courier").pack()))
        assert "Слишком коротко" in await feed(msg(bot, "дом"))
        await feed(msg(bot, "г. Примерный, ул. Тестовая, д. 1, кв. 2"))
        assert "Проверьте заказ" in last_text(session) and "Доставка: 0 ₽" in last_text(session)
        await feed(cb(bot, app.Confirm(yes=1).pack()))
        admin = [c for c in session.calls if getattr(c, "chat_id", None) == 999]
        assert admin and "Новая заявка" in admin[-1].text and "3 790" in admin[-1].text
        assert not store.cart(42), "корзина должна очиститься"
        assert "Заявка №1 принята" in texts(), texts()

        assert "кнопками" in await feed(msg(bot, "что посоветуете?"))

        smart_on = SmartAdvisor(api_key="test-key", model="claude-opus-5-5")
        fake = FakeMessages()
        smart_on.client = NS(messages=FakeMessages(), beta=NS(messages=fake))
        dp2 = app.build_dispatcher(store, smart_on)
        await dp2.feed_update(bot, msg(bot, "что взять маме, любит цветочный?"))
        assert "Те Гуань Инь" in last_text(session)
        assert fake.kwargs["fallbacks"] == "default" and fake.kwargs["model"] == "claude-opus-5-5"
        assert "Лист и пар" in fake.kwargs["system"] and "Сенча" in fake.kwargs["system"]
        await dp2.feed_update(bot, msg(bot, "а к нему посуду?"))
        assert len(fake.kwargs["messages"]) == 3, "история диалога не передаётся"
        fake.stop = "refusal"
        await dp2.feed_update(bot, msg(bot, "странный вопрос"))
        assert "Могу помочь" in last_text(session)
        smart_h = SmartAdvisor(api_key="k", model="claude-haiku-5-5")
        plain = FakeMessages("ok")
        smart_h.client = NS(messages=plain, beta=NS(messages=None))
        assert await smart_h.answer(1, "привет") == "ok" and "fallbacks" not in plain.kwargs
        store.close()
        print(f"OK: {len(session.calls)} вызовов Bot API: каталог, корзина, FAQ, подбор, заказ, умный режим")


if __name__ == "__main__":
    asyncio.run(run())
