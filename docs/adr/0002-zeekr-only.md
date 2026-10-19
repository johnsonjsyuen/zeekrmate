# ADR 0002: Zeekr-only (Ford support removed)

- Status: Accepted
- Date: 2026-10-19
- Supersedes in part: [ADR 0001](0001-vehicle-provider-boundary.md) (its Ford consequences only)

## Context

The project began as a fork of [FordLogger](https://github.com/lutzerb/fordlogger), with a plan ([Codex handoff](../../zeekr_fordlogger_codex_handoff.md)) to add a Zeekr 7X provider **while preserving Ford support** behind a multi-provider boundary (ADR 0001).

The only user is an Australian Zeekr 7X owner. Nobody runs the Ford path, and there are no existing installations or databases to migrate. Keeping Ford would mean maintaining an OAuth client, token files, Ford status vocabularies in the generic state machine and Grafana, provider columns in the schema, and Ford-only tests and documentation, none of which would ever be exercised.

## Decision

1. **Zeekr is the only supported vehicle.** All Ford code, configuration, CLI flags (`--auth`), scripts, tests and documentation are removed.
2. **Keep the provider boundary** from ADR 0001 (`VehicleProvider` with `get_vehicles()` and `get_telemetry()`). Zeekr-specific payloads still stop at the provider; the poller, state machine, detectors, schema and dashboards only see generic fields.
3. **No multi-provider persistence.** The handoff's PR 2 `provider` / `provider_vehicle_id` columns are dropped. The schema keeps provider-neutral additions only (such as observation time and snapshot keys for deduplication).
4. **Schema baseline is edited directly.** Because no databases exist yet, `sql/schema.sql` is changed in place; a migration mechanism is only needed for changes after the first real install.
5. **Generic state signals.** The state machine uses provider-neutral signals (`is_driving`, `is_charging`, speed, power) instead of Ford status strings. Grafana uses `positions.state` (`driving`, `charging`, `parked`, `sleeping`).
6. **Naming.** User-facing names (README, dashboard titles, folder) become *zeekrmate*. The Python package, database name and user, Compose service and dashboard/datasource uids stay `fordlogger` until a separate rename commit.
7. **Licence and attribution are unchanged in substance.** The work stays AGPL-3.0, upstream history is kept, and a [NOTICE](../../NOTICE) records the attribution to FordLogger by lutzerb and the 2026 modifications (AGPL-3.0 section 5(a)).
8. **Read-only towards the car.** zeekrmate never sends commands to the vehicle.

## Consequences

- The handoff's "preserve Ford" directives no longer apply; its Zeekr guidance (capture first, timestamp policy, deduplication, journey reconciliation, charging refinement, Grafana adaptation) still does.
- ADR 0001's consequences about Ford authentication, token discovery and `FordProvider` are void. Its boundary decision stands.
- Re-adding another make later would need a new ADR covering provider identity in the schema.
- Dashboards and documentation can assume Zeekr units and vocabulary once confirmed from real captures. Until then, panels that rely on unconfirmed fields are flagged.
- The Zeekr app keys required by `zeekr_ev_api` are a user-supplied prerequisite. The project neither ships them nor documents how to obtain them.

## Rejected alternatives

| Alternative | Reason rejected |
|---|---|
| Keep Ford as a dormant second provider | Untested code paths, Ford strings in generic logic, and schema columns with no user. |
| Rename the package and database in the same change | Large mechanical diffs would hide the behavioural changes; done separately later. |
| Squash or re-root history to drop upstream commits | Upstream history is part of the attribution. |

## References

| Topic | Location |
|---|---|
| Provider boundary | [ADR 0001](0001-vehicle-provider-boundary.md) |
| Original roadmap | [Codex handoff](../../zeekr_fordlogger_codex_handoff.md) |
| Attribution | [NOTICE](../../NOTICE), [README](../../README.md#attribution-and-licence) |
