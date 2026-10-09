import unittest
from types import SimpleNamespace

from ArmoredVision.service import ArmoredVision
from armored_core.services import VisionUnresolvedError


class VisionMissingLinkTests(unittest.TestCase):
    def test_missing_shopee_url_is_a_waiting_vision_outcome(self):
        item = SimpleNamespace(original_url=None)

        with self.assertRaisesRegex(VisionUnresolvedError, "WAITING_VISION"):
            ArmoredVision(api=object()).identify(item)


if __name__ == "__main__":
    unittest.main()
