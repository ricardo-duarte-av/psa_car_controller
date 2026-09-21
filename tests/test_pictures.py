import unittest
from unittest.mock import MagicMock, patch

from psa_car_controller.psacc.application.psa_client import PSAClient
from psa_car_controller.psacc.model.car import Cars, Car


def img(content, status=200, ct="image/png"):
    r = MagicMock()
    r.status_code = status
    r.content = content
    r.headers = {"Content-Type": ct}
    return r


def get_client():
    client = PSAClient.__new__(PSAClient)
    client.vehicles_list = Cars([Car("myvin", "myid", "peugeot")])
    client._pictures_cache = {}
    return client


class TestPictures(unittest.TestCase):

    @patch("requests.get")
    def test_identical_views_are_de_duplicated(self, get):
        client = get_client()
        client._get_api = MagicMock(return_value={"pictures": [
            "https://v/1", "https://v/2", "https://v/dup", "https://v/dup2"]})
        # two distinct images, then the same bytes twice
        get.side_effect = [img(b"A"), img(b"B"), img(b"SAME"), img(b"SAME")]
        pics = client.get_pictures("myvin")
        self.assertEqual(3, len(pics))
        self.assertEqual([b"A", b"B", b"SAME"], [p["content"] for p in pics])

    @patch("requests.get")
    def test_the_result_is_cached(self, get):
        client = get_client()
        client._get_api = MagicMock(return_value={"pictures": ["https://v/1"]})
        get.return_value = img(b"A")
        client.get_pictures("myvin")
        client.get_pictures("myvin")
        # the images are fetched only on the first call
        self.assertEqual(1, get.call_count)

    @patch("requests.get")
    def test_a_broken_image_is_skipped_not_fatal(self, get):
        client = get_client()
        client._get_api = MagicMock(return_value={"pictures": ["https://v/1", "https://v/2"]})
        get.side_effect = [OSError("boom"), img(b"B")]
        pics = client.get_pictures("myvin")
        self.assertEqual([b"B"], [p["content"] for p in pics])

    def test_no_vehicle_gives_none(self):
        client = get_client()
        client._get_api = MagicMock(return_value=None)
        self.assertIsNone(client.get_pictures("myvin"))
        self.assertIsNone(client.get_pictures("notavin"))
