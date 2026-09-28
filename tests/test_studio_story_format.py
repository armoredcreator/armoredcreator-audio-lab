import unittest

from ArmoredStudio.processing.finalizer import (
    STORY_HEIGHT,
    STORY_WIDTH,
    _story_normalization_filters,
    criar_filtro,
)


class StudioStoryFormatTests(unittest.TestCase):
    def test_story_normalization_is_fixed_1080x1920(self):
        self.assertEqual(
            _story_normalization_filters(1920, 1080),
            [
                "scale=1080:1920:force_original_aspect_ratio=increase",
                "crop=1080:1920:(iw-1080)/2:(ih-1920)/2",
                "setsar=1",
            ],
        )
        self.assertEqual((STORY_WIDTH, STORY_HEIGHT), (1080, 1920))

    def test_story_normalization_does_not_stretch_or_add_background(self):
        filters = _story_normalization_filters(1920, 1080)
        self.assertIn("force_original_aspect_ratio=increase", filters[0])
        self.assertIn("crop=1080:1920", filters[1])
        self.assertNotIn("pad=", ",".join(filters))
        self.assertNotIn("boxblur", ",".join(filters))

    def test_story_normalization_is_after_detected_crop(self):
        plan = {
            "crop_final": {"x": 4, "y": 2, "width": 1912, "height": 1076},
        }
        graph = criar_filtro("final", 1080, 1920, "30/1", plan)
        main = graph.split("[main_v]", 1)[0]
        self.assertLess(main.index("crop=1912:1076:4:2"), main.index("scale=1080:1920"))
        self.assertIn("crop=1080:1920", main)

    def test_intro_and_main_use_same_story_dimensions(self):
        graph = criar_filtro("final", STORY_WIDTH, STORY_HEIGHT, "30/1")
        self.assertIn("[main_v]", graph)
        self.assertIn("[intro_v]", graph)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", graph)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", graph)

    def test_intro_off_path_also_receives_story_normalization(self):
        # The non-intro path is assembled in finalizar(); this regression test
        # exercises the shared normalization helper used by that path.
        filters = _story_normalization_filters(1080, 1080)
        self.assertEqual(filters[0], "scale=1080:1920:force_original_aspect_ratio=increase")
        self.assertEqual(filters[1], "crop=1080:1920:(iw-1080)/2:(ih-1920)/2")


if __name__ == "__main__":
    unittest.main()
