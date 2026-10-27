# Architecture

```text
developer tool
    |
    | OpenAI-compatible request + device-local key
    v
127.0.0.1 local client
    |                     \
    | bounded Relay data  \ direct premium request when required
    v                       v
api.longweisos.com       user-selected premium provider
```

The local client owns provider selection, provider credential storage, the
loopback API, and the direct premium-provider call. The hosted service owns its
private optimization and memory implementation. Neither the local API nor this
repository exposes private routes, thresholds, model policies, or memory
construction details.

The ordinary handoff is versioned and fails closed before execution when the
service does not support the client's exact contract. See
[PROTOCOL_V1.md](PROTOCOL_V1.md).
