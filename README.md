# zeekrmate

Self-hosted data logger for the **Zeekr 7X**, built for an owner in Australia.

zeekrmate polls the Zeekr cloud API through the [`zeekr_ev_api`](https://github.com/Fryyyyy/zeekr_ev_api) library, stores the telemetry in PostgreSQL, detects trips and charge sessions, and shows everything in Grafana dashboards.

It is a modified fork of [FordLogger](https://github.com/lutzerb/fordlogger) by lutzerb (see [Attribution and licence](#attribution-and-licence)). Ford support has been removed; Zeekr is the only supported vehicle.

> **Status: not yet tested against a real car.** The logger, schema and dashboards run, but the mapping from Zeekr API fields to the database has not been confirmed with real 7X data. Follow [the first real test](docs/first_real_test.md) before relying on the numbers. Dashboard panels that depend on unconfirmed fields say so in their description (hover the panel's ⓘ icon).

> The internal Python package, database, database user and Docker service are still called `fordlogger`. They will be renamed in a later, separate change. You will see that name in commands below.

## What you get

- **Adaptive polling**: shorter intervals while driving or charging, longer while parked or asleep.
- **Vehicle state machine**: driving, charging, parked and sleeping.
- **Trip detection**: distance, duration, SoC used, speeds and (if the car reports energy) consumption in kWh/100 km.
- **Charge session detection**: SoC added, duration, power and (if reported) energy added.
- **8 Grafana dashboards**: Overview, Trips, Charging, Consumption, Battery Degradation, Map, Timeline and Raw Data.
- **Raw payload storage**: the API response is kept in `positions.raw_json` so the mapping can be checked and improved later.
- **Docker Compose deployment**: logger, PostgreSQL 16 and Grafana 11.

## Requirements

- A machine that runs **24/7** with Docker and Docker Compose v2.24 or later (a Raspberry Pi 4/5, a NAS, a home server or a small VPS). The logger polls; the car does not push data, so anything that happens while the logger is down is missed.
- About 256 MB RAM and under 1 GB of disk to start (the database grows slowly).
- A Zeekr account with access to the car. A dedicated logger account that the car is shared to is recommended, so the logger's sessions do not interfere with your phone app.
- **The Zeekr app keys required by `zeekr_ev_api`.** The library needs several app-level keys in addition to your username and password. You must supply them yourself, as described by `zeekr_ev_api`. This project does not provide them or explain how to obtain them.

## Setup

### 1. Clone

```bash
git clone https://github.com/johnsonjsyuen/zeekrmate.git
cd zeekrmate
cp config.example.json config.json
```

### 2. Create `.env`

All secrets live in a `.env` file next to `docker-compose.yml`. It is git-ignored; never commit it. Copy `.env.example` if present, then fill in every variable below.

| Variable | Required | Used by | Purpose |
|---|---|---|---|
| `POSTGRES_PASSWORD` | yes | db | Password of the `fordlogger` database owner (used by the logger). |
| `GRAFANA_DB_PASSWORD` | yes | db, grafana | Password of Grafana's read-only database role. |
| `GRAFANA_DB_USER` | no (default `grafana_ro`) | db, grafana | Name of that read-only role. |
| `GRAFANA_ADMIN_PASSWORD` | yes | grafana | Password of the Grafana `admin` user. |
| `GRAFANA_BIND` | no (default `127.0.0.1`) | grafana | Address Grafana listens on. See [Security](#security). |
| `ZEEKR_USERNAME` | yes | logger | Zeekr account login. |
| `ZEEKR_PASSWORD` | yes | logger | Zeekr account password. |
| `ZEEKR_COUNTRY_CODE` | no (default `AU`) | logger | Account country. |
| `ZEEKR_HMAC_ACCESS_KEY`, `ZEEKR_HMAC_SECRET_KEY`, `ZEEKR_PASSWORD_PUBLIC_KEY`, `ZEEKR_PROD_SECRET`, `ZEEKR_VIN_KEY`, `ZEEKR_VIN_IV` | yes | logger | App keys required by `zeekr_ev_api` (supply your own). |
| `FORDLOGGER_STORE_RAW_JSON` | no (default `true`) | logger | Keep raw API payloads. Leave on until the mapping is confirmed. |
| `FORDLOGGER_GEOCODING` | no (default `true`) | logger | Look up trip and charge addresses with OpenStreetMap Nominatim. This sends coordinates to OSM; set `false` to keep them local. |

Generate strong, distinct passwords, for example with `openssl rand -base64 24`. Avoid `$` in values (Compose treats it as a variable), or wrap the value in single quotes in `.env`.

`docker compose` refuses to start if a required password is missing.

**Database password for the logger:** the logger connects as `fordlogger` with `db_password` from `config.json`. Set it to the same value as `POSTGRES_PASSWORD`.

The database and Grafana passwords are applied **only when their volumes are first created**. To change them later, see [Changing passwords](#changing-passwords).

### 3. First real test

Before running the logger unattended, do a one-off check that login works and that the data looks right. Follow [docs/first_real_test.md](docs/first_real_test.md).

### 4. Run

```bash
docker compose up -d
docker compose logs -f fordlogger
```

This starts:

- **fordlogger**: the polling logger.
- **db**: PostgreSQL 16. It is not published on any host port.
- **grafana**: Grafana 11 at <http://localhost:3000> (user `admin`, password `GRAFANA_ADMIN_PASSWORD`).

## Dashboards

All dashboards are in the **zeekrmate** folder and have a **Vehicle** drop-down. Times are shown in the dashboard's time zone (your browser's by default).

| Dashboard | Shows |
|---|---|
| **Overview** | Current SoC, range, odometer, plugged-in and charging status, vehicle state, battery temperature, last position |
| **Trips** | Trip list with distance, duration, SoC used and consumption |
| **Charging** | Charge sessions, energy added, power curve, SoC while charging |
| **Consumption** | kWh/100 km per trip, against temperature and speed, monthly trend |
| **Battery Degradation** | Reported capacity and estimated full-charge range over time. These are **estimates**, not a measured state of health |
| **Map** | GPS route per trip with start and end markers |
| **Timeline** | State history (driving, charging, parked, sleeping) with SoC and speed |
| **Raw Data** | Every stored position, trip, charge session and state change, including raw API payloads |

**Battery Degradation setup:** the dashboard has two text boxes, *Rated capacity (kWh)* and *Rated range (km)*. They are empty by default. Fill them in from the published spec of your 7X variant to see capacity and range as a percentage of rated. To keep the values, save the dashboard.

**Fields not yet confirmed for Zeekr** (panels show *No data* if the car does not report them): battery and outside temperature, energy remaining (so trip consumption and charge energy), battery capacity, charge power, plug status values, and speed. The [first real test](docs/first_real_test.md) checks these.

![Overview dashboard (screenshot from upstream FordLogger)](docs/screenshot_overview.png)

## Security

The database holds a detailed history of where the car has been, including your home. The default setup is locked down:

- **PostgreSQL is not published** on the host. Only the logger and Grafana reach it, over the Compose network. To use `psql` from the host, uncomment the `127.0.0.1:5432:5432` line in `docker-compose.yml` (loopback only).
- **Grafana listens on `127.0.0.1` only.** To use it from other devices on your network, set `GRAFANA_BIND=0.0.0.0` in `.env`. Do not expose port 3000 to the internet; use a VPN (for example Tailscale or WireGuard) instead.
- **Grafana reads the database as a SELECT-only role** (`GRAFANA_DB_USER`). It is created by `sql/init-roles/20-grafana-readonly.sh` when the database is first initialised.
- **No default passwords.** Every password comes from `.env`.
- **Backups contain the same data.** Keep them private.

### Changing passwords

- **Grafana read-only role:** change `GRAFANA_DB_PASSWORD` in `.env`, then:
  ```bash
  docker compose up -d db
  docker compose exec db bash /docker-entrypoint-initdb.d/20-grafana-readonly.sh
  docker compose up -d --force-recreate grafana
  ```
- **Database owner:** run `docker compose exec db psql -U fordlogger -d fordlogger -c "\password fordlogger"`, then update `POSTGRES_PASSWORD` in `.env` and `db_password` in `config.json`, and restart the logger.
- **Grafana admin:** change it in the Grafana UI (profile, *Change password*). Changing `GRAFANA_ADMIN_PASSWORD` later has no effect on an existing Grafana volume.

## Backup and restore

```bash
./backup.sh                    # writes ./backups/zeekrmate_<UTC time>.dump
./backup.sh /path/to/backups   # or choose the directory
```

`backup.sh` runs `pg_dump` inside the `db` service with `docker compose exec`, so it works whatever your Compose project is called. It stops with an error, and leaves no file behind, if the database is not running, the dump fails or the result is empty or unreadable. Schedule it with cron, for example daily:

```cron
15 3 * * * cd /path/to/zeekrmate && ./backup.sh >> backups/backup.log 2>&1
```

To restore:

```bash
./restore.sh backups/zeekrmate_20261019T031500Z.dump
```

`restore.sh` asks for confirmation, stops the logger and Grafana, **replaces** the database contents with the backup, re-applies Grafana's read-only grants and starts the services again. The restore runs in a single transaction and stops at the first error, so a failed restore leaves the database unchanged. Plain SQL dumps (`.sql`, `.sql.gz`) are also accepted, but only into an empty database.

The data lives in the Docker volume `pgdata`. It survives `docker compose down` but **not** `docker compose down -v`.

## How it works

```
zeekrmate/
├── fordlogger/                 # Python package (rename pending)
│   ├── main.py                 # CLI entry point (--once, --backfill-addresses, daemon)
│   ├── config.py               # config.json + environment overrides, polling intervals
│   ├── providers/              # VehicleProvider boundary and the Zeekr provider
│   ├── poller.py               # Adaptive polling loop
│   ├── state_machine.py        # Vehicle state transitions
│   ├── trip_detector.py        # Trip summarisation
│   ├── charge_detector.py      # Charge session summarisation
│   ├── geocoder.py             # Optional reverse geocoding (Nominatim)
│   ├── db.py, models.py        # PostgreSQL access and data classes
├── sql/schema.sql              # Database schema
├── sql/init-roles/             # First-start database roles (Grafana read-only)
├── grafana/                    # Dashboards and provisioning
├── backup.sh, restore.sh
└── docs/                       # Design records and guides
```

All Zeekr-specific code stays behind the provider boundary in `fordlogger/providers/` ([ADR 0001](docs/adr/0001-vehicle-provider-boundary.md), [ADR 0002](docs/adr/0002-zeekr-only.md)). The poller, state machine, detectors, schema and dashboards only see generic fields.

### Database tables

- **`vehicles`**: one row per VIN.
- **`positions`**: one telemetry snapshot per poll, including `state` (`driving`, `charging`, `parked` or `sleeping`) and the raw payload.
- **`trips`**: summarised drives.
- **`charge_sessions`**: summarised charges.
- **`states`**: state transition log.

## Local development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt pytest
```

Run the tests against any PostgreSQL 16 you control (they use a separate test database):

```bash
FORDLOGGER_DB_HOST=localhost FORDLOGGER_DB_PORT=5432 python -m pytest tests/
```

The Compose database is not published on the host. For local development either run your own PostgreSQL or temporarily enable the loopback port mapping described in [Security](#security). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Known limitations

- **Short trips can be missed** if they start and end between two polls.
- **Max speed is a sampled value**, so it is usually lower than the true maximum.
- **AC/DC classification** of charge sessions is a power-threshold guess and can mislabel fast AC charging (the 7X supports 22 kW AC).
- **Battery degradation** panels are estimates derived from reported capacity or displayed range, not a measured state of health.

See [IDEAS.md](IDEAS.md) for planned work.

## Attribution and licence

zeekrmate is derived from **[FordLogger](https://github.com/lutzerb/fordlogger) by lutzerb**, licensed under the GNU Affero General Public License v3.0. The upstream git history is kept in this repository.

**Modified in 2026:** Ford support removed and Zeekr support added. See [NOTICE](NOTICE) for details.

zeekrmate is distributed under the same licence, [AGPL-3.0](LICENSE). You may use, modify and self-host it. If you run a modified version that other people interact with over a network, you must offer them its source code under the same licence.

[`zeekr_ev_api`](https://github.com/Fryyyyy/zeekr_ev_api) is a separate project under the MIT licence, used as a dependency.

zeekrmate is not affiliated with or endorsed by Zeekr or Ford.
