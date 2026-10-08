from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceConfig:
    key: str
    chat_id: str
    source_id: str


@dataclass(frozen=True)
class HubConfig:
    key: str
    chat_id: str
    topic_id: int


@dataclass(frozen=True)
class RouteConfig:
    source: SourceConfig
    hub: HubConfig


def _required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} não configurado")
    return value


def load_routes() -> tuple[RouteConfig, ...]:
    routes: list[RouteConfig] = []
    index = 1
    while True:
        chat = (os.getenv(f"ARMORED_SOURCE_{index}_CHAT_ID") or "").strip()
        if not chat:
            break
        source_id = (os.getenv(f"ARMORED_SOURCE_{index}_ID") or chat).strip()
        source_key = (os.getenv(f"ARMORED_SOURCE_{index}_KEY") or f"source{index}").strip()

        hub_chat = _required(f"ARMORED_HUB_{index}_CHAT_ID")
        topic_raw = _required(f"ARMORED_HUB_{index}_TOPIC_ID")
        try:
            topic_id = int(topic_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"ARMORED_HUB_{index}_TOPIC_ID inválido: {topic_raw!r}"
            ) from exc

        routes.append(
            RouteConfig(
                SourceConfig(source_key, chat, source_id),
                HubConfig(f"hub{index}", hub_chat, topic_id),
            )
        )
        index += 1

    if routes:
        expected_raw = (os.getenv("ARMORED_REQUIRED_SOURCE_COUNT") or "").strip()
        if expected_raw:
            try:
                expected_count = int(expected_raw)
            except ValueError as exc:
                raise RuntimeError(
                    f"ARMORED_REQUIRED_SOURCE_COUNT inválido: {expected_raw!r}"
                ) from exc
            if expected_count < 1:
                raise RuntimeError("ARMORED_REQUIRED_SOURCE_COUNT deve ser maior que zero")
            if len(routes) != expected_count:
                raise RuntimeError(
                    f"Esperadas {expected_count} fontes Telegram, mas somente {len(routes)} rota(s) "
                    "estão completas em ARMORED_SOURCE_n_* / ARMORED_HUB_n_*; "
                    "a inicialização foi interrompida para não omitir fontes silenciosamente."
                )
        return tuple(routes)

    source = (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
    source_id = (os.getenv("ARMORED_SYNC_SOURCE_ID") or source).strip()
    hub_chat = (os.getenv("ARMORED_CREATOR_GROUP_ID") or "").strip()
    topic = (os.getenv("ARMORED_HUB_TOPIC_ID") or "").strip()
    if source and source_id and hub_chat and topic:
        try:
            topic_id = int(topic)
        except ValueError as exc:
            raise RuntimeError(f"ARMORED_HUB_TOPIC_ID inválido: {topic!r}") from exc
        return (
            RouteConfig(
                SourceConfig("source1", source, source_id),
                HubConfig("hub1", hub_chat, topic_id),
            ),
        )
    return ()
