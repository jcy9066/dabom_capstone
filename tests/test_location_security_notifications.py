import unittest

from server.location_security_notifications import build_location_transition_message


class LocationSecurityNotificationTests(unittest.TestCase):
    def test_departure_message_contains_place_distance_and_coordinates(self):
        message = build_location_transition_message({
            "transition": "OUT_OF_AREA",
            "snapshot": {
                "last_location_name": "CtrlCV Lab",
                "gps": {"lat": 37.1, "lng": 127.2, "distance_m": 43.2},
            },
        })
        self.assertIn("구역 이탈", message)
        self.assertIn("CtrlCV Lab", message)
        self.assertIn("43.2 m", message)
        self.assertIn("37.100000, 127.200000", message)

    def test_return_message_is_emitted_only_for_normal_transition(self):
        normal = build_location_transition_message({
            "transition": "NORMAL",
            "snapshot": {"gps": {"matched_location_name": "CtrlCV Lab"}},
        })
        self.assertIn("정상 구역 복귀", normal)
        self.assertIsNone(build_location_transition_message({"transition": None, "snapshot": {}}))


if __name__ == "__main__":
    unittest.main()
