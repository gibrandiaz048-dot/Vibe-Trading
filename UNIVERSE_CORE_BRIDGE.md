# Universe Core client for Vibe-Trading

The optional `agent/universe_bridge.py` process connects Vibe-Trading to the
local Universe Core paper-trading backend.

## Environment

Configure these values in the Vibe runtime `.env` or process environment:

```dotenv
UNIVERSE_VIBE_TOKEN=THE_SAME_SECRET_CONFIGURED_IN_UNIVERSE_CORE
UNIVERSE_CORE_API_URL=http://127.0.0.1:8767
UNIVERSE_CORE_WS_URL=ws://127.0.0.1:8767/ws/integrations/vibe
VIBE_TRADING_API_URL=http://127.0.0.1:8000
UNIVERSE_BRIDGE_SYNC_SECONDS=15
UNIVERSE_BRIDGE_SWARM_POLL_SECONDS=20
```

If the local Vibe API is configured with `API_AUTH_KEY`, set
`VIBE_TRADING_API_TOKEN` to the same value. The shared Universe token must
match `UNIVERSE_VIBE_TOKEN` in Universe Core's local .env.

## Run

With the Vibe API and Universe Core API running, open another terminal in the
repository root:

```powershell
.\.venv313\Scripts\python.exe agent\universe_bridge.py
```

The client:
- authenticates to Universe Core before subscribing to its WebSocket events;
- fetches the initial capital/positions/trade/news snapshot and refreshes it;
- stores the last 500 events and snapshot under
  `%USERPROFILE%\.vibe-trading\universe-core\`;
- polls Vibe Swarm runs and forwards completed reports to Universe Core;
- relies on Universe Core's SQLite receipt key to ignore duplicate reports.

Read-only Vibe API mirror:
- `GET /universe/core/state`
- `GET /universe/core/events?limit=100`

The bridge never invokes a broker, never submits an order, and never changes a
Vibe broker connection. Universe Core remains the only authority for its
simulated capital, risk approvals and paper positions.
