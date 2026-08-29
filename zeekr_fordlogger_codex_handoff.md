# Codex Handoff — FordLogger → Multi-Provider EV Logger with Zeekr 7X Support

> **Document status (2026-08-29):** This is the source roadmap and historical
> execution handoff. PR 1 is implemented at commit `97faaa0` and verified in
> [the PR 1 implementation specification](docs/pr1-vehicle-provider-spec.md#implementation-evidence).
> PR 2 through PR 10 remain roadmap material and are not implemented. The
> “First Codex task” section records the completed PR 1 execution boundary; it
> is not an instruction to repeat that work.

## Objective

Fork and adapt:

- Upstream: `https://github.com/lutzerb/fordlogger`
- Zeekr API dependency: `https://github.com/Fryyyyy/zeekr_ev_api`

The goal is to extend FordLogger so it supports a **Zeekr 7X in Australia** while preserving the existing Ford implementation.

This is **not** a greenfield rewrite.

The implementation strategy is:

```text
Existing FordLogger
        │
        ▼
Extract vehicle-provider boundary
        │
        ├──────── FordProvider
        │             │
        │             └── existing FordConnect code
        │
        └──────── ZeekrProvider
                      │
                      └── zeekr_ev_api
```

Retain and reuse FordLogger's existing:

```text
Poller
StateMachine
Trip finalisation
Charge finalisation
PostgreSQL
Grafana
Geocoder
Docker Compose
Existing tests
```

Add Zeekr-specific functionality behind the provider abstraction.

The Zeekr implementation must be **read-only with respect to the vehicle** for v1.

---

# Core implementation principles

## 1. Do not rewrite working FordLogger infrastructure

Do **not** replace FordLogger's existing:

- `psycopg2`
- PostgreSQL schema approach
- state machine
- poller
- trip detector
- charge detector
- Docker Compose setup
- Grafana dashboards

just because a greenfield design might choose different technology.

Do **not** introduce:

- SQLAlchemy
- Alembic
- FastAPI
- Kafka
- Redis
- NATS
- TimescaleDB
- PostGIS
- Kubernetes
- Celery

unless a later, concrete requirement proves they are necessary.

The value of this implementation path is that FordLogger already solves a large amount of the historical telemetry problem.

---

## 2. Preserve Ford functionality

The first architectural change is to make FordLogger provider-independent.

Ford functionality must continue to work throughout the refactor.

Avoid a "Zeekr fork" that destroys or bypasses existing Ford support.

Preferred long-term architecture:

```text
                       Poller
                         │
                         ▼
                VehicleProvider
                         │
            ┌────────────┴────────────┐
            │                         │
            ▼                         ▼
      FordProvider              ZeekrProvider
            │                         │
      FordConnect                zeekr_ev_api
            │                         │
            └────────────┬────────────┘
                         │
                         ▼
                 Generic models
                         │
              ┌──────────┼─────────┐
              ▼          ▼         ▼
           State       Trips     Charging
          Machine
              │          │         │
              └──────────┴─────────┘
                         │
                         ▼
                     PostgreSQL
                         │
                         ▼
                      Grafana
```

---

## 3. Zeekr gives us an important capability FordLogger does not have

Zeekr exposes provider-side journey history and trip trackpoints.

Use this.

The architecture should therefore additionally support:

```text
Zeekr journey history
        │
        ▼
Journey reconciler
        │
        ▼
existing trips table
```

and:

```text
Zeekr trip trackpoints
        │
        ▼
trip_points
```

This allows the logger to recover journeys missed during downtime.

That is a major reliability advantage over relying purely on live polling.

---

## 4. Provider-specific data stops at the provider boundary

Never allow Ford-specific telemetry names or Zeekr JSON structures into the generic core.

Correct:

```text
Zeekr JSON
    ↓
Zeekr mapper
    ↓
Position / ProviderJourney / TrackPoint
    ↓
generic FordLogger core
```

Incorrect:

```python
if zeekr_payload["some"]["nested"]["field"] == ...
```

inside:

- `poller.py`
- `state_machine.py`
- `trip_detector.py`
- `charge_detector.py`

Likewise, generic code must not know about FordConnect metric names.

---

# Existing FordLogger architecture to preserve

Relevant current files include approximately:

```text
fordlogger/
├── main.py
├── config.py
├── auth.py
├── api.py
├── db.py
├── models.py
├── poller.py
├── state_machine.py
├── trip_detector.py
├── charge_detector.py
└── geocoder.py

sql/
└── schema.sql

grafana/
tests/
docker-compose.yml
```

Current responsibilities:

```text
api.py
    Ford API access + mapping into Position

poller.py
    polling
    deduplication
    state transitions
    persistence
    trip lifecycle
    charging lifecycle

state_machine.py
    parked / driving / charging / sleeping

trip_detector.py
    finalises trip from saved positions

charge_detector.py
    finalises charging session from saved positions

db.py
    psycopg2 persistence

models.py
    Vehicle
    Position
    Trip
    ChargeSession
```

Do not replace this structure wholesale.

Refactor incrementally.

---

# Licensing

FordLogger is AGPL-3.0.

Retain:

- licence
- upstream attribution
- copyright notices

The derived project must comply with AGPL-3.0.

`zeekr_ev_api` is MIT licensed and should be consumed as a dependency rather than vendored.

Avoid copying code from unrelated Zeekr projects unless necessary.

---

# PR 0 — Establish a clean upstream baseline

## Goal

Prove the fork works before making architecture changes.

## Tasks

1. Fork FordLogger.
2. Add the original repository as `upstream`.
3. Run all existing tests.
4. Run PostgreSQL-backed tests if available.
5. Build the Docker image.
6. Validate Docker Compose.
7. Start PostgreSQL and Grafana locally if practical.
8. Record the existing test count and known failures.
9. Make no behavioural changes.

Add:

```text
docs/upstream.md
```

Document:

```text
origin   = our fork
upstream = lutzerb/fordlogger
upstream branch = main
```

## Acceptance criteria

- existing tests pass, excluding any already-known upstream failures
- Docker image builds
- `docker compose config` succeeds
- no functional behaviour changes

Commit separately.

---

# PR 1 — Extract `VehicleProvider`

## Goal

Remove the direct dependency between `Poller` and `FordAPI`.

This PR must preserve Ford behaviour.

## Add

```text
fordlogger/providers/
├── __init__.py
├── base.py
├── factory.py
└── ford.py
```

Do not move `api.py` or `auth.py` yet.

Avoid creating a large rename-only diff.

---

## Provider interface

Create approximately:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    journey_history: bool = False
    trip_trackpoints: bool = False


class VehicleProvider(ABC):
    name: str

    @abstractmethod
    def get_vehicles(self) -> list["Vehicle"]:
        ...

    @abstractmethod
    def get_telemetry(self) -> list["Position"]:
        ...

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()
```

Do not create abstractions for features that do not yet exist.

---

## FordProvider

Implement:

```python
class FordProvider(VehicleProvider):
    ...
```

as a thin adapter around the existing `FordAPI`.

Conceptually:

```text
get_vehicles()
    -> FordAPI.get_garage()

get_telemetry()
    -> FordAPI.get_telemetry()
```

Do not rewrite Ford authentication or Ford telemetry mapping yet.

---

## Poller refactor

The poller must depend on providers rather than Ford API objects.

Before:

```python
for api in self.apis:
    vehicles = api.get_garage()
    positions = api.get_telemetry()
```

After:

```python
for provider in self.providers:
    vehicles = provider.get_vehicles()
    positions = provider.get_telemetry()
```

No Ford import should remain in the generic poller.

---

## Provider factory

Add:

```python
create_providers(cfg) -> list[VehicleProvider]
```

Initially support Ford only.

Preserve existing Ford configuration behaviour where possible.

---

## Tests

Create a fake provider:

```python
class FakeProvider(VehicleProvider):
    ...
```

Verify:

- poller works without `FordAPI`
- vehicles persist
- positions persist
- state transitions still occur
- `FordProvider` returns the same generic models the previous Ford path produced
- all current Ford tests continue to pass

## Hard stop

Do not add Zeekr in this PR.

---

# PR 2 — Make persistence provider-aware

## Goal

Add only the schema changes needed for more than one vehicle provider.

Do not redesign persistence.

---

## `vehicles`

Add:

```sql
provider TEXT NOT NULL DEFAULT 'ford'
```

Optionally:

```sql
provider_vehicle_id TEXT
```

VIN remains the main vehicle identifier for Ford and Zeekr.

---

## `positions`

Add:

```sql
observed_at TIMESTAMPTZ DEFAULT NOW()
source TEXT
```

Semantics:

```text
ts
    source/provider vehicle timestamp

observed_at
    time our logger fetched the data
```

Possible `source` values initially:

```text
live
```

Do not put provider names in `source`.

Provider is separate metadata.

---

## `raw_json`

Keep the current `raw_json JSONB`.

For Zeekr, one position may retain the combined source payload:

```json
{
  "vehicle_status": {},
  "charging_status": {},
  "charging_limit": {}
}
```

Do not introduce a separate raw event store yet.

---

## Schema evolution

FordLogger currently uses SQL schema files.

Do not introduce Alembic solely for this task.

Use additive/idempotent SQL such as:

```sql
ALTER TABLE ...
ADD COLUMN IF NOT EXISTS ...
```

or a minimal SQL migration sequence only if required.

---

# PR 3 — Add Zeekr provider shell + capture tooling

## Goal

Prove authentication and capture real Australian Zeekr 7X payloads before mapping the API deeply.

This PR should focus on:

- connecting
- capturing
- sanitising
- fixtures

not on guessing every telemetry field.

---

## Dependency

Add `zeekr_ev_api`.

Pin to:

- a known working release, or
- a known Git commit

once tested against the Australian 7X.

Do not vendor it.

---

## Add

```text
fordlogger/providers/zeekr/
├── __init__.py
├── provider.py
├── mapper.py
└── config.py
```

Also:

```text
scripts/dump_zeekr.py
scripts/sanitise_zeekr_capture.py
```

---

## Configuration

Support Zeekr credentials via configuration plus environment variables.

Expected secret values may include:

```text
ZEEKR_USERNAME
ZEEKR_PASSWORD
ZEEKR_COUNTRY_CODE
ZEEKR_HMAC_ACCESS_KEY
ZEEKR_HMAC_SECRET_KEY
ZEEKR_PASSWORD_PUBLIC_KEY
ZEEKR_PROD_SECRET
ZEEKR_VIN_KEY
ZEEKR_VIN_IV
```

Actual names should follow what `zeekr_ev_api` requires.

Never log these values.

Use:

```text
ZEEKR_COUNTRY_CODE=AU
```

Do not hard-code assumptions such as:

```text
Australia == SEA
```

Use the Zeekr library's region discovery/routing.

---

## Recommended Zeekr account setup

Document:

```text
primary Zeekr account
        │
        └── share vehicle
                 │
                 ▼
       dedicated logger account
```

Authentication/key extraction remains a manual prerequisite.

Do not automate APK modification or key extraction in this project.

---

# `dump_zeekr.py`

The capture tool should fetch all useful read-only payloads.

At minimum:

```text
vehicle list
vehicle status
vehicle charging status
vehicle charging limit
journey history
```

If available/useful:

```text
VTM status
```

For a known journey, also fetch:

```text
trip trackpoints
```

Example output:

```text
captures/
└── YYYY-MM-DD-HHMM/
    ├── vehicles.json
    ├── vehicle_status.json
    ├── charging_status.json
    ├── charging_limit.json
    ├── journeys.json
    └── trip_trackpoints.json
```

Keep the original local capture intact.

Sanitise separately before committing fixtures.

Sanitisation must remove or perturb:

```text
VIN
account IDs
tokens
names
addresses
exact coordinates
other personal identifiers
```

---

# Required real-car validation checkpoint

Before PR 4, capture real payloads from the Australian 7X in as many of these states as practical:

```text
parked
driving
AC charging
DC charging
journey history
trip trackpoints
```

## Hard rule

Do not map guessed fields.

If a field is not demonstrated by:

- actual Australian 7X capture data, or
- clearly verified library behaviour,

leave it unmapped.

---

# PR 4 — Zeekr live telemetry mapping

## Goal

Feed Zeekr telemetry through the existing FordLogger pipeline using generic models.

`ZeekrProvider.get_telemetry()` should combine read-only calls such as:

```text
get_vehicle_status()
get_vehicle_charging_status()
get_vehicle_charging_limit()
```

into one generic `Position` per vehicle.

---

## Initial mapping targets

Map only confirmed fields.

Expected useful concepts:

```text
SOC
estimated range
odometer
speed
latitude
longitude
heading
battery temperature
outside temperature
charge state
plug state
charge power
charger voltage
charger current
charge limit / target SOC
tyre pressures
lock status
```

Unknown/unavailable values remain `None`.

Do not invent values.

---

## Raw data preservation

Set:

```python
Position.raw_json
```

to a combined payload such as:

```python
{
    "vehicle_status": vehicle_status,
    "charging_status": charging_status,
    "charging_limit": charging_limit,
}
```

This allows future re-mapping if the Zeekr API changes.

---

# Timestamp policy

Do not use `datetime.now()` as the provider timestamp unless no source timestamp exists.

Use:

```text
Position.ts
    most authoritative vehicle/provider timestamp

Position.observed_at
    local retrieval timestamp
```

If different Zeekr endpoints have different update timestamps:

- retain all timestamps in raw JSON
- choose a documented canonical source timestamp for the generic `Position`

---

# Deduplication change

FordLogger's current timestamp-only deduplication may not be sufficient for Zeekr because vehicle and charging endpoints may update independently.

Add to `Position`:

```python
snapshot_key: str | None = None
```

Zeekr should calculate a deterministic hash over canonicalised relevant data.

Poller behaviour:

```text
if snapshot_key exists:
    compare snapshot_key
else:
    retain current timestamp-based deduplication
```

This allows Ford behaviour to remain intact while preventing missed Zeekr updates such as:

```text
charge power changed
vehicle-status timestamp unchanged
```

---

# Generic state signals

The existing state machine may contain Ford-specific ignition assumptions.

Do not rewrite it completely.

Add generic optional signals to `Position`:

```python
is_driving: bool | None = None
is_charging: bool | None = None
```

Provider mappers may populate them.

State inference priority should become approximately:

```text
DRIVING

explicit provider is_driving
    ↓
generic speed/movement inference
    ↓
legacy Ford ignition inference
```

and:

```text
CHARGING

explicit provider is_charging
    ↓
generic plug/power inference
    ↓
legacy Ford charge-string inference
```

Preserve Ford fallbacks.

---

# PR 4 acceptance criteria

Against the real Australian 7X, a one-shot or normal poll must persist at least:

```text
VIN
provider=zeekr
SOC
range
odometer
raw_json
observed_at
source timestamp where available
```

Grafana should be able to graph:

```text
SOC
odometer
```

using the existing database.

## Hard stop

Do not implement journey reconciliation until live telemetry works reliably against the real vehicle.

---

# PR 5 — Zeekr journey reconciliation

## Goal

Use Zeekr's provider-side journey history as an authoritative reconciliation/backfill mechanism.

This is one of the most valuable Zeekr-specific features.

---

# Add generic provider journey model

Add approximately:

```python
@dataclass
class ProviderJourney:
    provider_trip_id: str
    report_time: datetime | None

    vin: str
    start_ts: datetime
    end_ts: datetime

    start_odometer_km: float | None
    end_odometer_km: float | None
    distance_km: float | None

    duration_s: int | None
    avg_speed_kmh: float | None
    energy_used_kwh: float | None

    start_lat: float | None
    start_lon: float | None
    end_lat: float | None
    end_lon: float | None

    raw_json: dict | None
```

Expected Zeekr journey concepts include:

```text
trip ID
report time
start time
end time
start mileage
end mileage
distance
duration
average speed
energy consumption
start coordinates
end coordinates
```

Map actual fields only after confirming the captured payload.

---

# Extend provider capability

Add optional provider method:

```python
def get_journeys(
    self,
    vin: str,
    days_back: int,
) -> list[ProviderJourney]:
    return []
```

Provider capabilities:

```text
Ford
    journey_history = false

Zeekr
    journey_history = true
```

---

# Extend `trips`

Add:

```sql
provider TEXT
provider_trip_id TEXT
provider_report_time TIMESTAMPTZ
trip_source TEXT
reconciled_at TIMESTAMPTZ
provider_raw_json JSONB
```

`trip_source` values:

```text
detected
provider
reconciled
```

Add an idempotency constraint/index for non-null provider trip IDs.

Conceptually:

```text
(provider, vin, provider_trip_id) UNIQUE
```

when `provider_trip_id` is non-null.

---

# Journey matching algorithm

For each Zeekr journey:

## Case 1 — exact provider ID already exists

Update the existing trip.

Do not insert another row.

---

## Case 2 — no provider ID attached, but a local trip likely matches

Match using multiple signals:

```text
VIN
start time
end time
distance
odometer
```

Suggested initial timestamp tolerance:

```text
±5 minutes
```

Do not use timestamp alone when multiple candidate trips exist.

Attach the provider trip ID to the local trip.

Use Zeekr values as authoritative when present for:

```text
distance
duration
average speed
energy consumption
start/end odometer
start/end coordinates
```

Preserve locally derived values when the provider field is absent.

Set:

```text
trip_source = reconciled
```

---

## Case 3 — no local trip

Insert a new trip directly from the Zeekr journey record.

Set:

```text
trip_source = provider
```

This is the missed-while-offline path.

---

# Reconciliation schedule

Run:

```text
on application startup
periodically thereafter
```

Suggested initial cadence:

```text
while active:
    every 5–10 minutes

while parked:
    every ~30 minutes
```

Configuration:

```text
journey_reconciliation_enabled = true
journey_reconciliation_days = 7
```

Initial startup may use a longer lookback if desired.

Avoid repeatedly requesting very large history windows.

---

# Journey pagination

Implement provider pagination completely and defensively.

Requirements:

- retrieve all journeys in the requested window
- avoid duplicate results
- avoid infinite loops
- stop cleanly at the end of the requested window

Do not assume one API call returns every journey.

---

# Critical reconciliation tests

Must include:

```text
local trip + matching Zeekr journey
    -> one reconciled trip

same provider journey processed twice
    -> still one trip

logger offline for entire drive
    -> trip inserted from provider history

logger restarted mid-drive
    -> local trip reconciled rather than duplicated

two short journeys close together
    -> remain separate

provider field missing
    -> local value preserved

provider summary differs from local derived value
    -> documented authoritative-field policy applied
```

Idempotency is mandatory.

---

# PR 6 — Zeekr trip trackpoints

## Goal

Recover route data for journeys even when the live logger was offline.

The Zeekr API exposes trip trackpoints.

Treat historical provider trackpoints separately from live polling positions.

Do not feed them into the live vehicle state machine.

---

# Add `trip_points`

Create:

```sql
CREATE TABLE trip_points (
    id BIGSERIAL PRIMARY KEY,
    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,

    sequence_no INTEGER,
    ts TIMESTAMPTZ,

    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,

    speed_kmh REAL,
    heading REAL,

    source TEXT NOT NULL,
    raw_json JSONB
);
```

Indexes:

```text
trip_id, sequence_no
trip_id, ts
```

only where the source data supports them.

Do not invent unsupported fields.

---

# Import policy

After reconciling a Zeekr journey:

```text
if provider trip ID exists
and provider trackpoints have not been imported:
    request trackpoints
    persist them idempotently
```

Use:

```text
source = zeekr_journey
```

Do not repeatedly fetch complete historical tracks.

---

# Route selection

When displaying a trip:

```text
if provider trackpoints exist:
    prefer provider trackpoints
else:
    use live positions from the trip window
```

This allows a route to exist even if the collector was offline.

---

# PR 7 — Persist active runtime state

## Goal

Improve restart recovery inherited from FordLogger.

FordLogger currently keeps some active trip/charge lifecycle state in memory.

That is vulnerable to process restarts.

Zeekr journey reconciliation repairs completed drives, but runtime persistence still improves both providers.

---

# Add runtime state

Preferred small table:

```sql
CREATE TABLE runtime_state (
    vin TEXT PRIMARY KEY,
    provider TEXT,
    vehicle_state TEXT,

    drive_start_ts TIMESTAMPTZ,
    charge_start_ts TIMESTAMPTZ,

    last_snapshot_key TEXT,
    updated_at TIMESTAMPTZ
);
```

Restore this on startup.

Do not build a general workflow engine.

---

# State debounce

Real Zeekr fixtures must determine practical thresholds.

Avoid:

```text
one anomalous speed sample
    -> false drive start

one missing charging sample
    -> false charge end
```

Introduce configurable debounce/confirmation.

Conceptually:

```text
driving:
    confirm over multiple consistent observations

charging end:
    confirm over multiple observations
```

Do not hard-code values before examining the actual source update rate.

---

# PR 8 — Zeekr charging refinement

## Goal

Reuse FordLogger's existing charge-session lifecycle and aggregation with Zeekr data.

Map confirmed Zeekr concepts:

```text
charging state
plug state
power
voltage
current
target SOC
```

Add only schema fields that are genuinely useful and supported.

Possible additions:

```sql
positions.charge_limit_pct
charge_sessions.target_soc_pct
```

---

# Charging energy

If Zeekr exposes authoritative session energy:

```text
use provider value
```

Otherwise:

```text
derive from power samples
```

If implementing integration, use trapezoidal integration:

```text
energy_kwh ≈ ∫ power_kw dt
```

Do not represent estimated energy as exact metered consumption.

Add if useful:

```sql
charge_sessions.energy_source TEXT
```

Values:

```text
provider
integrated
unknown
```

---

# AC/DC classification

Prefer explicit provider information.

Only infer AC/DC from power characteristics when necessary.

If inferred:

- document the rule
- make the threshold configurable if appropriate

---

# PR 9 — Grafana adaptation

## Goal

Reuse existing FordLogger dashboards wherever possible.

Do not rebuild Grafana from scratch.

Existing dashboard concepts already map well:

```text
Overview
Trips
Charging
Consumption
Degradation
Map
Timeline
Raw Data
```

---

# Make dashboards provider-neutral

Remove Ford-specific labels where they would be incorrect for Zeekr.

Do this only after the Zeekr pipeline is working.

Avoid a large early rename.

---

# Expected reusable fields

Existing dashboards should be able to work with:

```text
soc_pct
range_km
odometer_km
speed_kmh
lat/lon
charge_power_kw
trips
charge_sessions
states
```

Unavailable Zeekr fields should:

- show no data
- remain hidden
- fail gracefully

They must not break the dashboard.

---

# Battery degradation wording

Do not claim a Zeekr:

```text
Battery Health %
```

unless the provider exposes a real BMS SOH value.

If the dashboard estimates degradation from range/SOC behaviour, label it clearly as an estimate.

---

# Zeekr-specific useful additions

Potential dashboard fields:

```text
provider journey ID
trip source
reconciliation status
journey energy consumption
provider route availability
target SOC
```

Do not create a separate Zeekr-only dashboard unless it adds real value.

---

# PR 10 — Cleanup and optional rename

Only do this after:

- Ford still works
- Zeekr live telemetry works
- journey reconciliation works
- charging works
- Grafana works

Possible final project name:

```text
EVLogger
```

if the repository intends to support both Ford and Zeekr.

Possible final package layout:

```text
evlogger/
├── main.py
├── config.py
├── db.py
├── models.py
├── poller.py
├── state_machine.py
├── trip_detector.py
├── charge_detector.py
├── journey_reconciler.py
│
└── providers/
    ├── base.py
    ├── factory.py
    │
    ├── ford/
    │   ├── provider.py
    │   ├── api.py
    │   └── auth.py
    │
    └── zeekr/
        ├── provider.py
        ├── mapper.py
        └── config.py
```

Only at this stage move existing:

```text
api.py
auth.py
```

under the Ford provider package.

Avoid doing this earlier because large file moves obscure functional changes.

---

# Configuration target

A future provider-neutral configuration may resemble:

```json
{
  "providers": [
    {
      "type": "zeekr",
      "country_code": "AU"
    }
  ],

  "polling": {
    "driving": 30,
    "charging": 30,
    "parked": 180,
    "sleeping": 600
  },

  "journey_reconciliation": {
    "enabled": true,
    "days_back": 7
  }
}
```

Secrets remain environment variables.

Maintain backwards compatibility with Ford configuration where practical.

---

# Zeekr polling strategy

Do not blindly reuse Ford polling assumptions.

Start conservatively:

```text
driving:
    ~30 seconds

charging:
    ~30 seconds

parked:
    2–5 minutes

long parked / sleeping:
    5–15 minutes
```

Then measure:

```text
how often Zeekr source timestamps actually change
```

There is no value polling faster than the provider publishes meaningful updates.

---

# No Zeekr remote control in v1

Even if `zeekr_ev_api` exposes commands, do not add:

```text
lock
unlock
climate control
window control
tailgate control
start/stop charging
```

The project is a telemetry logger.

Only use read endpoints.

This reduces security risk and keeps scope focused.

---

# Security requirements

Never log:

```text
Zeekr username
Zeekr password
HMAC access key
HMAC secret key
password encryption key
prod secret
VIN key
VIN IV
session tokens
authorization headers
```

Grafana must never receive Zeekr credentials.

The logger process/container is the only component that needs them.

---

# Testing layout

Recommended:

```text
tests/
├── providers/
│   ├── test_ford_provider.py
│   └── zeekr/
│       ├── test_mapper_parked.py
│       ├── test_mapper_driving.py
│       ├── test_mapper_ac_charging.py
│       ├── test_mapper_dc_charging.py
│       ├── test_journey_mapper.py
│       └── test_trackpoint_mapper.py
│
├── test_poller.py
├── test_state_machine.py
├── test_trip_detector.py
├── test_charge_detector.py
└── test_journey_reconciler.py
```

No CI test may require a live Zeekr account.

CI uses sanitised fixtures only.

---

# Shared provider contract tests

Both Ford and Zeekr providers should satisfy generic expectations:

```text
get_vehicles() returns Vehicle objects

get_telemetry() returns Position objects

VIN is always present

timestamps are timezone-aware

missing provider fields become None

raw provider payload is retained

provider failures do not produce malformed generic models
```

---

# Observability

Add provider context to logs.

Example:

```text
provider=zeekr
event=telemetry_poll_success
vin=<masked>
```

Useful events:

```text
provider_login_success
provider_login_failed
telemetry_poll_success
telemetry_poll_failed
vehicle_state_changed
trip_detected
trip_reconciled
trip_backfilled
trackpoints_imported
charge_started
charge_finished
```

Do not log entire raw API payloads during routine operation.

---

# Definition of Zeekr v1 complete

The Zeekr implementation is complete when the following are true.

## Live telemetry

The logger continuously captures at least:

```text
SOC
range
odometer
location
charging state
```

plus any other reliably exposed values.

---

## Trips

Every journey appears once.

When the logger was running:

```text
local telemetry
    +
provider journey
    ↓
one reconciled trip
```

When the logger was offline:

```text
provider journey history
    ↓
backfilled trip
```

---

## Routes

A trip can display route history from:

```text
live positions
```

or:

```text
Zeekr provider trackpoints
```

---

## Charging

Charging sessions show, where available:

```text
start/end time
start/end SOC
duration
power
target SOC
energy
```

with provider-reported vs estimated energy distinguished.

---

## Grafana

Existing dashboards operate for Zeekr without Ford-specific errors.

---

## Reliability

Restarting the logger:

```text
does not duplicate trips
does not duplicate journey imports
does not corrupt active charging
does not require manual database repair
```

---

# Explicitly out of scope for v1

Do not implement:

```text
remote vehicle control
custom React frontend
mobile application
Home Assistant integration
MQTT
Kubernetes
TimescaleDB
PostGIS
SQLAlchemy rewrite
Alembic rewrite
multi-user authentication
electricity pricing
home EVSE integration
predictive maintenance
unverified battery health
```

---

# Recommended Codex execution sequence

Codex must not implement the whole project in one pass.

Use:

```text
PR 0
Baseline fork

PR 1
VehicleProvider abstraction
Ford behaviour unchanged

PR 2
Provider-aware additive schema

PR 3
Zeekr authentication + capture tooling

======== HARD REAL-CAR CHECKPOINT ========

Capture:
- parked
- driving
- AC charging
- DC charging
- journeys
- trackpoints

PR 4
Zeekr live telemetry mapping

======== HARD REAL-CAR CHECKPOINT ========

Verify:
- SOC
- range
- odometer
- raw data
- Grafana history
- deduplication

PR 5
Journey reconciliation

PR 6
Trip trackpoint import

PR 7
Persistent runtime/restart state

PR 8
Charging refinement

PR 9
Grafana adaptation

PR 10
Optional package/project cleanup
```

Do not skip the real-car checkpoints.

Do not allow Codex to guess an Australian 7X payload shape.

---

# First Codex task

Implement **PR 1 only**.

Use this prompt:

> Refactor the current FordLogger codebase to introduce a vehicle-provider abstraction without changing observable Ford behaviour.
>
> Create a `VehicleProvider` interface exposing `get_vehicles()` and `get_telemetry()`, plus a small `ProviderCapabilities` model.
>
> Create `FordProvider` as an adapter around the existing `FordAPI`.
>
> Do not rewrite or move `api.py` or `auth.py` yet.
>
> Refactor `Poller` and startup/provider discovery so generic code depends on `VehicleProvider` rather than `FordAPI`.
>
> Preserve backward compatibility with the current Ford configuration, database schema, CLI, Docker setup and Grafana dashboards.
>
> Add fake-provider tests proving the poller no longer depends directly on Ford.
>
> All existing tests must continue to pass.
>
> Do not add Zeekr support in this PR.
>
> Do not rename the project or Python package.
>
> Do not change persistence technology.
>
> Do not introduce SQLAlchemy, Alembic, FastAPI or any new infrastructure framework.
>
> At completion, report:
>
> 1. files changed,
> 2. provider interface created,
> 3. Ford compatibility preserved,
> 4. tests run and results,
> 5. any remaining Ford-specific assumptions in the poller, state machine or generic core that should be addressed before Zeekr support.
>
> Stop after this PR and do not continue automatically.

---

# Architectural success criterion

The generic core should know about:

```text
vehicle
position
driving
charging
trip
```

but not about:

```text
FordConnect
Zeekr Cloud
Ford metric names
Zeekr JSON field names
Zeekr authentication
Zeekr encryption keys
```

External systems are converted into stable generic domain concepts by provider implementations.

That provider boundary is the most important architectural outcome of the refactor.
