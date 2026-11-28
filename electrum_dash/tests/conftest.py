# Tests inherited from Dash Electrum that still use Dash addresses, keys, URIs
# and wallet files, so they fail against Firo network constants. They are
# marked xfail so CI stays green and catches new regressions; once a
# test is ported to Firo vectors it shows as XPASS and should be removed from the list.
import os

import pytest

_here = os.path.dirname(__file__)

# test_dash_tx imports Dash special-tx types (SPEC_SUB_TX_*) that Firo removed.
collect_ignore = ['test_dash_tx.py']

with open(os.path.join(_here, 'known_failures.txt')) as f:
    KNOWN_FAILURES = {line.strip() for line in f if line.strip()}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.nodeid.split('tests/', 1)[-1] in KNOWN_FAILURES:
            item.add_marker(pytest.mark.xfail(
                reason='Dash test vectors not yet ported to Firo', strict=False))
