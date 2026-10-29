# Test feedback

The required integration gate remains:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

For faster local feedback, run intact discovery suites in up to six processes:

```sh
python3 -W error::ResourceWarning scripts/run_tests.py --jobs 6 \
  --report build/test-feedback/parallel.json
```

This runner needs only the Python standard library. Each process uses the same
`sys.executable`, interpreter flags, warning options, working directory, and inherited environment
(including HOME, package locations, PATH, and optional native-test configuration).
It performs full unittest discovery before running one top-level discovered
suite. Tests within a suite stay serial, with unittest's class/module fixtures
and subtests. Imported test classes can appear more than once in discovery;
those occurrences are retained. Test assertions and server/Swift/compiler
isolation are unchanged. JavaScript tests reached through Python wrappers run
as usual; this does not replace separate CI JavaScript checks.

To check equivalence on stable source and the same shell environment:

```sh
python3 -W error::ResourceWarning scripts/run_tests.py --jobs 1 \
  --report build/test-feedback/serial.json
python3 -W error::ResourceWarning scripts/run_tests.py --jobs 6 \
  --report build/test-feedback/parallel.json \
  --compare build/test-feedback/serial.json
```

The single-process mode runs the complete discovery suite in its original order.
Comparison requires both runs to pass and matches discovery and executed test
occurrences, outcomes, skip reasons, subtests, interpreter/package/environment
identity, and source SHA-256 maps. Environment values are hashed to avoid
recording credentials. Reports retain test failures and fixture errors; parallel
worker output is saved next to the report in `<report-stem>.logs/`.

Missing/crashed workers, timeouts, empty discovery, source edits during execution,
and comparison mismatches fail the command. The default timeout is 300 seconds
per parallel suite (`--timeout` overrides it). Interrupted parallel runs kill
only their own worker process groups, including test subprocesses, on Ctrl-C or
SIGTERM. Uncatchable signals such as SIGKILL cannot perform cleanup. Interactive
interpreter modes are unsupported. Reports fingerprint flags and `-X` options;
a worker option mismatch fails the integrity check.

Reported walltime includes source hashing and discovery, starting at the runner's
entry point. It excludes interpreter startup and imports of the runner itself.
Use an external wall clock around each command for a headline speed comparison.

Use `--pattern test_example.py` for focused feedback. A full speed comparison
needs a quiet timing window: do not overlap load benchmarks, builds, or other
full suites. Differences between discovery order and process isolation can
expose order-dependent tests; investigate mismatches rather than deleting tests
or accepting extra skips. Keep the canonical serial integration gate even when
the parallel check passes. Class/module setup skips that prevent discovered
test methods from starting deliberately fail the occurrence integrity check;
their skip reason remains in the report for diagnosis.
