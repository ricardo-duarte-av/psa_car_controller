import unittest
from unittest.mock import MagicMock

from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psa.oauth import Oauth2PSACCApiConfig, OauthAPIClient


def unauthorized():
    return ApiException(status=401, reason="Unauthorized")


class TestOauthAPIClient(unittest.TestCase):
    def setUp(self):
        self.config = Oauth2PSACCApiConfig()
        self.refresh = MagicMock(return_value=True)
        self.config.set_refresh_callback(self.refresh)
        self.client = OauthAPIClient(self.config)
        self.api_call = MagicMock()
        self.client._ApiClient__call_api = self.api_call  # pylint: disable=protected-access

    def call(self):
        return self.client.call_api("/user/vehicles", "GET")

    def test_an_expired_token_is_refreshed_and_the_call_made_again(self):
        # the hourly expiry: the first call after it used to come back None (24/09/2026 04:13)
        self.api_call.side_effect = [unauthorized(), "vehicles"]
        self.assertEqual("vehicles", self.call())
        self.refresh.assert_called_once()
        self.assertEqual(2, self.api_call.call_count)

    def test_a_token_refused_twice_is_an_error(self):
        self.api_call.side_effect = [unauthorized(), unauthorized()]
        with self.assertRaises(ApiException):
            self.call()

    def test_a_failed_refresh_is_an_error(self):
        self.refresh.return_value = False
        self.api_call.side_effect = [unauthorized(), "vehicles"]
        with self.assertRaises(ApiException):
            self.call()
        self.assertEqual(1, self.api_call.call_count)

    def test_other_errors_are_not_retried(self):
        self.api_call.side_effect = [ApiException(status=500, reason="Internal Server Error")]
        with self.assertRaises(ApiException):
            self.call()
        self.refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
