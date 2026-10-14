#!/usr/bin/env python3
"""Run one deterministic group of backend test modules without omitting tests."""
import argparse
import hashlib
from pathlib import Path
import sys
import unittest


def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


def select(suite, shard, count):
    # Keep each module's fixtures together, including discovery failures and
    # imported test classes. Use ordinary discovery so future subpackages count.
    return unittest.TestSuite(test for test in cases(suite)
        if int.from_bytes(hashlib.sha256(type(test).__module__.encode()).digest()[:8], 'big') % count == shard)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard', type=int, required=True, help='Zero-based group number')
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--list', action='store_true', help='Print selected test IDs without running tests')
    args = parser.parse_args()
    if not 0 <= args.shard < args.count:
        parser.error('Require 0 <= shard < count')
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    suite = select(unittest.TestLoader().discover(str(root / 'tests')), args.shard, args.count)
    if args.list:
        print('\n'.join(test.id() for test in cases(suite)))
        return 0
    print(f'Backend group {args.shard + 1}/{args.count}: {suite.countTestCases()} tests', flush=True)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
