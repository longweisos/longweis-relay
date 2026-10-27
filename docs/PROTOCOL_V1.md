# Public client protocol v1

The auditable client identifies ordinary Bridge requests with:

```text
client_protocol = longweis-relay-public-client/1
```

This field is a safety and billing boundary. A service that does not implement
the exact protocol must reject the request during validation, before executing
work or creating a charge. The client then calls the user-selected provider
directly with the original messages.

## Allowed outcomes

The service may return only one of these public actions:

- `return_relay_final`: return a completed Relay response and a non-empty
  charge receipt.
- `call_premium_direct`: call the selected provider locally with the original
  messages. Its Relay charge must be `null`.

The public client does not accept server-supplied provider prompts, rewritten
messages, routing explanations, thresholds, or private policy state.

## Credential boundary

- The Relay credential is sent only to `https://api.longweisos.com`.
- The premium-provider credential remains in the operating-system credential
  store and is sent only to the user-selected provider.
- The device-local API credential is accepted only by the loopback listener.

## Compatibility rule

Adding a new outcome or changing the meaning of an existing field requires a
new protocol identifier. Unknown fields and unknown outcomes fail closed.
Every successful response must echo the exact `client_protocol` value.
