# Log Inspector project rules

- Python 3.11+, standard library only. Keep the tool usable from the command line.
- Test command from project root: `python checks/run_tests.py` (unified entry point; runs the full unittest suite and exits non-zero on failure or zero tests). The underlying suite also runs via `python -m unittest discover -s tests -v`.
- Use only synthetic logs in tests. Do not read or upload private production logs.
- Input timestamps must include timezone information; compare absolute times.
- Malformed nonblank lines must be reported, never silently treated as valid logs.
- Preserve source order. Keep stdout for results, stderr for diagnostics.
- Do not add an intentionally broken implementation for Harness demonstrations.
- Implement one user-facing feature at a time and preserve existing regressions.
