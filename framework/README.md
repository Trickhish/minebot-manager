# mcbot

A lightweight, **data-driven** Python framework for talking to Minecraft: Java
Edition servers at the protocol level. Like [mineflayer] / [SoulFire], it only
emulates the *network* client — no rendering, no game logic you don't ask for —
so it stays small while still speaking many versions.

The trick is that packet definitions aren't hand-coded. A [protodef]
interpreter reads the declarative schemas that ship with [minecraft-data], so
one engine speaks every version whose schema is vendored. Adding a version is
(mostly) dropping in a JSON file.

[mineflayer]: https://github.com/PrismarineJS/mineflayer
[SoulFire]: https://github.com/soulfiremc-com/SoulFire
[protodef]: https://github.com/ProtoDef-io/ProtoDef
[minecraft-data]: https://github.com/PrismarineJS/minecraft-data

## Status

Working today:

- Server List Ping (status query)
- Offline-mode login → Configuration → Play (version-aware state machine)
- zlib compression; AES/CFB8 encryption hook (online-mode auth not yet wired)
- Automatic keep-alive; event dispatch; chat send/receive (incl. unsigned chat
  and commands on 1.19+)
- Movement: self-position tracking from server sync, teleport-confirm,
  `bot.look(yaw, pitch)`, `bot.move_to(x, y, z)` (straight-line, no
  collision/gravity), idle-heartbeat position updates
- World: chunk parsing (paletted containers, bit-unpacking) into an in-memory
  block-state grid, live-updated via `block_change`/`multi_block_change`;
  `bot.block_at(x, y, z)` and `bot.nearby_blocks(radius)`. 1.18+ only (see
  Architecture) — state ids resolved to names via a vendored per-version table.
- Inventory: full read-side tracking (`bot.inventory.items_in()`,
  `.held_item()`) across all three historical Slot wire shapes;
  `bot.select_hotbar(slot)`; `bot.creative_give(slot, item, count)` for
  creative-mode item placement. Survival-mode slot clicking
  (`window_click`/drag-and-drop) is **not implemented** — see Inventory notes.
- Rendering: `bot.render_map(radius)` / `bot.save_map(path, radius)` /
  `bot.start_live_map(path, interval, radius)` — a top-down PNG minimap of
  loaded chunks, colored by block type with vanilla-style height shading and a
  bot-position marker. numpy-vectorized with a per-chunk cache (only
  re-scans chunks that actually changed), PNG written via stdlib
  `zlib`/`struct` (no Pillow). Optional: needs `pip install numpy`; the rest
  of the framework has no dependency on it. See Rendering notes for the real
  framerate numbers and what's *not* rendered (no lighting, entities, or true
  first-person perspective — a raycast or GPU-backed renderer were both
  considered and explicitly deferred; see Rendering notes).
