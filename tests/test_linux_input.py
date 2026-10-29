import types
import unittest

try:
    from evdev import ecodes
    from utils.linux_input import LinuxKeyboardBackend, resolve_key
    _HAVE_EVDEV = True
except ImportError:
    _HAVE_EVDEV = False


@unittest.skipUnless(_HAVE_EVDEV, "python-evdev 未安装")
class LinuxKeyboardBackendTests(unittest.TestCase):
    def setUp(self):
        self.enters = 0
        self.toggles = 0
        self.backend = LinuxKeyboardBackend(
            lambda: setattr(self, "enters", self.enters + 1),
            lambda: setattr(self, "toggles", self.toggles + 1),
        )
        self.forwarded = []
        self.backend._forward = self.forwarded.append
        self.backend._trigger = lambda callback: callback()
        self.path = "/dev/input/event-test"

    def _event(self, code, value, etype=ecodes.EV_KEY):
        return types.SimpleNamespace(type=etype, code=code, value=value)

    def _press(self, code):
        self.backend._handle_key(self.path, self._event(code, 1))

    def _release(self, code):
        self.backend._handle_key(self.path, self._event(code, 0))

    def test_enter_is_swallowed_and_triggered(self):
        self._press(ecodes.KEY_ENTER)
        self._release(ecodes.KEY_ENTER)
        self.assertEqual(self.enters, 1)
        self.assertEqual(self.forwarded, [])

    def test_shift_enter_passes_through(self):
        self._press(ecodes.KEY_LEFTSHIFT)
        self._press(ecodes.KEY_ENTER)
        self._release(ecodes.KEY_ENTER)
        self._release(ecodes.KEY_LEFTSHIFT)
        self.assertEqual(self.enters, 0)
        self.assertEqual(
            [event.code for event in self.forwarded],
            [ecodes.KEY_LEFTSHIFT, ecodes.KEY_ENTER,
             ecodes.KEY_ENTER, ecodes.KEY_LEFTSHIFT],
        )

    def test_plain_p_passes_through(self):
        self._press(ecodes.KEY_P)
        self._release(ecodes.KEY_P)
        self.assertEqual(self.toggles, 0)
        self.assertEqual(len(self.forwarded), 2)

    def test_ctrl_alt_p_toggles_and_is_swallowed(self):
        self._press(ecodes.KEY_LEFTCTRL)
        self._press(ecodes.KEY_LEFTALT)
        self._press(ecodes.KEY_P)
        self._release(ecodes.KEY_P)
        self._release(ecodes.KEY_LEFTALT)
        self._release(ecodes.KEY_LEFTCTRL)
        self.assertEqual(self.toggles, 1)
        self.assertEqual(
            [event.code for event in self.forwarded],
            [ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT,
             ecodes.KEY_LEFTALT, ecodes.KEY_LEFTCTRL],
        )

    def test_enter_with_ctrl_passes_through(self):
        self._press(ecodes.KEY_LEFTCTRL)
        self._press(ecodes.KEY_ENTER)
        self.assertEqual(self.enters, 0)
        self.assertIn(ecodes.KEY_ENTER, [event.code for event in self.forwarded])

    def test_key_repeat_is_not_replayed(self):
        self._press(ecodes.KEY_A)
        self.backend._handle_key(self.path, self._event(ecodes.KEY_A, 2))
        self._release(ecodes.KEY_A)
        self.assertEqual([event.value for event in self.forwarded], [1, 0])

    def test_combo_resolution(self):
        self.assertEqual(resolve_key("ctrl"), ecodes.KEY_LEFTCTRL)
        self.assertEqual(resolve_key("a"), ecodes.KEY_A)
        self.assertEqual(resolve_key("enter"), ecodes.KEY_ENTER)
        with self.assertRaises(ValueError):
            resolve_key("not-a-key")


if __name__ == "__main__":
    unittest.main()
