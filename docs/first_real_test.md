# First real Zeekr connection: step by step

Branch: `zeekr-only`. Everything below is **read-only**. The code can only call
login, vehicle list, vehicle status, charging status, charging limit, journey
log and trip trackpoints. Remote commands and charge/travel plans are blocked
by an allow-list proxy and covered by tests.

Do the steps in order. Stop at the first step that fails and send back the
printed message. It is safe to share: VINs are masked and no secrets are
printed.

---

## 0. Before you start

- **Use a separate Zeekr account** that the 7X is shared with. The
  zeekr_ev_api README warns that API logins can log your phone app out.
- You need eight values for that account. Obtaining the key values is your
  responsibility (see the zeekr_ev_api README); this project does not supply
  them.
- Run all commands from the repo root.

## 1. Create `.env` (never commit it)

```bash
cp .env.example .env
chmod 600 .env
```

Fill in these variables in `.env`. The list below gives names only:

| Variable | Notes |
|---|---|
| `ZEEKR_USERNAME` | account e-mail / login |
| `ZEEKR_PASSWORD` | |
| `ZEEKR_COUNTRY_CODE` | optional, default `AU` |
| `ZEEKR_HMAC_ACCESS_KEY` | |
| `ZEEKR_HMAC_SECRET_KEY` | |
| `ZEEKR_PASSWORD_PUBLIC_KEY` | |
| `ZEEKR_PROD_SECRET` | |
| `ZEEKR_VIN_KEY` | |
| `ZEEKR_VIN_IV` | |

Also set `POSTGRES_PASSWORD`, `GRAFANA_DB_PASSWORD` and `GRAFANA_ADMIN_PASSWORD`
(see `.env.example`); compose refuses to start without them.

If any Zeekr variable is missing, the tools print `Missing required Zeekr environment
variable(s): <NAMES>` and exit with code 2. They never print values.

## 2. Create `config.json` and `data/` **before** any `docker compose` command

```bash
cp config.example.json config.json      # contains "providers": [{"type": "zeekr"}]
mkdir -p data && chmod 700 data
```

Why: compose mounts `./config.json` read-only with `create_host_path: false`,
so a missing file is an error, not a silently created directory. `./data` is
the only data mount. It holds `zeekr_session.json` (login tokens, mode 0600,
password stripped) and `captures/`. If `data/` does not exist, Docker creates
it owned by root, and the logger cannot write to it.

The logger container runs as a **non-root user, uid/gid 1000** by default.
`data/` must be owned by that user and `config.json` readable by it. If your
own uid is not 1000 (check with `id -u`), either build with your ids:

```bash
echo "APP_UID=$(id -u)" >> .env && echo "APP_GID=$(id -g)" >> .env
```

or hand the directory to uid 1000: `sudo chown -R 1000:1000 data`.

## 3. Connection check (`--check`)

Docker (recommended; reads `.env` automatically):

```bash
docker compose build zeekrmate
docker compose run --rm zeekrmate python scripts/dump_zeekr.py --check
```

Or on the host (Python ≥ 3.10):

```bash
python -m venv venv && . venv/bin/activate && pip install -r requirements.txt
set -a; . ./.env; set +a          # export the ZEEKR_* variables
python scripts/dump_zeekr.py --check
```

Success looks like this:

```
Login OK. Vehicles on this account: 1
  - VIN ***1234
OK
```

Exit code 0. Other outcomes:

| Exit | Meaning | What to do |
|---|---|---|
| 2 | `CONFIG ERROR: Missing ... <NAMES>` | fill in the named variables in `.env` |
| 3 | `AUTH FAILED: ...` | check username/password/country, then the key variables. On the host, check quoting in `.env` |
| 4 | `API ERROR: ...` | send the line back. It contains only a sanitised summary |
| 5 | no vehicles | share the car with this account in the Zeekr app |

A successful check writes `data/zeekr_session.json`. Later runs reuse it
instead of logging in again. To force a fresh login, delete that file.

## 4. Raw capture

```bash
docker compose run --rm zeekrmate python scripts/dump_zeekr.py
# host: python scripts/dump_zeekr.py      (writes ./captures/...)
```

Docker writes to `data/captures/YYYY-MM-DD-HHMM/` on the host. The directory
has mode 0700 and the files have mode 0600:

```
vehicles.json  vehicle_status.json  charging_status.json  charging_limit.json
journeys.json  trip_trackpoints.json (only if a trip exists in the last 30 days)
manifest.json  (which calls succeeded; masked VIN; library version)
```

