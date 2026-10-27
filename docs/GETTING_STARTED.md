# Getting started with Longweis Relay

## 1. Download the current macOS release

Open the [Longweis Relay open-testing page](https://relay.longweisos.com/?utm_source=github&utm_medium=community&utm_campaign=open_testing),
download the current Apple-silicon DMG, and compare its SHA-256 checksum with
the value published on the page before opening it.

The current desktop release requires macOS 13 or later on Apple silicon.

## 2. Register

Register with Email or Google. No application, invitation, or activation code
is required. A newly registered account receives a one-time $10 Relay service
credit.

Relay service credit is not cash, has no cash value, and does not cover charges
from your separate premium provider.

## 3. Optionally apply for another $50

The application is optional and is not required to download, register, or use
Relay. Submit a valid application with the same email as the registered account.
After account/application matching, the account receives one additional
one-time $50 Relay service credit.

## 4. Configure General API

Choose the premium provider and exact model you already use. The premium key is
stored in macOS Keychain and is not sent to Longweis. Start the local API and
connect a compatible runtime to the loopback endpoint shown by the app.

## 5. Begin with one low-risk workflow

Use a repeatable, non-sensitive task first. Compare the direct result and the
Relay result using the same premium model and acceptance criteria. Evaluate
task completion, quality, retries, total usage, your premium-provider charge,
and the Relay charge together.

Do not use secrets, regulated records, or irreplaceable production data during
open testing.

## 6. Audit the public client

From a verified source checkout:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/audit_public_tree.py
PYTHONPATH=src python3 -m longweis_relay_client doctor
PYTHONPATH=src python3 -m longweis_relay_client egress
```

The online doctor performs public, unauthenticated checks and does not read or
print a credential.

## Need help?

For public-client support, follow [SUPPORT.md](../SUPPORT.md). For security,
privacy, billing, refunds, commercial terms, media requests, or uncertain
product facts, contact `longweis.app@gmail.com` privately.
