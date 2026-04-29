from __future__ import annotations

from telegram import Message, MessageEntity
from telegram.constants import MessageEntityType

from backlog_tamer.agents.intake_triage.schemas import IncomingContext, SourceLink


def build_incoming_context(message: Message) -> IncomingContext:
    raw_text = message.text or message.caption or ""

    url_entities = _entities_of_types(message, [MessageEntityType.URL])
    text_link_entities = _entities_of_types(message, [MessageEntityType.TEXT_LINK])

    visible_urls = [_substring(raw_text, e) for e in url_entities]
    text_link_urls = [e.url for e in text_link_entities if e.url]

    links = _dedupe_preserving_order([*visible_urls, *text_link_urls])

    note = _strip_substrings(raw_text, visible_urls).strip() or None

    return IncomingContext(
        raw_text=raw_text,
        note=note,
        links=[SourceLink(url=url) for url in links],
    )


def _entities_of_types(
    message: Message,
    types: list[MessageEntityType],
) -> list[MessageEntity]:
    entities = message.entities or message.caption_entities or ()
    wanted = {t.value for t in types}
    return [e for e in entities if e.type in wanted]


def _substring(text: str, entity: MessageEntity) -> str:
    encoded = text.encode("utf-16-le")
    start = entity.offset * 2
    end = start + entity.length * 2
    return encoded[start:end].decode("utf-16-le")


def _strip_substrings(text: str, substrings: list[str]) -> str:
    result = text
    for substring in sorted(set(substrings), key=len, reverse=True):
        if substring:
            result = result.replace(substring, " ")
    return " ".join(result.split())


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
