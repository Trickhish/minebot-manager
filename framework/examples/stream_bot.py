"""Bot that connects to mc.dury.dev and starts the stream server on port 25566.

Run this first, then in a second terminal run stream_viewer.py.

    python -m examples.stream_bot [host] [version] [username] [stream_port]

Defaults:
    host         mc.dury.dev
    version      26.2
    username     StreamBot
    stream_port  25566
"""

import sys

from mcbot.client import Client, OnlineModeRequired


def main():
    host        = sys.argv[1] if len(sys.argv) > 1 else "mc.dury.dev"
    version     = sys.argv[2] if len(sys.argv) > 2 else "26.2"
    username    = sys.argv[3] if len(sys.argv) > 3 else "StreamBot"
    stream_port = int(sys.argv[4]) if len(sys.argv) > 4 else 25566

    bot = Client(host, username=username, version=version)

    @bot.on("ready")
    def _ready():
        print(f"[bot] in game as {username} on {host}")
        bot.chat("hello! streaming map on port " + str(stream_port))

    @bot.on("spawn")
    def _spawn(position):
        print(f"[bot] spawned at {position}")
        server = bot.start_stream_server(port=stream_port)
        print(f"[bot] stream server listening on port {server.port}")
        print(f"[bot] → now run:  python examples/stream_viewer.py 127.0.0.1 {server.port}")

    @bot.on("chat")
    def _chat(name, params, raw):
        print(f"[chat/{name}] {params}")

    @bot.on("disconnect")
    def _bye(reason):
        print(f"[bot] disconnected: {reason}")

    try:
        bot.connect()
    except OnlineModeRequired as exc:
        print(f"[bot] {exc}")
    except KeyboardInterrupt:
        bot.stop()


if __name__ == "__main__":
    main()
