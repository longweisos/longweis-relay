# Security policy

Please report suspected credential exposure, authentication flaws, unsafe
egress, dependency substitution, or release-integrity problems privately to
`longweis.app@gmail.com`.

Do not include live credentials, account keys, private prompts, customer data,
or exploit payloads in a public issue.

The client is designed to fail closed when the public registration state or
service origin differs from the audited contract. The supported Relay service
origin is `https://api.longweisos.com`; literal loopback HTTP is accepted only
for local development and tests.
