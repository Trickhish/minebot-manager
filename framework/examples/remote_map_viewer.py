"""A separate process from the bot: connects to a running ChunkStreamServer
and continuously renders a top-down PNG of the streamed world.

Run a bot with streaming enabled (e.g. add `bot.start_stream_server()` in
`@bot.on("ready")` in echo_bot.py or your own script), then in another
terminal:

    python examples/remote_map_viewer.py [host] [port] [out.png] [radius] [interval] [resourcepack]

`resourcepack` is an optional path to a .zip or extracted Minecraft resource
pack (yours, not vendored here) -- when given, block colors are sampled from
its actual textures instead of the built-in approximations.

Needs numpy (`pip install numpy`) -- the bot process itself does not.
"""

import sys
import time

from mcbot.render import encode_png, render_top_down
from mcbot.resourcepack import ResourcePack
from mcbot.stream import ChunkStreamClient


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 25566
    out = sys.argv[3] if len(sys.argv) > 3 else "remote_map.png"
    radius = int(sys.argv[4]) if len(sys.argv) > 4 else 80
    interval = float(sys.argv[5]) if len(sys.argv) > 5 else 0.5
    pack_path = sys.argv[6] if len(sys.argv) > 6 else None

    resource_pack = ResourcePack(pack_path) if pack_path else None
    if resource_pack:
        print(f"using resource pack: {pack_path}")

    client = ChunkStreamClient(host, port)
    print(f"connected to {host}:{port} (world version {client.world.block_table.version})")

    try:
        while True:
            pos = client.get_position()
            img = render_top_down(
                client.world, int(pos["x"]), int(pos["z"]), radius,
                bot_position=(pos["x"], pos["y"], pos["z"]), resource_pack=resource_pack)
            with open(out, "wb") as fh:
                fh.write(encode_png(img))
            print(f"wrote {out}  chunks={len(client.world.chunks)}  "
                  f"pos=({pos['x']:.1f}, {pos['y']:.1f}, {pos['z']:.1f})")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == "__main__":
    main()
