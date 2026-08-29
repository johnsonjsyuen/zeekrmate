# PR 1 Vehicle Provider Abstraction (Implementation)

- Status: Implemented and verified on 2026-08-29
- Source baseline: `ff1ffb758d056b4d8bd96a35364877939bdaf442`
- Scope: PR 1 only; no Zeekr implementation

## Objective

Remove the direct dependency between startup/polling and `FordAPI` while preserving all observable Ford behavior. External APIs produce the existing `Vehicle` and `Position` domain models at a provider boundary; the state machine, persistence, trip lifecycle, charge lifecycle, CLI, configuration, Docker Compose, and Grafana remain unchanged.

## Required behavior

### Provider contract

`fordlogger/providers/base.py` must define:

```python
@dataclass(frozen=True)
class ProviderCapabilities:
    journey_history: bool = False
    trip_trackpoints: bool = False


class VehicleProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def get_vehicles(self) -> list[Vehicle]: ...

    @abstractmethod
    def get_telemetry(self) -> list[Position]: ...

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()
```

No other provider methods or capabilities belong in PR 1.

### Ford adapter

`FordProvider` must:

- declare `name = "ford"`;
- accept an existing `FordAPI` client, enabling adapter tests without network access;
- provide a tested configuration constructor that builds the wrapped `FordAPI` for normal startup;
- return the wrapped client's `get_garage()` result from `get_vehicles()` without remapping it;
- return the wrapped client's `get_telemetry()` result from `get_telemetry()` without remapping it;
- inherit the default false capabilities.

### Provider discovery

`create_providers(cfg)` must preserve the current Ford discovery behavior:

| Condition | Result |
|---|---|
| `FORDLOGGER_TOKEN_DIR` contains sorted `tokens_*.json` files | One `FordProvider` per file, using a copied configuration whose `token_file` names that file. |
| No per-VIN files and configured legacy token file exists | One `FordProvider` built from the original configuration. |
| No token file exists | Empty list. |

Discovery must not mutate the caller's configuration. Logging must retain file-count and legacy-file visibility without exposing token contents.

### Poller

`Poller.__init__` accepts one provider or a list of providers and stores `self.providers`. Each cycle:

1. Ensures the database connection as before.
2. Calls `provider.get_vehicles()` during the existing garage refresh window and upserts every returned `Vehicle`.
3. Calls `provider.get_telemetry()` and processes every returned `Position` through the unchanged generic pipeline.
4. Logs and isolates vehicle-list failures, then still attempts telemetry for that provider.
5. Logs and isolates telemetry failures, then continues with later providers.
6. Uses the provider class name as safe diagnostic fallback when `provider.name` raises, without blocking that or later providers.
7. Updates the shared garage-refresh timestamp exactly as before.

`fordlogger/poller.py` must contain no `FordAPI` import, Ford API method name, or Ford-specific telemetry mapping.

### Startup compatibility

Startup calls `create_providers(cfg)`, preserves the existing no-token error and exit status, and passes the result to `Poller`. Authentication still calls the existing Ford auth flow because multi-provider authentication is outside PR 1.

## File ownership

| File | Change |
|---|---|
| `fordlogger/providers/__init__.py` | Export only the provider-neutral public contract without eagerly importing Ford dependencies. |
| `fordlogger/providers/base.py` | Define contract and capabilities. |
| `fordlogger/providers/ford.py` | Implement thin Ford adapter. |
| `fordlogger/providers/factory.py` | Implement backward-compatible Ford discovery. |
| `fordlogger/poller.py` | Depend on provider methods and terminology. |
| `fordlogger/main.py` | Use provider factory; remove API discovery. |
| `tests/providers/*` | Contract, adapter, and factory tests. |
| `tests/test_poller.py` | Replace Ford API mocks with a fake provider and add isolation coverage. |

Do not move or rewrite `fordlogger/api.py` or `fordlogger/auth.py`.

## Anti-patterns (DO NOT)

| Do not | Do instead | Reason |
|---|---|---|
| Add Zeekr code or dependency | Stop at the generic boundary and Ford adapter | The handoff explicitly limits the first implementation to PR 1. |
| Import `FordAPI` in `poller.py` | Type against `VehicleProvider` | The core must be provider-independent. |
| Rename `get_garage` across the Ford API client | Translate it only in `FordProvider.get_vehicles` | Avoid rewriting proven Ford behavior. |
| Move `api.py` or `auth.py` | Leave both files in place | Keeps the diff behavior-focused. |
| Mutate the caller's config while discovering tokens | Copy with `{**cfg, "token_file": ...}` | Multiple providers need independent token paths. |
| Catch provider errors inside the adapter and return malformed data | Preserve exceptions for `Poller` to isolate and log | Retains existing operational error behavior. |
| Add ORM, migration, messaging, or web frameworks | Keep current `psycopg2` and process model | No persistence or infrastructure change is required. |

