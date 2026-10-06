from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

LEGACY_STORAGE_DIRS = {
    "storage/sync",
    "storage/queue",
    "storage/publish_queue",
    "storage/pipeline",
    "storage/hub",
    "storage/products",
    "storage/generated",
    "storage/rejected",
    "storage/archive",
}

PRODUCTION_DIRS = (
    ROOT / "ArmoredSync",
    ROOT / "ArmoredVision",
    ROOT / "ArmoredIA",
    ROOT / "ArmoredStudio",
    ROOT / "ArmoredHub",
    ROOT / "armored_core",
)

# ArmoredStudio/runtime contains local/vendor runtime assets (RVC environment,
# installed site-packages and models). It is intentionally ignored by Git and
# is not production source code to audit for project portability.
IGNORED_SOURCE_DIRS = {
    (ROOT / "ArmoredStudio" / "runtime").resolve(),
}


def test_repository_has_only_canonical_storage_roots():
    storage = ROOT / "storage"
    assert storage.is_dir()
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in storage.iterdir()
        if path.is_dir()
    }
    allowed = {
        "storage/database",
        "storage/videos",
        "storage/logs",
        "storage/backups",
        "storage/sources",
    }
    assert actual <= allowed


def test_legacy_storage_directories_are_not_present():
    for relative in LEGACY_STORAGE_DIRS:
        assert not (ROOT / relative).exists(), relative


def test_studio_has_no_legacy_v1_v2_architecture():
    studio = ROOT / "ArmoredStudio"
    assert not (studio / "modules" / "v1").exists()
    assert not (studio / "modules" / "v2").exists()


def test_repository_has_one_operational_launcher():
    assert (ROOT / "START_ALL.bat").is_file()
    assert not (ROOT / "START_COORDINATOR.bat").exists()
    assert not (ROOT / "armored_core" / "demo.py").exists()


@pytest.mark.parametrize("production_root", PRODUCTION_DIRS)
def test_production_code_has_no_machine_specific_windows_paths(production_root):
    if not production_root.exists():
        pytest.fail(f"Production root missing: {production_root}")

    forbidden_fragments = (
        "C:\\Users\\",
        "C:/Users/",
        "\\Desktop\\ArmoredCreator",
        "/Desktop/ArmoredCreator",
        "\\Downloads\\ArmoredCreator",
        "/Downloads/ArmoredCreator",
        "\\Documents\\ArmoredCreator",
        "/Documents/ArmoredCreator",
    )

    for source in production_root.rglob("*.py"):
        if any(
            source.resolve() == ignored
            or ignored in source.resolve().parents
            for ignored in IGNORED_SOURCE_DIRS
        ):
            continue

        text = source.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            assert fragment not in text, f"{source}: {fragment}"

def test_vision_v2_is_frozen_out_of_active_v1_service():
    service = (ROOT / "ArmoredVision" / "service.py").read_text(encoding="utf-8")
    assert "CandidateDiscovery" not in service
    assert "VisionCandidateError" not in service
    assert "ARMORED_VISION_V2_ENABLED" not in service


def test_armored_ia_is_the_only_caption_runtime_boundary():
    assert (ROOT / "ArmoredIA" / "service.py").is_file()
    assert not (ROOT / "ArmoredVision" / "modules" / "caption").exists()
    assert not (ROOT / "ArmoredVision" / "modules" / "v1" / "caption").exists()
