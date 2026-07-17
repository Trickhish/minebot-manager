import threading
import unittest

from mcbot.client import Client, _JUMP_VELOCITY


def control_client():
    client = Client.__new__(Client)
    client.position = {
        "x": 0.0, "y": 1.0, "z": 0.0,
        "yaw": 0.0, "pitch": 0.0, "on_ground": True,
    }
    client.walk_speed = 4.317
    client._position_lock = threading.Lock()
    client._control_until = 0.0
    client._control_velocity_y = 0.0
    client._control_jump_held = False
    client._control_air_jumps = 0
    client._box_blocked = lambda *_: False
    client._resolve_horizontal = lambda x, _y, z, dx, dz, _ground: (x + dx, z + dz)
    client._ground_level_at = lambda *_: 1.0
    client._send_position_update = lambda: None
    client.emit = lambda *_: None
    return client


class ControlPhysicsTests(unittest.TestCase):
    def test_super_speed_doubles_horizontal_distance(self):
        normal = control_client()
        fast = control_client()

        normal.control_step(1, 0, False, False, 0, 0, 0.05)
        fast.control_step(1, 0, False, False, 0, 0, 0.05,
                          super_speed=True)

        self.assertAlmostEqual(fast.position["z"], normal.position["z"] * 2)

    def test_double_jump_allows_exactly_one_airborne_impulse(self):
        client = control_client()
        client.control_step(0, 0, True, False, 0, 0, 0.05,
                            double_jump=True)
        client.control_step(0, 0, False, False, 0, 0, 0.01,
                            double_jump=True)
        client.control_step(0, 0, True, False, 0, 0, 0.01,
                            double_jump=True)

        self.assertEqual(client._control_air_jumps, 1)
        self.assertLess(client._control_velocity_y, _JUMP_VELOCITY)
        velocity_after_second_jump = client._control_velocity_y

        client.control_step(0, 0, False, False, 0, 0, 0.01,
                            double_jump=True)
        client.control_step(0, 0, True, False, 0, 0, 0.01,
                            double_jump=True)

        self.assertEqual(client._control_air_jumps, 1)
        self.assertLess(client._control_velocity_y, velocity_after_second_jump)


if __name__ == "__main__":
    unittest.main()
