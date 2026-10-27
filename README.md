# Longweis Relay

**Keep the premium AI workflow you trust while making long-running work more
compute-efficient.**

This repository is the public home of Longweis Relay and its auditable local
client. The hosted optimization service remains separate; this source does not
contain its memory construction, execution routing, thresholds, private prompts,
provider configuration, calibration data, credentials, or private evaluation
material.

## Open testing

Download Relay and register with Email or Google—no application, invitation, or
activation code is required. A newly registered account receives a one-time
**$10 Relay service credit**.

The application is optional. A valid application submitted with the same
registered email earns one additional one-time **$50 Relay service credit**
after account/application matching.

Relay service credit is not cash, has no cash value, and does not cover charges
from a user's separate premium provider.

**[Download and start open testing](https://relay.longweisos.com/?utm_source=github&utm_medium=community&utm_campaign=open_testing)**

## What this source lets you verify

- Relay traffic is restricted to `https://api.longweisos.com`.
- Premium-provider credentials are used only for direct requests from the
  user's computer to the provider selected by the user.
- Relay, premium-provider, and device-local credentials stay in native
  operating-system credential storage, not the profile file.
- The local OpenAI-compatible API binds only to literal IPv4 loopback.
- Redirects are rejected before sensitive credentials or selected memory can
  be forwarded.
- The public client contains no hosted implementation or private memory engine.

## Public protocol v1

Ordinary requests identify the exact contract:

```text
longweis-relay-public-client/1
```

The client accepts only:

- a completed Relay response with a non-empty charge receipt; or
- a pre-execution direct-premium fallback with no Relay charge.

Unknown protocol versions, unknown outcomes, and malformed billing state fail
closed. See [the protocol](docs/PROTOCOL_V1.md) and
[threat model](docs/THREAT_MODEL.md).

## Client development and verification

The v0.1.0 candidate supports Python 3.9 or later and has no runtime package
dependencies.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/audit_public_tree.py
PYTHONPATH=src python3 -m longweis_relay_client doctor --offline
PYTHONPATH=src python3 -m longweis_relay_client egress
PYTHONPATH=src python3 -m longweis_relay_client doctor
```

Configuration, registration, and live requests remain explicit user actions.
The audit commands do not read or print credentials.

## Compatibility status

Verified compatibility currently covers:

- OpenClaw
- OpenCode 1.18.30
- Aider 0.82.3
- AutoGen 0.7.5

OpenHands 0.9.8 coverage is expanding; it is not yet claimed as supported.

## Measured compute ranges

Relay has reached up to about **50% less premium-model compute** on suitable
everyday work and up to about **90% less total model compute** on suitable
long-memory work. These are verified upper ranges, not guarantees. Results
depend on the task, selected premium model, usable history, and measured Relay
participation.

## Releases and contributions

The local v0.1.0 candidate has deterministic wheel/source archives, SHA-256
checksums, an SPDX SBOM, clean-install verification, and CI gates. No package
release is represented as published until its tag and artifacts are visible in
this repository.

Read [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the
[release process](docs/RELEASE.md) before contributing or testing an archive.

## Support

Use [SUPPORT.md](SUPPORT.md) for public-client questions. Report security,
privacy, billing, refund, commercial, or media matters privately to
`longweis.app@gmail.com`.

Apache License 2.0. Longweis Relay service use is governed separately by its
service terms.
