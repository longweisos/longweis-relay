# Longweis Relay client v0.1.0

This is the first auditable public-client release candidate for Longweis Relay.

## What it establishes

- A fail-closed `longweis-relay-public-client/1` boundary.
- Native operating-system credential storage.
- A loopback-only OpenAI-compatible local API.
- A fixed Relay service origin at `https://api.longweisos.com`.
- Explicit egress inventory and secret-free online/offline diagnostics.
- Pre-execution fallback to the user-selected premium provider when the exact
  public protocol is unavailable or unsupported.
- A required charge receipt for every accepted Relay-final response.

## Production compatibility

On 2026-10-27, the production public API advertised the exact protocol, Email
and Google registration, open registration, the one-time `$10.00` signup Relay
service credit, and production top-ups. The installed v0.1.0 candidate returned
`ready` from online doctor without reading a credential.

## Boundaries

This release does not publish the hosted implementation, memory construction,
routing policy, thresholds, private prompts, provider configuration, or private
evaluation material. It does not promise a fixed saving or fitness for every
workload.

Release archives must be distributed together with `SHA256SUMS` and
`SBOM.spdx.json` from the normalized local release bundle.
