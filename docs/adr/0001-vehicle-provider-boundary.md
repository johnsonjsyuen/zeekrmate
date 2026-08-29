# ADR 0001: Vehicle Provider Boundary (Strategic)

- Status: Accepted
- Date: 2026-08-29
- Scope: PR 1 only

## Context

At baseline `ff1ffb758d056b4d8bd96a35364877939bdaf442`, `Poller` and startup discovery constructed and called `FordAPI` directly. That coupling prevented another vehicle service from supplying the existing generic `Vehicle` and `Position` models without leaking provider-specific API details into polling, persistence, state detection, trip detection, or charge detection.

**Implementation implication:** startup must produce provider objects, and `Poller` must consume only the provider contract.

## Decision

Introduce `VehicleProvider` with exactly two required operations:

- `get_vehicles() -> list[Vehicle]`
- `get_telemetry() -> list[Position]`

Add immutable `ProviderCapabilities`, defaulting both `journey_history` and `trip_trackpoints` to `False`. Implement `FordProvider` as a thin adapter around the existing `FordAPI`. Preserve the legacy Ford token-file discovery rules while changing their result from API clients to provider objects.

**Implementation implication:** `fordlogger/poller.py` must not import or name `FordAPI`; all Ford API access stays in `fordlogger/providers/ford.py`.

## Consequences

- Ford authentication and telemetry mapping remain unchanged.
- Existing configuration, SQL schema, CLI flags, Docker Compose, and Grafana behavior remain unchanged.
- Later providers can map external payloads into the existing domain models.
- Later capabilities can be advertised without adding unused abstract methods now.
- Provider vehicle-list, telemetry-request, and diagnostic-name failures retain per-provider isolation in the polling loop.

## Rejected alternatives

| Alternative | Reason rejected |
|---|---|
| Rename or move `api.py` and `auth.py` now | Large move-only diffs obscure the provider-boundary behavior change. |
| Put provider conditionals in `Poller` | Leaks external API details into the generic core. |
| Add journey and trackpoint methods in PR 1 | Those capabilities are not implemented in this slice. |
| Replace persistence or orchestration technology | Outside PR 1 and unnecessary for the boundary. |

## Success measures

- A fake non-Ford provider drives vehicle persistence, position persistence, and state transitions through `Poller`.
- `FordProvider` returns exactly the `FordAPI` results it wraps.
- Legacy token discovery returns Ford providers in deterministic filename order and retains `tokens.json` fallback.
- All existing non-live tests pass unchanged in observable behavior.
- The Docker image builds and Compose configuration renders.

## References

| Topic | Location | Section |
|---|---|---|
| Executable behavior | [PR 1 implementation specification](../pr1-vehicle-provider-spec.md#required-behavior) | Required behavior |
| Source handoff | [Codex handoff](../../zeekr_fordlogger_codex_handoff.md#pr-1--extract-vehicleprovider) | PR 1 |
| Current polling loop | [`poller.py`](../../fordlogger/poller.py) | `Poller` |
| Current startup wiring | [`main.py`](../../fordlogger/main.py) | `create_providers` call |
