# UtilityMind API and local stack

Flask backend for the Wheelworks utility metering pilot. It stores factory and metering master data in PostgreSQL, ingests MQTT readings into ClickHouse, computes notifications and historical baselines, renders PDF reports, and powers the dashboard's AI chat. The source of truth for project goals and limitations is [PREFERENCES.md](PREFERENCES.md). All demonstration readings and history are synthetic; the app never controls machines or utility supply.

## Local setup

Clone the three repositories side by side using these directory names (the Docker build expects them):

```bash
git clone https://github.com/huben92/utilitymind-app.git app
git clone https://github.com/huben92/utilitymind-api.git api
git clone https://github.com/huben92/utilitymind-simulator.git simulator
cd api
cp .env.example .env
# Set local database passwords in .env before the first startup.
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml exec -T dashboard python api/bootstrap_superadmin.py
cd ../simulator
python3 app.py --port 8766
```

Open the dashboard at `http://127.0.0.1:8765/` and simulator at `http://127.0.0.1:8766/`. The simulator publishes automatically to local Mosquitto on port 1884. Run the bootstrap command only on a new PostgreSQL volume; it prints the initial super-admin password once. Keep that password private. The local service key in ignored `api/data/` lets the simulator discover dashboard metering points.

The [Compose file](compose.yaml) starts Flask, PostgreSQL, ClickHouse, and Mosquitto. Its build context is the parent directory so it can compile the sibling `app/` checkout. The [Dockerfile](Dockerfile) installs Python requirements and builds Vue. Runtime secrets and keys are in ignored `data/`; databases use Docker volumes. Do not expose this demo Compose setup publicly.

## Development and docs

- [Development guide](docs/development.md): commands, configuration, tests, troubleshooting.
- [Architecture](docs/architecture.md): data flow and boundaries.
- [API reference](docs/api-reference.md): local HTTP endpoints and MQTT payloads.
- [Manufacturing audit](docs/wheel-manufacturing-audit.md): synthetic scenario assumptions.
- [Cloud deployment](docs/cloud-deployment.md): Firebase, Cloud Run, VM, cost, and operations.

The Flask entry point is `operations_app.py`. Other modules handle authentication, AI chat, chart ranges, reporting, alerts, and historical baselines. `seed_historical_data.py` adds the optional three-month synthetic demonstration history without changing configured baselines or operational maximums:

```bash
docker compose -f compose.yaml exec -T dashboard python api/seed_historical_data.py
```

The frontend is in [utilitymind-app](https://github.com/huben92/utilitymind-app); the MQTT publisher and slider UI are in [utilitymind-simulator](https://github.com/huben92/utilitymind-simulator).
