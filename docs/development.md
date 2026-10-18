# Development and operations guide

Clone the app, API, and simulator repositories as sibling `app/`, `api/`, and `simulator/` directories. Run Compose commands below from `api/` unless a step says otherwise. The app is a local pilot; do not publish its default Compose configuration directly to a network.

## Prerequisites and first run

- Docker Desktop with Docker Compose.
- Python 3.12+ for the standalone simulator and local tests.
- Node.js 22+ and npm for UI development. Docker builds the UI without a host Node installation.

```bash
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml exec -T dashboard python api/bootstrap_superadmin.py
(cd ../simulator && python3 app.py --port 8766)
```

The bootstrap script is deliberately one-time and prints a generated password only once. If a super admin already exists, use that account; the script will not reset it. PostgreSQL and ClickHouse are initialized by the API at startup. The dashboard needs both databases healthy before it starts.

The simulator starts publishing automatically to `127.0.0.1:1884`, TLS off, topic prefix `factory/wheel-rim`. Override broker settings with `SIMULATOR_MQTT_*` environment variables if needed. The subscriber's default topic is `factory/wheel-rim/#`. The simulator has a local SQLite cache and retains slider values between runs. Its dashboard catalog sync uses `data/simulator_service.key`, created by the API on first startup.

To stop the stack while retaining data:

```bash
docker compose -f compose.yaml down
```

PostgreSQL and ClickHouse use named Docker volumes. The simulator SQLite file lives in ignored `simulator/data/`; API encryption and session secrets live in ignored `api/data/`. Back up both volumes and data folders if you need to preserve an environment. `docker compose -f compose.yaml down -v` deletes database volumes.

## Editing and building

- Dashboard components, navigation, and chart UI: `app/src/`.
- Shared dashboard CSS and theme tokens: `app/src/style.css` and `app/src/styles/`.
- API and data logic: `api/operations_app.py` plus focused modules in `api/`.
- Simulator publisher and HTTP API: `simulator/app.py`; simulator UI: `simulator/static/`.
- MQTT broker config: `deploy/mosquitto.conf`.

```bash
cd ../app
npm ci
npm run dev
```

Vite runs at `http://127.0.0.1:5173` and proxies `/api` to the running Flask service on 8765. Run `npm run build` to create the ignored `app/dist/` when using Flask directly on the host. The dashboard container's multi-stage Dockerfile executes this build automatically. After source changes, rebuild it with `docker compose -f compose.yaml up -d --build dashboard`.

## Python tests

Use a virtual environment with the same Python dependencies as the API:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-operations.txt -r requirements-markdown.txt
PYTHONPATH=.:../simulator .venv/bin/python -m unittest discover -s tests -v
```

Tests use `unittest`; most mock databases and do not need the Compose stack. The MQTT protocol test opens a loopback socket. Test imports intentionally use the historical module names `operations_app` and `app`; the `PYTHONPATH` entries map them to their new service folders.

Useful smoke checks:

```bash
docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml ps
curl -fsS http://127.0.0.1:8765/api/health
curl -fsS http://127.0.0.1:8766/api/state
```

`/api/health` checks PostgreSQL and ClickHouse and reports MQTT subscriber state. `/api/state` checks the simulator. Dashboard data endpoints require login.

## Configuration

| Setting | Where | Default / purpose |
| --- | --- | --- |
| `POSTGRES_PASSWORD`, `CLICKHOUSE_PASSWORD` | API `.env` for Compose | Local-only defaults in Compose if omitted. Must match existing volumes. |
| `DATABASE_URL` | Dashboard container | PostgreSQL connection string, set by Compose. |
| `CLICKHOUSE_HOST`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD` | Dashboard container | ClickHouse client connection. |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC` | Dashboard container | Subscriber broker and topic filter. |
| `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_CA_CERT` | Simulator environment or simulator `.env` | Optional external-broker authentication; the bundled broker is anonymous. |
| `SENSOR_DB` | Simulator environment | Overrides simulator `data/sensors.sqlite3`. |
| `DASHBOARD_URL` | Simulator environment | Dashboard catalog-sync URL; defaults to `http://127.0.0.1:8765`. |
| `SESSION_SECRET_FILE`, `SIMULATOR_SERVICE_KEY_FILE`, `AI_KEY_FILE` | API environment | Paths to generated secrets in `data/`. |
| `DASHBOARD_DIR` | API environment | Overrides compiled `app/dist/` path. |

The simulator loads only MQTT credential variables from the simulator `.env`; its broker, interval, and publishing settings come from defaults, cached local settings, or `SIMULATOR_MQTT_*`, `SIMULATOR_INTERVAL`, and `SIMULATOR_PUBLISHING` environment variables. The dashboard's AI endpoint, model, and key are set in **App Settings**, not in `.env`. The encrypted key needs both the PostgreSQL setting and `data/ai_settings.key` to remain usable.

## Seed data and schema

On a new PostgreSQL database, startup creates the factory hierarchy, sample machines, and metering points. It also creates a ClickHouse `telemetry` table. To add the optional fixed-period demonstration history:

```bash
docker compose -f compose.yaml exec -T dashboard python api/seed_historical_data.py
```

The script creates additional utility-system machines and meters as needed, writes synthetic ClickHouse rows, and computes PostgreSQL historical profiles. It is idempotent after success. It does not change meter configuration baselines or maximums. See [the audit](wheel-manufacturing-audit.md) for scenario assumptions and [architecture](architecture.md) for data ownership.

There is no versioned migration framework yet. Startup contains additive/conditional schema changes for this prototype. Treat the current database as stateful: back it up before modifying table definitions or deploying a changed API image.

## Troubleshooting

- **Dashboard 404 at /:** build `app/dist/` locally or rebuild the dashboard image; Flask serves the compiled files.
- **No live readings:** check the simulator status pill, its broker host/port/topic prefix environment settings, then `/api/health` for MQTT connection and last message. A stale reading is not zero.
- **New dashboard meter missing in simulator:** confirm the dashboard is reachable from the simulator and both share `data/simulator_service.key`. The simulator refreshes its catalog periodically.
- **AI saved key cannot be decrypted:** restore its matching `data/ai_settings.key` or re-enter the API key in App Settings.
- **Changed database password fails:** Compose does not rotate an existing database volume's credentials; restore the previous password or rotate it in the database separately.
