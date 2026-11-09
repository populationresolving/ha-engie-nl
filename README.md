# ENGIE Nederland (unofficial) for Home Assistant

Brings your **Mijn ENGIE** data into Home Assistant: rates, contract, monthly meter readings and usage,
the expected annual statement, advance payment and invoices.

> **Unofficial.** ENGIE NL has no public API. This integration signs in the way the Mijn ENGIE website
> does (Okta) and reads the same JSON the website uses. It can break when ENGIE changes its site.
> Not affiliated with ENGIE.

## What you get
- **Rates** (all-in incl. VAT and energy tax): electricity normal/low, feed-in normal/low, gas per m³;
  fixed charges, network costs and energy tax credit per day.
- **Contract**: product, contract end, monthly advance payment, next meter-reading request, yearly estimates.
- **Meter readings** (ENGIE reads the smart meter about monthly) and **this month's usage**.
- **Bills**: expected annual statement, expected settlement, advised advance payment, open amount, latest invoice.
- **Long-term statistics** for the Energy dashboard: `engie_nl:electricity_low`, `engie_nl:electricity_normal`,
  `engie_nl:return_low`, `engie_nl:return_normal`, `engie_nl:gas` and matching `_cost` statistics.
  One step per meter reading (about monthly). Costs use the current tariff, so periods before a tariff change are
  approximate; fixed/daily charges are not included. ENGIE only lists the last two readings, so history builds up
  from the day you install it.

For live usage, use a P1 meter reader (e.g. HomeWizard P1, DSMR) instead.

## Install
Add this repository to the Home Assistant Marketplace (or HACS) as a custom repository (type: integration),
install **ENGIE Nederland (unofficial)**, restart, then *Settings → Devices & services → Add integration →
ENGIE Nederland*. Sign in with your Mijn ENGIE username and password. Accounts with a second sign-in step (MFA)
are not supported.

## Privacy and security
- The password is stored encrypted (Fernet); the key lives in a separate `.storage/engie_nl_key` file.
- Credentials, tokens and cookies are never logged. Diagnostics redact username, EANs and invoice references.
- Refreshes every 6 hours (15 minutes after a failure).

## Development
Tests for the HA-free parts (sign-in chain against a fake ENGIE, parsing, statistics maths):
`cd tests && python -m unittest` (needs `aiohttp`).