## Test case specifications

### Unit tests

| ID | Component | Input | Expected output | Edge case |
|---|---|---|---|---|
| U-01 | `ProviderCapabilities` | No arguments | Both flags are false and the instance is immutable | Mutation raises `FrozenInstanceError`. |
| U-02 | `VehicleProvider` | Incomplete subclass | Instantiation fails | Contract methods cannot be omitted. |
| U-03 | `FordProvider.get_vehicles` | Wrapped client returning `Vehicle` list | Same list object/value is returned and `get_garage` called once | Empty list passes through. |
| U-04 | `FordProvider.get_telemetry` | Wrapped client returning `Position` list | Same list object/value is returned and API method called once | Empty list passes through. |
| U-05 | `FordProvider` configuration constructor | Ford configuration and patched offline client constructor | Configuration is forwarded and created client delegates normally | No network call. |
| U-06 | Factory per-VIN discovery | Unsorted `tokens_*.json` files in configured directory | Returned providers are ordered by filename and receive copied configs | Different empty working directory; caller config unchanged. |
| U-07 | Factory legacy discovery | Only configured legacy file exists | One provider using original configuration values | Non-default `token_file`. |
| U-08 | Factory empty discovery | No token files | Empty list | Token directory exists but is empty. |
| U-09 | Generic import boundary | Clean-process import of `providers.base` | Neither `fordlogger.api` nor `requests` is loaded | Package initializer remains provider-neutral. |
| U-10 | Generic core boundary | Import/AST inspection of `poller.py` | No Ford module/API import, `FordAPI`, `get_garage`, or legacy `.apis` dependency | Alias and attribute imports are detected. |

### Integration tests

| ID | Flow | Setup | Verification | Teardown |
|---|---|---|---|---|
| I-01 | Fake-provider poll | PostgreSQL test DB plus fake `VehicleProvider` | Vehicle and position rows persist | Existing autouse table cleanup. |
| I-02 | Fake-provider state transition | Parked then moving positions | State becomes `driving` and state row persists | Existing autouse table cleanup. |
| I-03 | Multiple-provider isolation | First provider telemetry raises; second returns a position | Second provider position persists and cycle does not raise | Existing autouse table cleanup. |
| I-04 | Provider metadata isolation | First provider's `name` property raises; second returns a position | Class-name fallback is logged and second provider position persists | Existing autouse table cleanup. |
| I-05 | Startup compatibility | Patched auth, DB, factory, and Poller boundaries | Auth bypass, no-provider exit 1, and `--once` wiring remain unchanged | No external calls. |
| I-06 | Ford behavior regression | Existing parser, state, trip, charge, DB, and config suite | All pre-existing non-live tests pass | Existing suite fixtures. |

No CI test may call the live Ford API.

## Error handling matrix

| Failure | Detection | Required response | Fallback | Logging |
|---|---|---|---|---|
| Vehicle-list request fails | `get_vehicles()` raises | Continue the cycle | Still request telemetry from that provider | Error with provider name; no secrets/raw payload. |
| Telemetry request fails | `get_telemetry()` raises | Skip that provider's positions | Continue with the next provider | Error with provider name and traceback. |
| Provider diagnostic name fails | `provider.name` raises | Use provider class name | Continue both provider calls and later providers | Safe warning without provider payload or credentials. |
| Database health check fails | Existing `_ensure_db()` path | Attempt current reconnect flow | Abort only the current cycle if reconnect fails | Preserve existing messages. |
| No token files | Factory returns empty list | Startup exits with status 1 | User runs existing auth command | Preserve existing user-facing message. |
| Invalid provider result | Iteration or position processing raises | Do not fabricate or normalize malformed models | Let the existing downstream exception surface; later providers are not guaranteed to run | No provider-boundary log is added in PR 1. |
| Ford API authentication/rate limit | Existing `FordAPI` raises or retries | No adapter-specific behavior | Existing `FordAPI` policy | Preserve current API logging. |

## Verification commands

