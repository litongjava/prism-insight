#!/usr/bin/env python3
"""Authorize and inspect the isolated PRISM moomoo paper broker."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from trading.moomoo_paper import (
    MoomooError, MoomooPaperBroker, OAuthSession, OutcomeUnknown, REDIRECT_URI,
    STATE_DIR, limit_order,
)


async def login(auth: OAuthSession) -> None:
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    exchanging = False

    async def callback(reader, writer):
        nonlocal exchanging
        status, message = "400 Bad Request", "Invalid callback. Return to the terminal."
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            method, target, _ = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            if (method == "GET" and target.startswith("/callback?")
                    and not exchanging and not done.done()):
                exchanging = True
                try:
                    await auth.finish_login(REDIRECT_URI.split("/callback")[0] + target)
                except MoomooError as exc:
                    exchanging = False
                    message = "Authorization failed. Return to the terminal and restart login."
                    if not (auth.directory / "pending-login.json").exists():
                        done.set_exception(exc)
                else:
                    status, message = "200 OK", "moomoo connected. You may close this window."
                    done.set_result(None)
            body = message.encode()
            writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                          f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                          "Connection: close\r\n\r\n").encode() + body)
            await writer.drain()
        except (ValueError, UnicodeError, OSError, asyncio.TimeoutError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    # Bind before registration so port conflicts cannot invalidate an active login.
    server = await asyncio.start_server(callback, "127.0.0.1", 60355, limit=8192)
    async with server:
        url = await auth.begin_login()
        print("Open this official moomoo URL in your browser:", flush=True)
        print(url, flush=True)
        print("For initial paper setup: keep only Market Data; deselect Watchlists, Accounts & Orders, "
              "and Trade Execution. Do not select real accounts.", flush=True)
        print("If your browser is on another computer, forward port 60355 to this host first.", flush=True)
        print("Waiting up to 20 minutes for authorization; no orders will be sent.", flush=True)
        try:
            await asyncio.wait_for(done, 1200)
        except asyncio.TimeoutError:
            raise MoomooError("Login timed out; run login again.") from None
    print("Authorization saved privately. Run 'status' to verify your paper account.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--state-dir", type=Path, default=STATE_DIR)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Log in on moomoo's website; credentials stay local")
    commands.add_parser("accounts", help="List US personal paper accounts")
    status = commands.add_parser("status", help="Read-only account, position and order check")
    status.add_argument("--account-id")
    status.add_argument("--details", action="store_true", help="Print private paper account details locally")
    order = commands.add_parser("order", help="Preview a paper limit order; submit only with --submit")
    order.add_argument("--account-id", required=True)
    order.add_argument("--symbol", required=True)
    order.add_argument("--side", choices=["BUY", "SELL"], required=True)
    order.add_argument("--quantity", type=int, required=True)
    order.add_argument("--limit-price", required=True)
    order.add_argument("--submit", action="store_true")
    cancel = commands.add_parser("cancel", help="Preview cancellation; submit only with --submit")
    cancel.add_argument("--account-id", required=True)
    cancel.add_argument("--order-id", required=True)
    cancel.add_argument("--submit", action="store_true")
    return result


async def run(args) -> None:
    # A preview does not authenticate or reach the network.
    if args.command == "order" and not args.submit:
        print(json.dumps({"environment": "SIMULATE", "submitted": False,
                          "order": limit_order(args.symbol, args.side, args.quantity, args.limit_price)}))
        return
    if args.command == "cancel" and not args.submit:
        print(json.dumps({"environment": "SIMULATE", "submitted": False,
                          "action": "cancel", "order_id": args.order_id}))
        return
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        auth = OAuthSession(client, args.state_dir)
        broker = MoomooPaperBroker(client, auth)
        if args.command == "login":
            await login(auth)
            return
        if args.command == "accounts":
            result = await broker.accounts()
        elif args.command == "status":
            snapshot = await broker.snapshot(args.account_id)
            result = snapshot if args.details else {
                "broker": "moomoo", "environment": "SIMULATE", "connected": True,
                "account_suffix": snapshot["account_id"][-4:],
                "positions": len(snapshot["positions"]), "orders_today": len(snapshot["orders"]),
                "scheduled_trading": False,
            }
        elif args.command == "order":
            result = await broker.submit_limit(args.account_id, args.symbol, args.side,
                                               args.quantity, args.limit_price)
        else:
            result = await broker.cancel(args.account_id, args.order_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> int:
    try:
        asyncio.run(run(parser().parse_args()))
        return 0
    except OutcomeUnknown as exc:
        print(json.dumps({"success": False, "outcome_unknown": True, "message": str(exc)}), file=sys.stderr)
        return 3
    except MoomooError as exc:
        print(json.dumps({"success": False, "message": str(exc)}), file=sys.stderr)
        return 2
    except OSError:
        print("Local file or callback port unavailable; check permissions and port 60355.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
