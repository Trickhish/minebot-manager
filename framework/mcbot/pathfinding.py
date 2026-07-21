"""Bounded block-grid pathfinding for a walking Minecraft player."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Callable


Node = tuple[int, int, int]  # block x, feet y, block z


@dataclass(frozen=True)
class PathResult:
    nodes: list[Node]
    visited: int


_BODY_HAZARDS = frozenset({
    "lava", "fire", "soul_fire", "cactus", "sweet_berry_bush",
    "powder_snow", "cobweb",
})
_SUPPORT_HAZARDS = frozenset({
    "magma_block", "campfire", "soul_campfire", "cactus",
})


def find_path(world, start: tuple[float, float, float],
              target: tuple[float, float],
              is_passable: Callable[[str | None], bool], *,
              max_nodes: int = 30_000, max_drop: int = 3,
              margin: int = 24) -> PathResult | None:
    """Find a cardinal walking path through loaded blocks using A*.

    Nodes represent the integer Y of the player's feet at a block-column
    centre. Movement can climb one full block and drop up to ``max_drop``
    blocks. The finite search margin and node budget prevent a distant or
    unloaded map click from monopolising the world worker.
    """
    sx, sy, sz = start
    tx, tz = math.floor(target[0]), math.floor(target[1])
    start_x, start_z = math.floor(sx), math.floor(sz)
    loaded_chunks = getattr(world, "chunks", None)
    if loaded_chunks is not None and (tx >> 4, tz >> 4) not in loaded_chunks:
        return None
    standable_cache: dict[Node, bool] = {}
    clear_cache: dict[Node, bool] = {}

    def standable(node: Node) -> bool:
        cached = standable_cache.get(node)
        if cached is not None:
            return cached
        x, y, z = node
        feet = world.block_name_at(x, y, z)
        head = world.block_name_at(x, y + 1, z)
        support = world.block_name_at(x, y - 1, z)
        known = feet is not None and head is not None and support is not None
        value = (
            known
            and is_passable(feet)
            and is_passable(head)
            and feet not in _BODY_HAZARDS
            and head not in _BODY_HAZARDS
            and not is_passable(support)
            and support not in _SUPPORT_HAZARDS
            and not support.endswith(("_fence", "_wall", "_fence_gate"))
        )
        standable_cache[node] = value
        return value

    def clear(node: Node) -> bool:
        """Whether the player body (feet + head) fits in a column cell.

        Used to keep a diagonal move from clipping the shared corner: both
        flanking columns must be open for the 0.6-wide box to pass through.
        """
        cached = clear_cache.get(node)
        if cached is not None:
            return cached
        x, y, z = node
        feet = world.block_name_at(x, y, z)
        head = world.block_name_at(x, y + 1, z)
        value = (
            feet is not None and head is not None
            and is_passable(feet) and is_passable(head)
            and feet not in _BODY_HAZARDS and head not in _BODY_HAZARDS
        )
        clear_cache[node] = value
        return value

    start_node = _nearest_start_node(start_x, sy, start_z, standable)
    if start_node is None:
        return None

    direct_distance = abs(tx - start_x) + abs(tz - start_z)
    search_radius = max(16, direct_distance + margin)
    node_budget = min(max_nodes, max(4_000, search_radius * search_radius * 2))

    def in_bounds(node: Node) -> bool:
        x, _, z = node
        return abs(x - start_x) + abs(z - start_z) <= search_radius

    def heuristic(node: Node) -> float:
        # Octile distance: admissible once diagonal moves are allowed.
        dx, dz = abs(tx - node[0]), abs(tz - node[2])
        return (dx + dz) - 0.5858 * min(dx, dz)

    frontier: list[tuple[float, float, Node]] = []
    heapq.heappush(frontier, (heuristic(start_node), 0.0, start_node))
    came_from: dict[Node, Node] = {}
    costs: dict[Node, float] = {start_node: 0.0}
    visited = 0

    while frontier and visited < node_budget:
        _, queued_cost, current = heapq.heappop(frontier)
        if queued_cost != costs.get(current):
            continue
        visited += 1
        if current[0] == tx and current[2] == tz:
            return PathResult(_reconstruct(came_from, current), visited)

        for neighbour, kind in _neighbours(
                current, standable, clear, max_drop):
            if not in_bounds(neighbour):
                continue
            vertical = neighbour[1] - current[1]
            if kind == "jump":
                # Distance travelled plus a penalty so walking always wins when
                # there is a floor; a real gap has no cheaper cardinal route.
                span = abs(neighbour[0] - current[0]) + abs(neighbour[2] - current[2])
                base = span + 1.0
            else:
                base = 1.4142 if kind == "diagonal" else 1.0
            step_cost = base + (0.35 if vertical > 0 else 0.12 * abs(vertical))
            new_cost = queued_cost + step_cost
            if new_cost >= costs.get(neighbour, float("inf")):
                continue
            costs[neighbour] = new_cost
            came_from[neighbour] = current
            heapq.heappush(
                frontier,
                (new_cost + heuristic(neighbour), new_cost, neighbour),
            )
    return None


def _nearest_start_node(x: int, y: float, z: int,
                        standable: Callable[[Node], bool]) -> Node | None:
    base = math.floor(y + 0.01)
    candidates = [base, base + 1, base - 1, base - 2, base + 2, base - 3]
    return next(((x, candidate, z) for candidate in candidates
                 if standable((x, candidate, z))), None)


_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_DIAGONALS = ((1, 1), (1, -1), (-1, 1), (-1, -1))

_PLAYER_HALF_WIDTH = 0.3


def _build_jump_offsets() -> dict[int, list[tuple[int, int]]]:
    """Landing offsets (dx, dz) reachable by a running/sprint jump, per rise dy.

    Covers straight *and* oblique jumps (e.g. the classic 3-1) up to the
    horizontal reach the physics can carry within a jump's air time. Cells that
    are merely adjacent (|dx|,|dz| <= 1) are excluded -- those are handled by
    the ordinary walk/diagonal moves. Upward jumps reach less far than flat or
    downward ones.
    """
    # Max Euclidean horizontal reach of a jump landing dy blocks above takeoff.
    reach = {1: 3.4, 0: 4.3, -1: 4.6, -2: 4.8, -3: 5.0}
    offsets: dict[int, list[tuple[int, int]]] = {}
    for dy, rmax in reach.items():
        seen: set[tuple[int, int]] = set()
        for dx in range(0, 5):
            for dz in range(0, 5):
                if max(dx, dz) <= 1 or math.hypot(dx, dz) > rmax:
                    continue
                for ax, az in ((dx, dz), (dz, dx)):
                    for sx in (1, -1):
                        for sz in (1, -1):
                            seen.add((sx * ax, sz * az))
        # Nearer landings first so A* expands the cheapest jump early.
        offsets[dy] = sorted(seen, key=lambda o: (o[0] * o[0] + o[1] * o[1]))
    return offsets


_JUMP_OFFSETS = _build_jump_offsets()


def _swept_columns(dx: int, dz: int) -> tuple[tuple[int, int], ...]:
    """Block columns the player box sweeps over jumping from origin to (dx, dz).

    Samples the straight takeoff->landing segment with the 0.6-wide footprint,
    excluding the takeoff and landing columns themselves. Cached per offset.
    """
    r = _PLAYER_HALF_WIDTH
    cells: set[tuple[int, int]] = set()
    samples = 4 * (abs(dx) + abs(dz)) + 4
    for i in range(samples + 1):
        t = i / samples
        px, pz = 0.5 + dx * t, 0.5 + dz * t
        for ax in (px - r, px + r):
            for az in (pz - r, pz + r):
                cells.add((math.floor(ax), math.floor(az)))
    cells.discard((0, 0))
    cells.discard((dx, dz))
    return tuple(sorted(cells))


_SWEPT_CACHE: dict[tuple[int, int], tuple[tuple[int, int], ...]] = {}


def _jump_clear(x: int, y: int, z: int, dx: int, dz: int, ny: int,
                clear: Callable[[Node], bool]) -> bool:
    """Whether the arc of a jump from (x, y, z) to (x+dx, ny, z+dz) is clear.

    Every column the player box crosses must have a clear body corridor tall
    enough for the jump apex, so the bot never launches into a ceiling or clips
    a block poking into the path.
    """
    lo, hi = min(y, ny), max(y, ny)
    cols = _SWEPT_CACHE.get((dx, dz))
    if cols is None:
        cols = _SWEPT_CACHE[(dx, dz)] = _swept_columns(dx, dz)
    for cx, cz in cols:
        # clear((c, by, c)) requires cells by and by+1 open, so scanning
        # by in [lo, hi+1] guarantees a 3-tall corridor over the gap.
        if not all(clear((x + cx, by, z + cz)) for by in range(lo, hi + 2)):
            return False
    return True


def _neighbours(node: Node, standable: Callable[[Node], bool],
                clear: Callable[[Node], bool], max_drop: int):
    """Yield ``(neighbour, kind)`` moves, kind in cardinal/diagonal/jump."""
    x, y, z = node
    # Cardinal moves: climb one block (step/jump), stay level, or drop.
    gap_adjacent = False
    for dx, dz in _CARDINALS:
        nx, nz = x + dx, z + dz
        for ny in (y + 1, y, *(y - drop for drop in range(1, max_drop + 1))):
            candidate = (nx, ny, nz)
            if standable(candidate):
                yield candidate, "cardinal"
                break
        else:
            gap_adjacent = True  # a wall or a hole borders this direction
    # Diagonal moves: only when both flanking columns are open so the wide
    # (0.6) player box slides through the corner instead of clipping it.
    for dx, dz in _DIAGONALS:
        nx, nz = x + dx, z + dz
        for ny in (y + 1, y, y - 1):
            candidate = (nx, ny, nz)
            if not standable(candidate):
                continue
            lo, hi = min(y, ny), max(y, ny)
            if all(clear((nx, fy, z)) and clear((x, fy, nz))
                   for fy in range(lo, hi + 1)):
                yield candidate, "diagonal"
            break
    # Parkour jumps: straight or oblique (e.g. the 3-1) run-up-and-jump. Upward
    # jumps need a higher landing block to exist, so they are naturally sparse
    # and always cheap to consider. Flat/downward jumps land readily on open
    # terrain, so only bother when a gap or wall actually borders this node --
    # otherwise walking is cheaper and generating them just bloats the search.
    for dy, offsets in _JUMP_OFFSETS.items():
        if dy <= 0 and not gap_adjacent:
            continue
        ny = y + dy
        for dx, dz in offsets:
            candidate = (x + dx, ny, z + dz)
            if not standable(candidate):
                continue
            if _jump_clear(x, y, z, dx, dz, ny, clear):
                yield candidate, "jump"


def _reconstruct(came_from: dict[Node, Node], current: Node) -> list[Node]:
    path = [current]
    while current in came_from:
        current = came_from[current]
        path.append(current)
    path.reverse()
    return path
