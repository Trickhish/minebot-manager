# mcbot dashboard

A web dashboard to **create/start/stop**, **send chat to**, and **monitor**
`mcbot` bots live. Thin control/monitor layer over `mcbot.Client` — the
framework is unchanged.

- **Backend:** FastAPI + WebSockets (`backend/`). Each bot runs `Client.connect()`
  in its own thread; its `bot.on(...)` events are bridged onto the asyncio loop
  (`call_soon_threadsafe`) and fanned out to WebSocket subscribers.
- **Frontend:** vanilla JS SPA, no build step (`frontend/`).
- **Isolation:** thread-per-bot, single process.
- **Persistence:** none (v1). Bots live in memory and die with the process.

## Run

```bash
./run.sh                 # 127.0.0.1:21306 (behind minebot.dury.dev proxy)
HOST=0.0.0.0 ./run.sh    # bind all interfaces
```

`run.sh` puts `../framework` on `PYTHONPATH` and launches uvicorn. First-time
setup (already done once): `python3 -m venv .venv && .venv/bin/pip install -r
backend/requirements.txt`.

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/versions` | supported protocol versions |
| GET  | `/api/bots` | list bots + status |
| POST | `/api/bots` | create+start `{host, port, username, version, advertise_protocol?}` |
| GET  | `/api/bots/{id}` | status + recent event history |
| POST | `/api/bots/{id}/chat` | send `{message}` (or `/command`) |
| POST | `/api/bots/{id}/stop` | disconnect |
| DELETE | `/api/bots/{id}` | stop + remove |
| WS   | `/api/bots/{id}/ws` | replay history, then live events |

## Constraints (inherited from the framework)

- **Offline-mode servers only.** An online-mode server triggers
  `OnlineModeRequired`, surfaced as an `error` event / bot status.
- A whitelisted or otherwise restrictive server disconnects the bot at login;
  the reason is captured and shown in the log.

## Deferred to v2

Live minimap (numpy `save_map` → PNG, or bridge the stream protocol),
`move_to`/`look` controls, inventory view + `creative_give`, config
persistence, and auth. The event/queue plumbing is built to accept these
without rework.
