#!/usr/bin/env python3
"""Read-only OpenD readiness check; never unlock or submit an order."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL", help="US stock quote to verify")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    from prism_core.moomoo_trading import MoomooStockTrading
    client = None
    try:
        client = MoomooStockTrading()
        positions = client.get_portfolio(strict=True)
        orders = client.get_unfilled_orders()
        quote = client.get_current_price(args.ticker, strict=True)
        print(json.dumps(dict(status="OK", broker="MOOMOO", mode=client.mode,
                              account="****", position_count=len(positions),
                              open_order_count=len(orders), ticker=args.ticker.upper(),
                              quote=quote["current_price"], quote_time=quote["observed_at"],
                              orders_submitted=0), ensure_ascii=False))
        return 0
    except Exception as error:
        # SDK exceptions can contain account numbers or authentication details.
        print(json.dumps(dict(status="UNVERIFIED", error_type=type(error).__name__,
                              orders_submitted=0)))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
