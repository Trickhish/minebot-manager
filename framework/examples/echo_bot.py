"""A tiny example bot: logs in, prints chat, and echoes greetings.

    python -m examples.echo_bot mc.dury.dev 26.2 MyBot

Works against any offline-mode server whose version has vendored data
(see `python -c "from mcbot.protocol import available_versions as a; print(a())"`).
"""

import sys

from mcbot.client import Client, OnlineModeRequired


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "mc.dury.dev"
    version = sys.argv[2] if len(sys.argv) > 2 else "26.2"
    username = sys.argv[3] if len(sys.argv) > 3 else "EchoBot"

    client = Client(host, username=username, version=version)

    @client.on("ready")
    def _ready():
        print(f"[bot] in game as {username}")
        client.chat("hello! i am a mcbot. say 'ping' and i'll reply.")

    @client.on("chat")
    def _chat(name, params, raw):
        print(f"[chat/{name}] {params}")
        # crude: react to any decoded text containing 'ping'
        if params and "ping" in str(params).lower():
            client.chat("pong!")

    @client.on("disconnect")
    def _bye(reason):
        print(f"[bot] disconnected: {reason}")

    try:
        client.connect()  # blocks until the connection ends
    except OnlineModeRequired as exc:
        print(f"[bot] {exc}")
    except KeyboardInterrupt:
        client.stop()


if __name__ == "__main__":
    main()
