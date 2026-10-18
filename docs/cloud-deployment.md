# UtilityMind pilot deployment

The hosted pilot uses Firebase Hosting for the Vue app. Hosting rewrites `/api/**`
and `/simulator/**` to the stateless `utilitymind-api` Cloud Run gateway in
`us-central1`. Direct VPC egress sends those requests to an always-on Compute
Engine VM at `10.90.0.2`. Docker Compose on that VM runs the Flask API and MQTT
subscriber, PostgreSQL, ClickHouse, Mosquitto, and the simulator. The simulator
UI requires an admin or super-admin login through the gateway. No database or
MQTT port is publicly exposed.

## Pilot resources and costs

Project: `utilitymind-wheelworks`. Region: `us-central1`. VM:
`utilitymind-data-1`, `e2-small`, 30 GB standard persistent boot disk. The VM
has a private IP in `utilitymind-vpc` and an ephemeral external IPv4 address.
Its firewall allows SSH only through Google Cloud IAP and app ports 8765/8766
only from `10.90.0.0/24`. The `UtilityMind pilot Rp500k` billing budget sends
alerts at 50%, 80%, and 100% of Rp500,000 per month. A budget is an alert, not
a hard spending cap. VM, disk, and IPv4 cost continue while the VM runs; Cloud
Run, storage, build, egress, and taxes may add to the total.

## Bootstrap on a new VM

Clone the three public repositories under one parent directory as `app`, `api`,
and `simulator`. Install Docker Engine and the Compose plugin. Then:

```bash
cd api
bash deploy/setup-cloud-env.sh
docker compose -f compose.cloud.yaml up -d --build
docker compose -f compose.cloud.yaml exec -T dashboard python api/bootstrap_superadmin.py
docker compose -f compose.cloud.yaml exec -T dashboard python api/seed_historical_data.py
```

The bootstrap command prints the initial password only once. Save it in a
password manager and change it after login. The historical readings are
synthetic and explicitly marked as such in ClickHouse.

## Gateway and frontend

Build `Dockerfile.proxy` for `linux/amd64`, push it to Artifact Registry, and
deploy Cloud Run service `utilitymind-api` with:

- public invocation (the dashboard's own login enforces API access);
- Direct VPC egress on `utilitymind-vpc` / `utilitymind-us-central1` for private ranges;
- `PRIVATE_API_URL=http://10.90.0.2:8765`;
- `PRIVATE_SIMULATOR_URL=http://10.90.0.2:8766`;
- zero minimum instances and a low maximum instance count.

In the `app` checkout, build with `VITE_SIMULATOR_URL=/simulator/ npm run
build:firebase`, then run `firebase deploy --only hosting`. The Firebase
project and Hosting rewrite are defined in `app/.firebaserc` and
`app/firebase.json`.

## Operations and limitations

Check `/api/health` through the hosted URL for PostgreSQL, ClickHouse, and
MQTT subscriber state. Back up the VM's Docker volumes and ignored `api/data/`
directory together before replacing the VM; this pilot has no managed database
failover. AI settings and the encrypted Gemini key reside in PostgreSQL and
`api/data/ai_settings.key`. Configure the key through App Settings after first
login. Avoid embedding it in source or build arguments.

The VM is intentionally small to fit the pilot spending target. Three-month
synthetic history insertion can take time, and concurrent report/AI queries
may need a larger VM. Monitor memory, disk, Cloud Run request volume, and the
billing alerts. The application recommends action but never changes utility
supply or machine state automatically.
