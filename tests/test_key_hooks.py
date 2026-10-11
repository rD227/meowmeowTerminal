import ast
import pathlib
import types
import unittest


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "main.py"


class FakeUser32:
    held = set()

    def GetAsyncKeyState(self, key):
        return 0x8000 if key in self.held else 0


class KeyHookTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_handle_enter", "_handle_pause"}]
        user32 = FakeUser32()
        self.user32 = user32
        self.enters = 0
        self.toggles = 0
        namespace = {
            "keyboard": types.SimpleNamespace(KEY_DOWN="down"),
            "ctypes": types.SimpleNamespace(windll=types.SimpleNamespace(user32=user32)),
            "_intercepted_enters": set(),
            "on_enter": lambda: setattr(self, "enters", self.enters + 1),
            "toggle": lambda: setattr(self, "toggles", self.toggles + 1),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.enter = namespace["_handle_enter"]
        self.pause = namespace["_handle_pause"]

    def test_enter_is_handled_without_hotkey_state(self):
        event = types.SimpleNamespace(event_type="down", scan_code=28)
        self.assertFalse(self.enter(event))
        self.assertEqual(self.enters, 1)
        event.event_type = "up"
        self.assertFalse(self.enter(event))

    def test_shift_enter_passes_through(self):
        self.user32.held.add(0x10)
        event = types.SimpleNamespace(event_type="down", scan_code=28)
        self.assertTrue(self.enter(event))
        event.event_type = "up"
        self.assertTrue(self.enter(event))
        self.assertEqual(self.enters, 0)

    def test_pause_uses_os_modifier_state(self):
        event = types.SimpleNamespace(event_type="down", scan_code=25)
        self.assertTrue(self.pause(event))
        self.assertEqual(self.toggles, 0)
        self.user32.held.update({0x11, 0x12})
        self.assertTrue(self.pause(event))
        self.assertEqual(self.toggles, 1)


if __name__ == "__main__":
    unittest.main()
