import unittest
from perception.models.weapon_detector import weapon_class_ids


class WeaponClassContractTests(unittest.TestCase):
    def test_explicit_knife_and_gun(self):
        self.assertEqual({0: "KNIFE", 2: "GUN"},
                         weapon_class_ids({0: "knife", 1: "person", 2: "pistol"}))

    def test_coco_knife_only_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "knife AND gun"):
            weapon_class_ids({0: "person", 43: "knife"})

    def test_person_class_never_becomes_weapon(self):
        with self.assertRaises(ValueError):
            weapon_class_ids(["person", "chair", "dog"])

    def test_aliases_are_supported(self):
        self.assertEqual({3: "GUN", 5: "KNIFE"},
                         weapon_class_ids({3: "FireArm", 5: "knives"}))


if __name__ == "__main__":
    unittest.main()