- Streaming: `bot.start_stream_server(host, port, radius, tick_interval)` runs
  a small private protocol (not Minecraft's) that pushes live position +
  a radius-bounded window of chunk/block-change data to any number of
  connected viewers, so rendering can happen in a **wholly separate process**
  and never costs the bot's own event loop anything beyond a background-thread
  enqueue. `ChunkStreamClient` (in `stream.py`) is the other end — see
  `examples/remote_map_viewer.py` for a complete separate render process built
  on it (reuses `render_top_down`/`encode_png` unchanged). See Streaming notes.
- Texture packs: pass a `ResourcePack` (a .zip or extracted Minecraft
  resource pack **you** legally own — nothing from it is vendored, only read
  locally at render time) to `render_map`/`save_map`/`start_live_map`/
  `render_top_down`, and block colors are sampled from its real textures
  instead of the built-in approximate table. Needs a hand-written PNG decoder
  (`png.py`, stdlib-only) since we don't depend on Pillow. See Resource pack
  notes.
- Verified live: **1.21.11** and **26.2 (protocol 776)** — login, 40+
  keep-alives, a chat message the server broadcast, a confirmed `move_to()`
  walk with no anti-cheat correction, `nearby_blocks()` correctly reading
  sand/sandstone terrain around spawn from 328 loaded chunk columns,
  `window_items`/`set_slot`/`select_hotbar` round-tripping against the real
  (empty) player inventory with no decode errors or disconnects,
  `render_map()` producing a correct 161x161 PNG whose color histogram
  matches the known sand/water beach terrain at spawn, the streaming protocol
  reconstructing an identical map **in a genuinely separate OS process** (bot
  on system Python without numpy, viewer on a venv with it) with live position
  tracking through an actual `move_to()` walk, and a synthetic resource pack
  correctly overriding sand/water colors end-to-end through that same
  streamed pipeline.

Not done yet: Microsoft/online-mode auth, survival-mode inventory clicking,
movement physics (collision, gravity, jumping), pathfinding, biome/light data,
block entities, entity rendering, first-person perspective rendering.

## Install

```bash
pip install cryptography    # only needed once online-mode encryption is used
pip install numpy           # only needed for bot.render_map() / save_map() / render_top_down()
```

`bot.start_stream_server()` and `ResourcePack` need no new dependency at all
— only the actual `render_top_down()` call (wherever it happens: in-process
or in a separate `ChunkStreamClient`-based script) needs numpy.

Pure standard library otherwise. No runtime dependency on `minecraft-data` —
the protocol schemas are vendored under `mcbot/data/`. (This repo's own dev/test
environment is a local `.venv` with numpy installed, since system `pip` here is
externally managed — see `python3 -m venv .venv`.)

## Quick start

```python
from mcbot.client import Client

bot = Client("mc.dury.dev", username="MyBot", version="26.2")

@bot.on("ready")                     # reached the Play state
def _():
    bot.chat("hello world")

@bot.on("chat")
def _(name, params, raw):
    print(name, params)

@bot.on("spawn")
def _(position):
    print(bot.nearby_blocks(radius=5))          # [(x, y, z, "sand"), ...]
    print(bot.inventory.items_in())             # [(slot, {"name": ..., "count": ...}), ...]
    bot.select_hotbar(0)
    bot.creative_give(36, "diamond_sword", 1)    # creative mode only
    bot.start_live_map("map.png", interval=1.0, radius=80)  # requires numpy
    bot.start_stream_server(port=25566)  # let a separate process render instead

bot.connect()                        # blocks, pumping packets
```

Then, as a genuinely separate process (no numpy needed on the bot's side):

```bash
python examples/remote_map_viewer.py 127.0.0.1 25566 map.png 80 1.0 my_resource_pack.zip
```

Or run the example:

```bash
python -m examples.echo_bot mc.dury.dev 26.2 MyBot
```

> Offline mode only for now: the target server must run with
> `online-mode=false`. Against an online-mode server the client raises
> `OnlineModeRequired` (it detects the `encryption_begin` packet).

## Events

`bot.on(event, fn)`:

| event | fires | callback |
|-------|-------|----------|
| `state` | protocol state changes | `(state)` |
| `login` | login success | `(params)` |
| `ready` | entered Play | `()` |
| `chat`  | any chat/system message | `(name, params, raw)` |
| `spawn` | first position sync (in-game and controllable) | `(position)` |
| `move`  | later server-driven position sync (teleport/knockback) | `(position)` |
| `block_change` / `multi_block_change` | a loaded block changed | `(name, params, raw)` |
| `window_items` / `set_slot` | inventory sync (also updates `bot.inventory`) | `(name, params, raw)` |
| `disconnect` | kicked / closed | `(reason)` |
| `packet` | every Play packet | `(name, params, raw)` |
| `<packet_name>` | that specific packet | `(name, params, raw)` |

Params are decoded lazily — only when something is listening for that packet —
so a bot that ignores movement doesn't pay to parse thousands of them.

## Architecture

```
mcbot/
  buffer.py      wire primitives (VarInt, strings, floats, UUID, ...)
  nbt.py         NBT — classic (named root) and 1.20.2+ network (nameless)
  types.py       protodef interpreter: container/array/switch/option/bitfield/…
  protocol.py    schema registry; protocol-number lookup; packet codecs
  connection.py  socket + length framing + zlib compression + AES hook
  client.py      state machine, login, keep-alive, movement, world, events
  chunk.py       hand-parser for chunk section bytes (paletted containers)
  blocks.py      block-state-id -> name lookup (vendored per-version table)
  world.py       in-memory chunk/block store + nearby_blocks query
  items.py       item-id -> name lookup (vendored per-version table)
  inventory.py   Slot decode/encode across 3 historical wire shapes + state
  colors.py      approximate block -> RGB (curated + dye-name + hash fallback)
  png.py         stdlib-only PNG encoder + decoder (RGB/RGBA, all 5 filters)
  render.py      numpy-vectorized top-down minimap
  resourcepack.py  real-texture colors from a user-supplied resource pack
  stream.py      push world/position data to a separate render process
  data/pc/       vendored schemas + block/item tables: 1.8, 1.18.2, 1.21.11, 26.2
tools/
  build_version_from_viaversion.py   generate a schema for a version newer
                                     than minecraft-data (see below)
  build_block_table.py               vendor a compact block-state-id table
  build_item_table.py                vendor a compact item-id table
examples/
  echo_bot.py
  remote_map_viewer.py   a separate render process driven by stream.py
```

### World / chunk parsing notes

- Only the modern (1.18+, no cross-long bit packing) chunk section format is
  implemented; **1.8 has no world tracking** (`bot.world` is `None`).
- The chunk section byte layout isn't in minecraft-data's JSON (it's an opaque
  buffer there) and changed *again* at protocol 770 (1.21.5), which dropped
  the explicit per-container data-array-length field in favor of computing it
  from `bitsPerEntry`. `chunk.py` handles both eras; if you add a version,
  re-derive this by capturing a real `chunkData` buffer and checking it parses
  to exactly zero leftover bytes (brute-force a few framing hypotheses against
  that captured buffer, the way this project's own byte layout was originally
  worked out) rather than trusting memory of the spec.
- Block *properties* (facing, waterlogged, ...) aren't resolved — only which
  block type a state id belongs to. World height is assumed to be vanilla
  default (`min_y=-64`); custom dimensions with a different height will
  resolve the wrong Y.

### Inventory notes

- The item "Slot" wire format changed twice across supported versions: 1.8's
  `blockId=-1`-sentinel format, 1.9-1.20.4's `present`-bool format, and
  1.20.5+'s `itemCount=0`-sentinel + data-components format. `inventory.py`
  normalizes all three on read and picks the right one to encode based on the
  target version's actual schema (introspected at runtime, not hardcoded per
  version) — so `creative_give` works unmodified across all four vendored
  versions.
- **Survival-mode slot clicking is not implemented.** Since 1.21.2,
  `window_click` reports predicted changes via `HashedSlot` — a hash of each
  item's components, not the item itself — as an anti-cheat integrity check.
  Replicating that hash algorithm is real, separate work; `bot.click_slot()`
  raises `NotImplementedError` rather than pretending to support it.
  `creative_give` sidesteps this entirely (it uses `set_creative_slot`, which
  has no hash requirement) but only takes effect in creative mode.
- Item *data components* (enchantments, custom names, attribute modifiers,
  ...) decode structurally via the existing protodef engine — nothing
  component-specific was hand-written — but `bot.inventory` only surfaces
  `name`/`count`; reach into `item["raw"]["components"]` for the rest.

### Rendering notes

- **Why top-down, not first-person.** A true first-person view was considered
  and explicitly ruled out for now: pure-Python/numpy raycasting won't hit
  real framerates at usable resolution without prototyping risk, and a
  GPU-backed renderer (e.g. `moderngl`) is real-time but pulls in an OpenGL
  context and likely a headless display setup — a much bigger step away from
  this project's "lightweight, protocol-only" design than a 2D map. A
  top-down minimap uses data we already track (loaded chunks), stays fast in
  pure numpy, and needs no new runtime surface beyond one optional dependency.
- **Colors are approximate by default, real if you supply a resource pack.**
  Minecraft doesn't publish a block-color table, and we don't vendor the
  game's (copyrighted) textures. Without a pack, `colors.py` curates common
  blocks by name, resolves anything named after one of the 16 standard dye
  colors (`red_wool`, `light_blue_concrete`, ...) automatically, and falls
  back to a deterministic per-name hash color for anything else — visually
  distinct, but arbitrary. With a pack (see Resource pack notes), real
  texture colors are used instead.
- **Measured performance** (400 loaded chunks, radius=64, this repo's dev
  machine): a cold render (every chunk freshly dirty) costs **~200ms**
  one-time. After that, `world.dirty_chunks` means only *changed* chunks get
  rescanned — steady-state re-renders while only a few chunks change per frame
  (the realistic case: a bot walking, or a static world) measured
  **300-900+ fps**. This is the actual "decent framerate" answer: not from
  raw per-frame speed, but from not repeating work that didn't change.
- Not rendered: light levels, entities (players/mobs), or true 3D perspective
  — this is a data map, not a screenshot.

### Streaming notes

- **Why a private protocol instead of reusing Minecraft's.** The bot already
  has a `World`; the goal is only to get a *read-only, radius-bounded* view of
  it into another process cheaply. Reusing the full MC protocol (versioned
  packet schemas, login handshake, compression thresholds) would solve a
  problem we don't have. The stream protocol is deliberately tiny: 5 message
  types, built on the same `Buffer` primitives as everything else here.
- **Why the bot never blocks on it.** `ChunkStreamServer` runs its own
  accept/tick/sender threads; the bot's packet-pump thread and any event
  handler on it only ever does a `queue.put()` — a slow or stalled viewer
  socket can back up that queue without ever blocking a keep-alive response.
- **The window, not the whole world.** The server doesn't mirror everything
  `bot.world` has loaded — it pushes only chunks within `radius` of the bot's
  *current* position, sending `CHUNK_UNLOAD` for ones that fall out of range
  as the bot moves. This is deliberately smaller than the server's own view
  distance so a stationary viewer's memory doesn't grow unbounded.
- **A real bug this caught:** the first implementation only broadcast
  position updates *on change*, to whichever clients happened to be connected
  at that moment. A viewer connecting after the bot had already stopped
  moving got no baseline position at all. Fixed by sending an immediate
  position snapshot to each client right at connect time, independent of the
  change-broadcast path — worth noting because it only showed up once tested
  as actual separate processes with realistic timing, not in an in-process
  mock.
- Multiple viewers can connect to one `ChunkStreamServer` simultaneously
  (each gets its own catch-up burst and independent chunk window).

### Resource pack notes

- **The point:** avoid shipping or touching Mojang's copyrighted textures at
  all. `ResourcePack` reads a `.zip` or extracted pack **you** already have
  (from your own game install, or a Creative Commons pack) directly from
  disk/zip at render time — nothing is copied, cached to this repo, or
  redistributed, the same way the vanilla client itself uses resource packs.
- Needs a PNG *decoder*, which `png.py` provides (`encode_png` only wrote
  files; texture files can use any of PNG's 5 scanline filter types, so
  reading them needed the real thing). Verified against ImageMagick-encoded
  files pixel-for-pixel, including alpha, not just against our own encoder's
  round-trip.
- Per block, the texture's face most useful for a *top-down* view is
  preferred (e.g. `grass_block_top.png` over the side texture), and its
  **average color** is used (alpha-weighted — fully transparent pixels are
  excluded so a mostly-transparent leaves/glass texture isn't washed out).
  This is a summary color for a flat map, not the texture itself.
- Known simplification: animated textures (water, lava) are stored as
  multiple frames stacked in one PNG file; averaging the whole file blends
  every frame together rather than picking frame 0. Still a reasonable
  representative color in practice (the frames are minor variations), just
  not pixel-exact to any single frame.
- `ResourcePack` itself needs no new dependency (stdlib `zipfile` + `png.py`)
  — only combining it with `render_top_down` needs numpy, same as before.

## Supported versions

`Client` status-pings the server with the newest local protocol, then selects
the exact schema returned by the server. ViaVersion-style proxies can negotiate
that probe down to another supported protocol. Callers normally do not pass a
version:

```python
bot = Client("mc.example.org", username="Bot")
```

Protocol requirements are capability-driven: the client checks whether the
selected schema contains configuration, player-loaded, chat, and other packets
instead of branching on release names. Version-specific adapters should only be
added where the same capability has genuinely different behavior.

```python
from mcbot.protocol import available_versions
print(available_versions())   # ['1.18.2', '1.21.11', '26.2', '1.8']
```

Add a version that minecraft-data already covers by copying its
`protocol.json` into `mcbot/data/pc/<version>/`, then vendor its block table
(needed for `block_at`/`nearby_blocks`) with:

```bash
python tools/build_block_table.py <version> <mcdata_blocks_subpath>
```

### Versions newer than minecraft-data

minecraft-data lags the newest release by a version or two. `26.2`
(protocol 776) is handled with a hybrid: **field layouts from 1.21.11** +
**packet ID ordering scraped from ViaVersion's packet enums**. The generator
reconciles ViaVersion's vanilla packet names with minecraft-data's legacy names
automatically (by aligning the two lists for a shared base version), so no alias
table is hand-maintained:

```bash
python tools/build_version_from_viaversion.py 26.2
```

This works because a minor bump usually only reorders/inserts packets while
keeping the layouts of the ones you actually use (keep_alive, chat, movement).
Explicit schema selection remains available for protocol development and
diagnostics:

```python
Client(host, version="26.2")                       # 26.2 already = 776
Client(host, version="1.21.11", advertise_protocol=776)   # decode w/ 774 layouts
```
