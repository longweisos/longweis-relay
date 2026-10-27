## Summary

Describe the public-client behavior being changed and why.

## Boundary review

- [ ] No credential, customer content, private prompt, provider configuration,
      hosted execution logic, routing threshold, or private evaluation material.
- [ ] New network destinations or persistent fields are documented and tested.
- [ ] Protocol changes use a new identifier or preserve v1 semantics exactly.
- [ ] Failure paths remain pre-execution and fail closed where required.

## Verification

- [ ] Full unit suite passes.
- [ ] Whole-tree public audit passes.
- [ ] Offline doctor and egress inventory pass.
- [ ] Release archive checks pass when packaging is affected.
