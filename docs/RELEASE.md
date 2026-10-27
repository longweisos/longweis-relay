# Release process

The public client and hosted Relay service are released independently. A client
release is acceptable only when the service advertises the exact protocol the
client sends.

## Required gates

1. The worktree contains only intended public-client changes.
2. The Python 3.9-compatible full suite passes.
3. `scripts/audit_public_tree.py` scans the complete public text tree and passes.
4. Wheel and source archives pass `scripts/verify_release.py`.
5. A clean virtual environment installs the wheel with `--no-deps` and passes
   `relay-client --version`, offline doctor, egress, and online doctor.
6. The release archives receive SHA-256 checksums before upload.
7. The tag, archives, checksums, and release notes all name the same version.

Build the normalized local release bundle with:

```bash
python3 scripts/build_release.py --output release/v0.1.0
```

The builder uses a fixed `SOURCE_DATE_EPOCH`, normalizes archive entry order,
ownership, permissions, and timestamps, verifies both archives, and writes
`SHA256SUMS`. Repeating it from the same source and build-tool versions must
produce identical archive hashes.

Publishing a Git repository, tag, package, or release is a separate owner action.
Building and verifying local artifacts does not publish them.

## v0.1.0 production compatibility evidence

On 2026-10-27, the public API health check passed and
`/v1/auth/methods` advertised `longweis-relay-public-client/1`, Email and Google
registration, open registration, the one-time `$10.00` signup service credit,
and production top-ups. The public client online doctor returned `ready`.

This verifies protocol availability, not a fixed saving or fitness for every
workload.
