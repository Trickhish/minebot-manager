import unittest

from mcbot.pathfinding import find_path


def passable(name):
    return name in {"air", "water", "lava", "fire", "cobweb"}


class FakeWorld:
    def __init__(self, x_range=range(-3, 6), z_range=range(-3, 4)):
        self.blocks = {}
        for x in x_range:
            for z in z_range:
                self.blocks[(x, 0, z)] = "stone"
                for y in range(1, 6):
                    self.blocks[(x, y, z)] = "air"

    def block_name_at(self, x, y, z):
        return self.blocks.get((x, y, z))


class PathfindingTests(unittest.TestCase):
    def test_rejects_unloaded_destination_chunk_immediately(self):
        world = FakeWorld()
        world.chunks = {(0, 0): object()}

        result = find_path(
            world, (0.5, 1.0, 0.5), (32.5, 0.5), passable)

        self.assertIsNone(result)

    def test_routes_around_two_block_wall(self):
        world = FakeWorld()
        world.blocks[(1, 1, 0)] = "stone"
        world.blocks[(1, 2, 0)] = "stone"

        result = find_path(
            world, (0.5, 1.0, 0.5), (2.5, 0.5), passable)

        self.assertIsNotNone(result)
        self.assertEqual(result.nodes[0], (0, 1, 0))
        self.assertEqual(result.nodes[-1], (2, 1, 0))
        self.assertNotIn((1, 1, 0), result.nodes)
        self.assertTrue(any(z != 0 for _, _, z in result.nodes))

    def test_climbs_and_descends_single_block(self):
        world = FakeWorld()
        world.blocks[(1, 1, 0)] = "stone"

        result = find_path(
            world, (0.5, 1.0, 0.5), (2.5, 0.5), passable)

        self.assertIsNotNone(result)
        self.assertIn((1, 2, 0), result.nodes)
        self.assertEqual(result.nodes[-1], (2, 1, 0))

    def test_avoids_lava_and_other_body_hazards(self):
        world = FakeWorld()
        world.blocks[(1, 1, 0)] = "lava"

        result = find_path(
            world, (0.5, 1.0, 0.5), (2.5, 0.5), passable)

        self.assertIsNotNone(result)
        self.assertNotIn((1, 1, 0), result.nodes)

    def test_returns_none_when_loaded_route_is_sealed(self):
        world = FakeWorld(x_range=range(0, 3), z_range=range(-2, 3))
        for z in range(-2, 3):
            world.blocks[(1, 1, z)] = "stone"
            world.blocks[(1, 2, z)] = "stone"

        result = find_path(
            world, (0.5, 1.0, 0.5), (2.5, 0.5), passable)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
