# Changelog

All notable public-client changes are recorded here. The hosted Relay service
has an independent release lifecycle.

## 0.1.0 — 2026-10-27

- Added the fail-closed `longweis-relay-public-client/1` contract.
- Required a charge receipt for a Relay-final response.
- Required a null Relay charge for pre-execution direct-provider fallback.
- Rejected redirects before forwarding Relay credentials or selected memory.
- Restricted public Relay traffic to `https://api.longweisos.com`.
- Kept Relay, premium-provider, and local API credentials in native operating
  system credential storage.
- Added secret-free online/offline diagnostics and an explicit egress report.
- Certified production protocol advertisement on 2026-10-27.

This entry does not claim that every task uses Relay or achieves a fixed saving.
