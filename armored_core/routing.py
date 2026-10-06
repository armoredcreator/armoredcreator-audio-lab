from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceConfig:
    key: str
    chat_id: str
    source_id: str
    hub_key: str


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
        source_key = (os.getenv(f"ARMORED_SOURCE_{index}_KEY") or "").strip()
        source_chat = (os.getenv(f"ARMORED_SOURCE_{index}_CHAT_ID") or "").strip()
        if not source_key and not source_chat:
            break
        source_chat = source_chat or _required(f"ARMORED_SOURCE_{index}_CHAT_ID")
        source_id = (os.getenv(f"ARMORED_SOURCE_{index}_ID") or source_chat).strip()
        hub_key = (os.getenv(f"ARMORED_SOURCE_{index}_HUB") or f"hub{index}").strip()
        hub_chat = _required(f"ARMORED_HUB_{hub_key.upper()}_CHAT_ID")
        topic_raw = _required(f"ARMORED_HUB_{hub_key.upper()}_TOPIC_ID")
        try:
            topic_id = int(topic_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"ARMORED_HUB_{hub_key.upper()}_TOPIC_ID inválido: {topic_raw!r}"
            ) from exc
        routes.append(RouteConfig(
            SourceConfig(source_key or f"source{index}", source_chat, source_id, hub_key),
            HubConfig(hub_key, hub_chat, topic_id),
        ))
        index += 1

    if routes:
        return tuple(routes)

    # Backward-compatible Source 1 / Hub 1 configuration.
    source = (os.getenv("ARMORED_SYNC_SOURCE") or "").strip()
    source_id = (os.getenv("ARMORED_SYNC_SOURCE_ID") or source).strip()
    hub_chat = (os.getenv("ARMORED_CREATOR_GROUP_ID") or "").strip()
    topic = (os.getenv("ARMORED_HUB_TOPIC_ID") or "").strip()
    if source and source_id and hub_chat and topic:
        return (RouteConfig(
            SourceConfig("source1", source, source_id, "hub1"),
            HubConfig("hub1", hub_chat, int(topic)),
        ),)
    return ()
