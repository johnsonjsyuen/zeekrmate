# Contributing to zeekrmate

Thanks for your interest in contributing!

zeekrmate is a Zeekr-only fork of [FordLogger](https://github.com/lutzerb/fordlogger) (see [NOTICE](NOTICE)). The Python package and database are still named `zeekrmate`; a rename will come as a separate change.

## Getting started

1. Fork the repository.
2. Clone your fork and set up the development environment:

```bash
git clone https://github.com/YOUR_USERNAME/zeekrmate.git
cd zeekrmate
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt pytest
```

3. Start a PostgreSQL 18 for the tests. The Compose `db` service does not publish a host port, so either run your own PostgreSQL or temporarily uncomment the loopback port mapping in `docker-compose.yml`.

## Running tests

Tests use a separate `zeekrmate_test` database, never the real `zeekrmate` database:

```bash
ZEEKRMATE_DB_HOST=localhost ZEEKRMATE_DB_PORT=5432 python -m pytest tests/
```

The test suite must not contact the Zeekr API or any other network service. Zeekr mapping tests use sanitised captures stored in the repository.

Also check that the Compose file still renders (with a throw-away `.env` that sets the required passwords):

```bash
docker compose config > /dev/null
```

## Pull requests

- Create a feature branch from `main`.
- Add tests for new behaviour.
- Make sure all tests pass before submitting.
- Keep pull requests focused: one feature or fix per pull request.
- Keep Zeekr-specific code behind the provider boundary in `zeekrmate/providers/` (see [ADR 0001](docs/adr/0001-vehicle-provider-boundary.md) and [ADR 0002](docs/adr/0002-zeekr-only.md)).
- When you change a dashboard query, check it runs against a database built from `sql/schema.sql`.

## Secrets and captured data

- Never commit `.env`, `config.json`, session files, Zeekr credentials or app keys.
- Do not add instructions or tooling for obtaining the Zeekr app keys. Users supply them as required by `zeekr_ev_api`.
- Sanitise captures before committing them: remove VINs, tokens, user IDs, exact home coordinates and anything else that identifies a person or car.
- Never paste unsanitised logs in issues. At DEBUG level `zeekr_ev_api` can log request headers that contain tokens.

## Reporting issues

Please include:

- steps to reproduce,
- relevant, sanitised log output (`docker compose logs zeekrmate`),
- your car's model, variant and market, if relevant.

## Zeekr API notes

If you find out how a Zeekr API field behaves (units, update frequency, values while asleep), please document it in `docs/` with a sanitised example.

## Licence

By contributing, you agree that your contributions will be licensed under the [AGPL-3.0](LICENSE) licence.
