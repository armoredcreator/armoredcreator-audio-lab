from __future__ import annotations

import json
import os

import pytest
import requests

from ArmoredIA.caption.generator import CaptionGenerator, CaptionGenerationError
from ArmoredIA.caption.policy import CaptionPolicyError, validate_caption
from ArmoredIA.providers.gemini import GeminiProvider


def _gemini_response(captions):
    return {
        "candidates": [{
            "content": {"parts": [{"text": json.dumps({"captions": captions}, ensure_ascii=False)}]}
        }]
    }


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("ARMORED_IA_ENABLED", raising=False)
    monkeypatch.delenv("ARMORED_IA_CAPTION_ENABLED", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ARMORED_IA_MODEL", raising=False)
    monkeypatch.delenv("ARMORED_IA_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("ARMORED_IA_RETRY_DELAY", raising=False)


def test_policy_accepts_contextual_caption():
    assert validate_caption(
        "Cores organizadas ✨\n#unhas #organizacao",
        product_name="Expositor de esmaltes com gavetas",
    ) == "Cores organizadas ✨\n#unhas #organizacao"


def test_policy_allows_natural_overlap_with_description():
    assert validate_caption(
        "Cantinho resistente ✨\n#casa",
        product_name="Bancada Suspensa",
        product_context={"description": "estrutura de aço carbono resistente"},
    ) == "Cantinho resistente ✨\n#casa"


def test_policy_blocks_only_hard_leaks():
    invalid = (
        "Compre agora 🔥\n#oferta",
        "Batom matte ✨\n#beleza",
        "Olha isso ✨\n#batommatte",
        "Tramontina linda ✨\n#casa",
        "Pro 900 lindo ✨\n#casa",
        "Medida 10ml ✨\n#beleza",
    )
    for caption in invalid:
        with pytest.raises(CaptionPolicyError):
            validate_caption(
                caption,
                product_name="Batom Matte Vermelho",
                product_context={"brand": "Tramontina", "model": "Pro 900"},
            )


def test_provider_makes_one_request_and_returns_up_to_ten(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "configured")
    calls = {"count": 0}

    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return _gemini_response([
                "Compre agora 🔥\n#oferta",
                "Olha esse charme ✨\n#beleza",
                "Visual bonito 😍\n#beleza",
            ])

    def requester(*args, **kwargs):
        calls["count"] += 1
        return Response()

    provider = GeminiProvider(requester=requester)
    candidates = provider.generate_candidates("caption", {
        "productName": "Batom Matte Vermelho",
    }, 10)

    assert calls["count"] == 1
    assert len(candidates) == 3


def test_caption_selects_valid_candidate_without_second_gemini_call(monkeypatch):
    monkeypatch.setenv("ARMORED_IA_CAPTION_ENABLED", "1")

    class Provider:
        def __init__(self):
            self.calls = 0
        def generate_candidates(self, task, context, limit):
            self.calls += 1
            return [
                "Compre agora 🔥\n#oferta",
                "Olha esse charme ✨\n#beleza",
            ]

    provider = Provider()
    caption = CaptionGenerator(provider=provider).generate({
        "productName": "Batom Matte Vermelho",
    })

    assert caption == "Olha esse charme ✨\n#beleza"
    assert provider.calls == 1


def test_all_policy_rejections_are_one_request_then_recovery(monkeypatch):
    monkeypatch.setenv("ARMORED_IA_CAPTION_ENABLED", "1")

    class Provider:
        def __init__(self):
            self.calls = 0
        def generate_candidates(self, task, context, limit):
            self.calls += 1
            return [
                "Compre agora 🔥\n#oferta",
                "Batom matte ✨\n#beleza",
                "Olha isso 😍✨\n#beleza",
            ]

    provider = Provider()
    with pytest.raises(CaptionGenerationError):
        CaptionGenerator(provider=provider).generate({
            "productName": "Batom Matte Vermelho",
        })

    assert provider.calls == 1


def test_provider_retries_transport_but_not_policy(monkeypatch):
    monkeypatch.setenv("ARMORED_IA_CAPTION_ENABLED", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "configured")
    monkeypatch.setenv("ARMORED_IA_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("ARMORED_IA_RETRY_DELAY", "0")

    calls = {"count": 0}

    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return _gemini_response(["Cantinho profissional ✨\n#barbearia"])

    def requester(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise requests.exceptions.ReadTimeout("temporary")
        return Response()

    provider = GeminiProvider(requester=requester)
    caption = CaptionGenerator(provider=provider).generate({
        "productName": "Bancada Suspensa",
    })

    assert calls["count"] == 3
    assert caption == "Cantinho profissional ✨\n#barbearia"
