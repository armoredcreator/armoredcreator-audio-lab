from __future__ import annotations

from pathlib import Path

from armored_core.storage import Storage


def test_sources_use_identical_top_level_workspace_shape(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARMORED_SOURCE_1_ID", "-1003788989075")
    monkeypatch.setenv("ARMORED_SOURCE_1_VIDEO_DIR", "Videos GRUPO_FONTE_1")
    monkeypatch.setenv("ARMORED_SOURCE_2_ID", "-1002698134896")
    monkeypatch.setenv("ARMORED_SOURCE_2_VIDEO_DIR", "Videos GRUPO_FONTE_2")

    storage = Storage(tmp_path)

    source1 = storage.workspace("550", source_id="-1003788989075")
    source2 = storage.workspace("550", source_id="-1002698134896")

    assert source1 == tmp_path / "storage" / "Videos GRUPO_FONTE_1" / "550"
    assert source2 == tmp_path / "storage" / "Videos GRUPO_FONTE_2" / "550"
    assert source1 != source2
    assert not (tmp_path / "storage" / "sources").exists()


def test_each_source_keeps_the_same_canonical_filenames(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARMORED_SOURCE_1_ID", "-1003788989075")
    monkeypatch.setenv("ARMORED_SOURCE_1_VIDEO_DIR", "Videos GRUPO_FONTE_1")
    monkeypatch.setenv("ARMORED_SOURCE_2_ID", "-1002698134896")
    monkeypatch.setenv("ARMORED_SOURCE_2_VIDEO_DIR", "Videos GRUPO_FONTE_2")

    storage = Storage(tmp_path)

    for source_id, folder in (
        ("-1003788989075", "Videos GRUPO_FONTE_1"),
        ("-1002698134896", "Videos GRUPO_FONTE_2"),
    ):
        original = storage.original("177625", original_url="https://shopee.com.br/product/abc", source_id=source_id)
        working = storage.working("177625", source_id=source_id)
        result = storage.result(
            "177625",
            affiliate_url="https://affiliate.invalid/novo",
            source_id=source_id,
        )
        base = tmp_path / "storage" / folder / "177625"
        assert original == base / "177625_abc.mp4"
        assert working == base / "177625_.mp4"
        assert result == base / "177625_novo.mp4"


def test_unconfigured_legacy_storage_remains_available_for_unit_tests(tmp_path: Path):
    storage = Storage(tmp_path)
    assert storage.workspace("550", source_id="telegram") == tmp_path / "storage" / "videos" / "550"
