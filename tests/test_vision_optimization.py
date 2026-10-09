import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from ArmoredVision.modules.v1.shopee_api import ShopeeAffiliateAPI, ShopeeAPIError
from ArmoredVision.service import ArmoredVision


class ShopeeRetryPolicyTests(unittest.TestCase):
    def make_api(self):
        session = Mock()
        api = ShopeeAffiliateAPI(
            app_id="test-app", secret_key="test-secret", session=session
        )
        return api, session

    def test_graphql_error_is_not_retried(self):
        api, session = self.make_api()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"errors": [{"message": "invalid query"}]}
        session.post.return_value = response

        with patch.dict("os.environ", {"SHOPEE_API_MAX_RETRIES": "3"}):
            with patch("ArmoredVision.modules.v1.shopee_api.time.sleep") as sleep:
                with self.assertRaisesRegex(ShopeeAPIError, "Erro GraphQL"):
                    api._post("query { test }")

        self.assertEqual(session.post.call_count, 1)
        sleep.assert_not_called()

    def test_server_error_is_retried_and_then_succeeds(self):
        api, session = self.make_api()
        failed = requests.Response()
        failed.status_code = 503
        failed.url = "https://example.invalid/graphql"
        success = Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {"data": {"ok": True}}
        session.post.side_effect = [requests.HTTPError("503", response=failed), success]

        with patch.dict("os.environ", {"SHOPEE_API_MAX_RETRIES": "2"}):
            with patch("ArmoredVision.modules.v1.shopee_api.time.sleep") as sleep:
                result = api._post("query { test }")

        self.assertEqual(result, {"data": {"ok": True}})
        self.assertEqual(session.post.call_count, 2)
        sleep.assert_called_once()

    def test_same_session_is_reused_for_multiple_requests(self):
        api, session = self.make_api()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": {"ok": True}}
        session.post.return_value = response

        api._post("query { first }")
        api._post("query { second }")

        self.assertEqual(session.post.call_count, 2)
        self.assertIs(api._session, session)


class VisionMemoizationTests(unittest.TestCase):
    @patch("ArmoredVision.service.resolve_short_url")
    def test_reuses_only_short_lived_successful_resolution_and_product(self, resolve):
        resolve.return_value = SimpleNamespace(
            shop_id="123", item_id="456", resolved_url="https://shopee.com.br/product/123/456"
        )
        product = {
            "shopId": "123",
            "itemId": "456",
            "productName": "Produto teste",
            "offerLink": "https://affiliate.shopee.com.br/test",
        }
        api = Mock()
        api.get_exact_product.return_value = product
        api.affiliate_link_for_product.return_value = "https://affiliate.shopee.com.br/test"
        vision = ArmoredVision(api=api)
        first = SimpleNamespace(content_id="source_1", original_url="https://shope.ee/abc")
        second = SimpleNamespace(content_id="source_2", original_url="https://shope.ee/abc")

        vision.identify(first)
        vision.identify(second)

        resolve.assert_called_once_with("https://shope.ee/abc")
        api.get_exact_product.assert_called_once_with("123", "456")
        self.assertEqual(api.affiliate_link_for_product.call_count, 2)


if __name__ == "__main__":
    unittest.main()
