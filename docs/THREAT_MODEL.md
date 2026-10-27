# Client threat model

## Assets

- Relay account credential.
- User-selected premium-provider credential.
- Device-local API credential.
- Prompts, tool calls, selected memory imports, and final responses.

## Trust boundaries

1. Tools on the user's computer call the loopback-only local API.
2. The local client sends the bounded Relay request to
   `https://api.longweisos.com`.
3. When required, the local client calls the user's selected premium provider
   directly. The premium credential is not included in the Relay request.
4. Optional selected memory material is sent to Relay only after an explicit
   user action.

## Defensive properties

- No credential is stored in the profile JSON.
- No credential is accepted in a command-line argument.
- Local API binding is restricted to `127.0.0.1`.
- Relay URLs reject credentials, query strings, fragments, and nonstandard
  public origins.
- Public registration that unexpectedly requests restricted access fails
  closed.
- Responses and downloads are bounded before parsing or writing.

## Out of scope

- A compromised operating system or credential store.
- A malicious premium provider selected by the user.
- Customer material intentionally submitted to Relay or a selected provider.
- The implementation of the hosted Relay optimization service.
