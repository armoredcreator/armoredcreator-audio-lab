from ArmoredStudio.processing.finalizer import (
    INTRO_MUSIC_VOLUME,
    MUSIC_VOLUME,
    criar_filtro,
)


def test_effect_volume_and_loudness_are_aligned_between_intro_and_main():
    assert INTRO_MUSIC_VOLUME == MUSIC_VOLUME == 0.8
    graph = criar_filtro("final", 1080, 1920, "30/1")
    assert graph.count("loudnorm=I=-12:LRA=7:TP=-1") == 2
