from __future__ import annotations

import pytest

from ArmoredIA.service import ArmoredIA


class _Generator:
    def __init__(self):
        self.calls = 0
    def generate(self, context):
        self.calls += 1
        assert context["productName"] == "Produto"
        return "Olha isso ✨\n#casa"


def test_armored_ia_dispatches_caption_without_knowing_gemini():
    generator = _Generator()
    ia = ArmoredIA(caption_generator=generator)

    assert ia.run("caption", {"productName": "Produto"}) == "Olha isso ✨\n#casa"
    assert generator.calls == 1


def test_armored_ia_rejects_unknown_task():
    ia = ArmoredIA(caption_generator=_Generator())
    with pytest.raises(ValueError, match="task não suportada"):
        ia.run("future-task", {})