Run from the repository root with the local PostgreSQL service healthy:

```text
FORDLOGGER_DB_HOST=definitely-invalid venv/bin/python -m pytest tests/providers tests/test_main.py -v
venv/bin/python -m pytest tests/test_poller.py -v
venv/bin/python -m pytest tests/ --ignore=tests/test_live_api.py -v
SKIP_LIVE_TESTS=1 venv/bin/python -m pytest tests/test_live_api.py -v
venv/bin/python -m compileall -q fordlogger tests
docker compose config
docker compose build fordlogger
git diff --check
```

The live API suite is excluded because it requires private Ford credentials and is not part of CI-safe verification.

## Clarity gate record

All 13 Stream Coding clarity checks pass for this executable PR 1 slice:

| Check group | Result | Evidence |
|---|---|---|
| Actionable, current, decision-focused, prompt-ready | Pass | Required behavior and file ownership specify each code change. |
| Single source, no future-state or fluff | Pass | Later-provider behavior is excluded and linked back to the roadmap. |
| Document type and section placement | Pass | This file is marked Implementation and contains anti-patterns, tests, and error handling. |
| Deep links and no cross-document duplication | Pass | The ADR owns rationale; this document owns executable behavior and links to source sections. |

Weighted AI-coder understandability self-score: **9.8/10**.

| Criterion | Score | Rationale |
|---|---:|---|
| Actionability (25%) | 10.0 | Every requirement names behavior or an owned file. |
| Specificity (20%) | 9.5 | Interfaces, discovery order, failure isolation, and exclusions are explicit. |
| Consistency (15%) | 10.0 | Rationale remains in the ADR; executable details remain here. |
| Structure (15%) | 10.0 | Contract, tables, matrices, and checklists are separated by purpose. |
| Disambiguation (15%) | 10.0 | Seven anti-patterns and edge/error cases constrain implementation choices. |
| Reference clarity (10%) | 9.5 | Every repository reference includes a path and section or symbol. |

Self-assessment: every section is actionable; concrete values are used where PR 1 needs them; no decision is duplicated across operative documents; tables replace repeated prose; more than five anti-patterns are explicit; no vague “see elsewhere” reference remains.

## Implementation evidence

| Evidence | Result |
|---|---|
| Implementation commits | `c3bcb15`, `c497216`, `ff318dc`, `97faaa0` |
| Provider/startup tests without PostgreSQL | 13 passed |
| Poller tests with PostgreSQL 16 | 12 passed |
| Complete non-live regression suite | 86 passed |
| Credentialed live module safety check | 10 collected, 10 skipped with `SKIP_LIVE_TESTS=1`; no live calls made |
| Generic import boundary | `fordlogger.providers.base` loaded neither `fordlogger.api` nor `requests` |
| Python compile | Passed |
| Docker Compose render | Passed |
| Docker image build | Passed |
| Diff whitespace and scope checks | Passed; only provider, poller, startup, and tests changed |

## Acceptance checklist

- [x] Provider contract and immutable default capabilities exist.
- [x] Ford adapter delegates without remapping or behavior changes.
- [x] Factory preserves per-VIN and legacy token discovery.
- [x] Poller has no direct Ford dependency.
- [x] Startup uses providers and preserves the legacy no-token path.
- [x] Fake-provider persistence and state tests pass.
- [x] Vehicle-list, telemetry-request, and diagnostic-name failures are isolated across multiple providers.
- [x] All 69 baseline non-live tests plus 17 new tests pass (86 total).
- [x] Docker image builds, Compose renders, and diff checks pass.
- [x] No Zeekr support, schema change, package rename, or infrastructure rewrite is present.

## References

| Topic | Location | Section |
|---|---|---|
| Architectural decision | [ADR 0001](adr/0001-vehicle-provider-boundary.md#decision) | Decision |
| Original PR 1 requirements | [Codex handoff](../zeekr_fordlogger_codex_handoff.md#pr-1--extract-vehicleprovider) | PR 1 |
| Explicit execution boundary | [Codex handoff](../zeekr_fordlogger_codex_handoff.md#first-codex-task) | First Codex task |
| Existing poll lifecycle | [`poller.py`](../fordlogger/poller.py) | `Poller.poll_once` |
| Current startup wiring | [`main.py`](../fordlogger/main.py) | `create_providers` call |
| Generic models | [`models.py`](../fordlogger/models.py) | `Vehicle` and `Position` |
