"""Опциональный режим «умный ответ» через Claude API.

Включается, только если в .env задан ANTHROPIC_API_KEY. Без ключа бот работает на кнопках и FAQ.
"""
import logging
import os
from collections import defaultdict, deque

from catalog import SHOP, catalog_for_prompt

log = logging.getLogger("smart")

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM = (
    f"Ты — консультант чайной лавки «{SHOP}» в Telegram. Отвечай по-русски, коротко (до 5 предложений), "
    "дружелюбно и по делу. Рекомендуй только товары из каталога ниже, называй их точно и с ценой. "
    "Если вопрос не про чай, посуду или условия магазина, вежливо верни разговор к ассортименту. "
    "Не выдумывай скидки, акции, сроки и товары, которых нет в каталоге. Если не знаешь ответа, предложи "
    "оформить заявку, и менеджер уточнит. Не используй Markdown-разметку.\n\n" + catalog_for_prompt()
)


class SmartAdvisor:
    def __init__(self, api_key: str | None = None, model: str | None = None, history: int = 6):
        self.api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
        self.enabled = bool(self.api_key.strip())
        self._history: dict[int, deque] = defaultdict(lambda: deque(maxlen=history))
        self.client = None
        if self.enabled:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=self.api_key)

    async def answer(self, user_id: int, question: str) -> str:
        if not self.enabled:
            return ""
        import anthropic

        hist = self._history[user_id]
        messages = list(hist) + [{"role": "user", "content": question[:2000]}]
        kwargs = dict(model=self.model, max_tokens=1024, system=SYSTEM, messages=messages,
                      output_config={"effort": "low"})
        try:
            if self.model in FALLBACK_MODELS:
                resp = await self.client.beta.messages.create(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            log.warning("Claude API: превышен лимит запросов")
            return "Сейчас много вопросов, попробуйте через минуту или выберите ответ в разделе «Вопросы»."
        except anthropic.APIStatusError as e:
            log.error("Claude API вернул ошибку %s: %s", e.status_code, e.message)
            return "Умный ответ временно недоступен. Загляните в «Вопросы» или оформите заявку — ответит менеджер."
        except anthropic.APIConnectionError as e:
            log.error("Нет связи с Claude API: %s", e)
            return "Умный ответ временно недоступен, нет связи с сервисом. Попробуйте позже."

        if resp.stop_reason == "refusal":
            return "На этот вопрос не отвечу. Могу помочь с выбором чая, посуды или условиями доставки."
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            return "Не получилось сформулировать ответ. Попробуйте переформулировать вопрос."
        hist.append({"role": "user", "content": question[:2000]})
        hist.append({"role": "assistant", "content": text})
        return text
