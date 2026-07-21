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

        for neighbour, diagonal in _neighbours(
                current, standable, clear, max_drop):
            if not in_bounds(neighbour):
                continue
            vertical = neighbour[1] - current[1]
            base = 1.4142 if diagonal else 1.0
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


def _neighbours(node: Node, standable: Callable[[Node], bool],
                clear: Callable[[Node], bool], max_drop: int):
    """Yield ``(neighbour, is_diagonal)`` reachable in one walking move."""
    x, y, z = node
    # Cardinal moves: climb one block (jump), stay level, or drop.
    for dx, dz in _CARDINALS:
        nx, nz = x + dx, z + dz
        for ny in (y + 1, y, *(y - drop for drop in range(1, max_drop + 1))):
            candidate = (nx, ny, nz)
            if standable(candidate):
                yield candidate, False
                break
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
                yield candidate, True
            break


def _reconstruct(came_from: dict[Node, Node], current: Node) -> list[Node]:
    path = [current]
    while current in came_from:
        current = came_from[current]
        path.append(current)
    path.reverse()
    return path
