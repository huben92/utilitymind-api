# HTTP API reference

The Flask dashboard listens at `http://127.0.0.1:8765`; the simulator listens at `http://127.0.0.1:8766`. Both return JSON for API routes. This is an implementation reference for the local pilot, not a stable public API. The frontend uses same-origin cookies and the CSRF token returned by login. See `api/operations_app.py` and `simulator/app.py` for the authoritative request validation.

## Authentication

| Method | Dashboard path | Purpose |
| --- | --- | --- |
| POST | `/api/auth/login` | JSON `{ "username": "...", "password": "..." }`; establishes session and returns `user` and `csrf_token`. |
| GET | `/api/auth/me` | Returns current user and CSRF token, or a null user. |
| POST | `/api/auth/logout` | Clears the session. |
| POST | `/api/auth/change-password` | Changes the signed-in user's password. |
| GET, POST, PATCH, DELETE | `/api/users` and `/api/users/{id}` | Super-admin user management. |

Every dashboard data endpoint except `/api/health`, login, and session lookup requires authentication. Mutating methods require the `X-CSRF-Token` header from the current session. Only super admins manage users and AI settings; viewers cannot change master data or notification state. The simulator's service token has access only to `GET /api/simulator-catalog` and the enabled-only `PUT /api/master/sensors` payload.

`factory_admin` users require a `factory_id`. Dashboard catalog, alerts,
reports, category and machine charts, and master-data operations are scoped
to that factory, including direct ID requests. AI chat and simulator controls
are unavailable to factory admins because those interfaces are global.

## Dashboard reads

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/health` | PostgreSQL and ClickHouse checks plus MQTT subscriber status. |
| GET | `/api/dashboard` | Master hierarchy, metering, fresh readings, and overview data. |
| GET | `/api/category/{category}` | One utility's live summary, machine share, and feed. |
| GET | `/api/overview/trends` | Overview time-series data. |
| GET | `/api/chart-trends` | Category time-series for the selected date range. |
| GET | `/api/machine/{id}/trend` | Aggregate machine history. |
| GET | `/api/machine/{id}/metering-trends` | Separate time series for a machine's meters. |
| GET | `/api/notifications` | Current and historical advisory notification episodes. |
| GET | `/api/reports/daily`, `/api/reports/monthly` | Calculated period report as JSON. |
| GET | `/api/reports/{kind}/pdf` | Same report rendered as a downloadable PDF. |
| GET | `/api/simulator-catalog` | Meter configuration synchronized to the simulator. |

Chart APIs support named ranges such as last 60 minutes, today, this week/month, last month, last three months, and inclusive custom dates. Dates are interpreted using the factory's `Asia/Jakarta` timezone, then converted to UTC for ClickHouse queries. Inspect `api/chart_ranges.py` for accepted parameter values and bucket widths. A missing time bucket represents no observation, not a value of zero.

## Dashboard mutations and AI

| Method | Path | Purpose |
| --- | --- | --- |
| POST, PUT, DELETE | `/api/master/{table}` | Manage `factories`, `locations`, `production_lines`, `machines`, or `sensors`; machine edits may include left/right flow links. |
| POST | `/api/notifications/{id}/action` | Resolve with a note, ignore, or confirm a machine off/on question. |
| GET, POST | `/api/ai/settings` | Super-admin AI endpoint, model, and encrypted key configuration. |
| POST | `/api/ai/settings/test` | Super-admin test of saved AI settings. |
| POST | `/api/ai/chat` | Ask a factory utility question with `question` and `conversation_id`; the server saves the turn and builds model context from that user's saved conversation. |
| GET | `/api/ai/conversations` | List the signed-in user's saved chats, newest first. |
| GET, DELETE | `/api/ai/conversations/{id}` | Reopen a saved chat with answers, visuals, and evidence, or delete it. |

Legacy work-plan endpoints and tables remain in the API to preserve existing records, but Work Plans are no longer offered in the dashboard and do not drive active notices.

## Simulator API

| Method | Simulator path | Purpose |
| --- | --- | --- |
| GET | `/api/state` | Current local meters, broker settings, publish status, and recent publishes. |
| POST | `/api/settings` | Save broker host, port, TLS, topic prefix, interval, or publisher switch. |
| PATCH | `/api/sensors/{id}` | Set `current_value` or `active`; rejects changes to baseline or variance. |
| POST | `/api/publish-now` | Queue an immediate MQTT batch. |
| POST | `/api/sensors` | Create a simulator-only local meter. |

The simulator process is intended for loopback use and does not provide dashboard-style user authentication. Do not expose port 8766 on a shared network. A dashboard-created meter is synchronized to the simulator; a simulator-only meter is not automatically added to PostgreSQL.

## Persistence and compatibility

The MQTT payload contract and main table relationships are documented in [architecture.md](architecture.md). Database schemas are created in `api/operations_app.py`; the simulator SQLite schema is created in `simulator/app.py`. The technical identifiers `sensors` and `sensor_id` are retained even though the interface calls them metering points.

Chat conversations and messages are stored in PostgreSQL and scoped to the signed-in user. Saved assistant Markdown is sanitized again when a conversation is reopened. Deleting a chat cascades to its messages.
