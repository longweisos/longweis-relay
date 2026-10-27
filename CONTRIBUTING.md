# Contributing

Keep contributions inside the auditable local-client boundary. Do not submit
hosted execution logic, memory construction, routing policy, private prompts,
provider configuration, credentials, customer content, or private evaluation
material.

Before opening a change:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/audit_public_tree.py
PYTHONPATH=src python3 -m longweis_relay_client doctor --offline
PYTHONPATH=src python3 -m longweis_relay_client egress
```

New network destinations, protocol outcomes, credential flows, or persistent
fields require an explicit threat-model update and tests that prove fail-closed
behavior. Never include a live credential or customer prompt in a test fixture,
issue, commit, or diagnostic output.

For vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a
public issue.
