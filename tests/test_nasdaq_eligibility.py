from datetime import datetime, timezone
import json

import pytest
from prism_core.nasdaq_eligibility import build_cache
from prism_core.us_stock_universe import eligibility_reason

N = 'Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares'
O = 'ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol'
F = 'File Creation Time: 1117202618:00|||||||'


def fixtures():
    nasdaq = '\n'.join([N, 'AAA|Acme Common Stock|Q|N|N|100|N|N',
                        'BBB|Beta Inc.|G|N|N|100|N|N', F])
    other = '\n'.join([O, 'CCC|Cee Common Stock|N|CCC|N|100|N|CCC', F])
    def row(symbol):
        return dict(symbol=symbol, name=f'{symbol} Common Stock', sector='Technology',
                    industry='Software', marketCap='5,000,000,000', lastsale='$10.00')
    downloads = {'NASDAQ':{'data':{'rows':[row('AAA'), row('BBB')]}},
                 'NYSE':{'data':{'rows':[row('CCC')]}}}
    return nasdaq, other, downloads


def build(*args):
    return build_cache(*args, captured_at=datetime.now(timezone.utc).isoformat())


def test_confirmed_common_only_and_existing_policy_still_applies():
    cache = build(*fixtures())
    assert set(cache) == {'AAA', 'CCC'}  # BBB has no confirmed directory security type.
    assert cache['AAA']['exchange'] == 'NMS'
    assert cache['CCC']['exchange'] == 'NYQ'
    assert cache['AAA']['source_asof'] is None
    assert eligibility_reason(cache['AAA'], 1e9) is None
    assert eligibility_reason(cache['AAA'], 6e9) == 'market_cap'


@pytest.mark.parametrize('field,value', [('sector',''), ('industry',None), ('marketCap','N/A'),
                                        ('marketCap','NaN'), ('lastsale','€10.00')])
def test_missing_data_is_not_filled(field,value):
    n,o,d = fixtures()
    d['NASDAQ']['data']['rows'][0][field] = value
    assert 'AAA' not in build(n,o,d)


def test_ambiguous_symbols_fail_closed():
    n,o,d = fixtures()
    d['NASDAQ']['data']['rows'].append(d['NASDAQ']['data']['rows'][0].copy())
    with pytest.raises(ValueError, match='Ambiguous'):
        build(n,o,d)


def test_real_us_cache_consumer_avoids_failed_yahoo_profiles(monkeypatch, tmp_path):
    # KR and US both own `cores`. Run the real US consumer in a fresh pytest
    # process instead of depending on the parent suite's import order.
    import os
    import subprocess
    import sys
    from pathlib import Path
    if os.getenv("PRISM_TEST_NASDAQ_US_CHILD") != "1":
        result = subprocess.run(
            [sys.executable, "-m", "pytest", __file__ + "::" +
             "test_real_us_cache_consumer_avoids_failed_yahoo_profiles", "-q"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PRISM_TEST_NASDAQ_US_CHILD":"1", "PRISM_DISABLE_SIGNAL_PUBLISH":"1"},
            capture_output=True, text=True, timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    from test_us_eligibility_metadata_cache import _run
    cache = build(*fixtures())
    diag, fetched, saved, kept = _run(monkeypatch, tmp_path,
        amounts={'AAA':1e8,'CCC':2e8}, cache=cache, fetch_error=('AAA','CCC'))
    assert fetched == []
    assert set(kept) == {'AAA','CCC'}
    assert diag['metadata_cache_hits'] == 2 and diag['metadata_status'] == 'COMPLETE'