A failing secondary call is recorded as `{"_error": "..."}` and the capture
continues. Useful options: `--vin 1234` (VIN suffix, if more than one car) and
`--days 7`.

For the best mapping evidence, take **three captures**:
1. parked and asleep (car locked for 30 min or more);
2. **plugged in and charging** (AC; DC too if you can);
3. **while driving** or just after a drive. This one is needed before
   `is_driving` can be mapped. Do it as a passenger, or run the command from
   home while someone else drives.

## 5. Sanitise before sharing

```bash
python scripts/sanitise_zeekr_capture.py data/captures/2026-10-19-0930
# -> data/captures/2026-10-19-0930-sanitised/
# or, without Python on the host:
docker compose run --rm zeekrmate python scripts/sanitise_zeekr_capture.py /app/data/captures/2026-10-19-0930
```

The script replaces VINs, IDs, names, phone numbers, e-mails and token-like
strings with stable placeholders, and rounds coordinates to 2 dp (about 1 km).
SoC, odometer, temperatures, status codes and timestamps are kept, because
those are what we need to verify. **Open the sanitised files and check them
yourself before you send them.** Never send the unsanitised folder or `data/`.

## 6. One poll into Postgres (`--once`)

```bash
docker compose up -d db
docker compose run --rm zeekrmate python -m zeekrmate --once
```

Success in the log looks like this (timestamps shortened; with no stored
session from step 3 you see `Zeekr: logged in with a fresh session` instead of
`reusing stored session`):

```
... [INFO] Database schema created/verified
... [INFO] Configured 1 provider(s)
... [INFO] Zeekr: reusing stored session
... [INFO] Zeekr: 1 vehicle(s): ***1234
... [INFO] Provider zeekr: 1 vehicle(s)
... [INFO] Position #1 ***1234 | SoC=78.0% | Range=412.0km | Odo=12345.6km | State=parked
```

`--once` exits 0 when a new position was stored, and 1 when a provider failed
or nothing new was stored. Running it twice in a row while the car is asleep
returns the same snapshot, which is skipped as a duplicate; the second run
then ends with:

```
... [ERROR] Single poll failed: db_ok=True providers_ok=1 providers_failed=0 positions_stored=0 position_errors=0
```

A missing-variable error is a single log line naming the variables. If the
database is not reachable, the single line is `Database unavailable at
<host>:<port> (OperationalError): ...`.

Check what was stored:

```bash
docker compose exec db psql -U zeekrmate -c "select ts, soc_pct, range_km, odometer_km, lat is not null as has_pos, charge_power_kw, charge_status, plug_status, door_lock_status, state from positions order by id desc limit 3;"
```

Some columns stay empty on purpose. `energy_remaining_kwh`, `bat_capacity_kwh`,
`bat_temp_c`, `outside_temp_c`, `speed_kmh` and the tyre pressures are not
mapped until a capture confirms which Zeekr field holds them. They are never
estimated (for example from SoC times a nominal capacity). Until then the
Grafana panels that need them (Total Energy Added, consumption in kWh/100 km,
Battery Capacity, temperatures) show *No data*. Trips are still recorded:
driving is inferred from odometer/position changes between snapshots.

`ts` is the car's `climateStatus.updateTime` in UTC. If that field is missing
or unparsable, `ts` is the last valid `updateTime` seen for the car by the
running process, or the time of the poll if there is none yet. `state` uses the mapper's `is_charging` flag, which is
provisional until your charging capture confirms the `chargerState` codes.

Then stop the stack. Run it unattended (`docker compose up -d`) only after the
mapping has been checked against your captures:

```bash
docker compose down
```

## 7. What to send back

1. The output of `--check` and the `--once` log lines. VINs are masked.
2. The **sanitised** capture folders from step 4. Label each one: parked,
   charging AC, charging DC, driving.
3. For each capture, what the app showed at the same time: SoC %, range,
   odometer, charging power, plugged in yes/no, locked yes/no.

We need these to confirm or correct every mapping in
`zeekrmate/providers/zeekr/mapper.py`. All of them are marked PROVISIONAL.
The open questions are:
- `chargerState` codes while charging and not charging (the HA hint is `{1,2,15}`);
- the `statusOfChargerConnection` values;
- whether `climateStatus.updateTime` is a good snapshot timestamp;
- which tyre key is which corner on a right-hand-drive car;
- a speed or driving indicator (until then driving is inferred from odometer
  and position changes, so the driving capture also shows how often the
  snapshot refreshes while driving);
- which fields, if any, report energy remaining, battery capacity and
  temperatures (those columns stay empty until confirmed).
