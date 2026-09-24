import unittest
from unittest.mock import MagicMock

from psa_car_controller.psa.connected_car_api.rest import ApiException
from psa_car_controller.psa.oauth import Oauth2PSACCApiConfig, OauthAPIClient
from tests.test_psa_probe import get_client


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


class TestPSAClientTokenRefresh(unittest.TestCase):
    def test_the_retry_sends_the_refreshed_token(self):
        # 24/09/2026 15:56: the retry went out with the revoked token, "Invalid client id or secret"
        client = get_client()
        client.api_config = Oauth2PSACCApiConfig()
        client.api_config.set_refresh_callback(client._refresh_api_token)  # pylint: disable=protected-access
        client.manager.access_token = "old"

        def refresh():
            client.manager.access_token = "new"
            return True
        client.manager.refresh_token_now = refresh
        api_client = client.api().api_client
        sent = []

        def call(*args, **kwargs):  # pylint: disable=unused-argument
            sent.append(api_client.configuration.access_token)
            if len(sent) == 1:
                raise unauthorized()
            return "vehicles"
        api_client._ApiClient__call_api = call  # pylint: disable=protected-access

        self.assertEqual("vehicles", api_client.call_api("/user/vehicles", "GET"))
        self.assertEqual(["old", "new"], sent)

    def test_the_retry_is_the_same_request_with_the_new_token(self):
        # 24/09/2026 17:31, with 0.1.26: the retry carried the new token but the client_id twice,
        # the generated client appending it to the caller's query list, and psa refused it
        client = get_client()
        client.api_config = Oauth2PSACCApiConfig()
        client.api_config.api_key['client_id'] = "cid"
        client.api_config.api_key['x-introspect-realm'] = "realm"
        client.api_config.set_refresh_callback(client._refresh_api_token)  # pylint: disable=protected-access
        client.manager.access_token = "old"

        def refresh():
            client.manager.access_token = "new"
            return True
        client.manager.refresh_token_now = refresh
        api = client.api()
        sent = []

        def get(url, headers=None, query_params=None, **kwargs):  # pylint: disable=unused-argument
            sent.append((headers["Authorization"], headers["x-introspect-realm"], list(query_params)))
            if len(sent) == 1:
                raise unauthorized()
            raise StopIteration  # the second request is what's checked, not its answer
        api.api_client.rest_client.GET = get

        with self.assertRaises(StopIteration):
            api.get_vehicle_status("vehicle")
        self.assertEqual([("Bearer old", "realm", [("client_id", "cid")]),
                          ("Bearer new", "realm", [("client_id", "cid")])], sent)

    def test_a_failed_refresh_keeps_the_token(self):
        client = get_client()
        client.api_config = Oauth2PSACCApiConfig()
        client.api_config.access_token = "old"
        client.manager.refresh_token_now.return_value = False
        self.assertFalse(client._refresh_api_token())  # pylint: disable=protected-access
        self.assertEqual("old", client.api_config.access_token)


if __name__ == '__main__':
    unittest.main()
