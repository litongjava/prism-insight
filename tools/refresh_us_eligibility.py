#!/usr/bin/env python3
"""Refresh public metadata before the local paper analysis batch."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prism_core.nasdaq_eligibility import refresh


def main():
    path = os.environ.get('US_ELIGIBILITY_CACHE_PATH')
    if not path:
        raise SystemExit('Explicit US metadata cache path required')
    try:
        print(json.dumps(refresh(path)), flush=True)
    except Exception:
        raise SystemExit('Nasdaq metadata refresh failed; analysis not started') from None


if __name__ == '__main__':
    main()
