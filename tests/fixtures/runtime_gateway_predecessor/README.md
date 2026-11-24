# Historical gateway source

These files preserve the gateway implementation at revision `84d6a59` for
predecessor intake, authorization, receipt and retirement regression tests.
They are test source and are excluded from packaged product payloads.

Do not update this source to match the successor implementation. The manifest
records the exact historical bytes. Tests load it under a separate namespace;
ordinary stable-origin behavior is covered by `test_runtime_edge.py`,
`test_runtime_gateway_retirement.py`, `test_macos_runtime_updates.py`, and the
ordinary composer submission tests. Exact signed affected-build qualification
continues to use the independent retained signed package, not this fixture.
