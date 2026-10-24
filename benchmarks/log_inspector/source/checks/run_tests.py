#!/usr/bin/env python3
"""Unified test entry point for Log Inspector.

Run from the project root: ``python checks/run_tests.py``.

Discovers and runs the full unittest suite under ``tests/`` and exits non-zero
when any test fails/errors or when no tests are discovered.
"""

import sys
import unittest
from pathlib import Path

# Executing a file sets sys.path[0] to checks/, not the project root.
# The runner must work without inherited PYTHONPATH from an Agent environment.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    failed = len(result.failures) + len(result.errors)
    if result.testsRun == 0:
        print('未发现任何测试。', file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(1 if failed else 0)
