import unittest

from utils.foreground import _find_focused, _names


class ForegroundTests(unittest.TestCase):
    def test_names_are_lowercase(self):
        self.assertEqual(_names("QQ", None), {"qq"})

    def test_names_empty_when_nothing_known(self):
        self.assertEqual(_names("", None), set())

    def test_find_focused_nested_node(self):
        tree = {
            "focused": False,
            "nodes": [
                {"focused": False, "nodes": [], "floating_nodes": []},
                {"focused": True, "pid": 123, "nodes": [], "floating_nodes": []},
            ],
            "floating_nodes": [],
        }
        self.assertEqual(_find_focused(tree)["pid"], 123)

    def test_find_focused_returns_none(self):
        tree = {"focused": False, "nodes": [], "floating_nodes": []}
        self.assertIsNone(_find_focused(tree))


if __name__ == "__main__":
    unittest.main()
