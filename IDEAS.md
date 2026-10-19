# Future Development Ideas

Collecting data first: these are planned for later. zeekrmate is read-only by design; it never sends commands to the car.

## High value, low effort
- ~~**Reverse geocoding**~~: **Done.** Trips and charge sessions have addresses via Nominatim (optional).
- **Charge cost tracking**: add an electricity price (c/kWh, with time-of-use tariffs) to the config and calculate the cost per charge session and per month.
- **Grafana alerting**: charge complete, low SoC, unexpected movement (Grafana's built-in alerting to email or Telegram).

## Medium effort
- **Login and session monitoring**: warn when the Zeekr login or session refresh keeps failing, before data is silently lost.
- **Health/uptime panel**: show polling gaps, API errors and the last successful poll.
- **Data retention policy**: downsample old positions (for example 1-minute data for 90 days, then 5-minute).
- **Zeekr journey history**: reconcile locally detected trips with the car's own journey log, and import its trackpoints for the map.

## Larger features
- ~~**Multi-vehicle support**~~: **Done.** Dashboards have a Vehicle drop-down and the poller handles several VINs.
- **Companion web UI**: status, configuration and manual actions such as an address backfill.
- **Fleet comparison**: a central server that aggregates anonymised, opt-in statistics (no VIN, no GPS: model, monthly consumption, capacity trend, charge speeds, rough region) and shows "your car vs fleet" percentiles. Needs enough users to be useful.
