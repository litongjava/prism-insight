"""US paper trading via moomoo REST; independent of KIS and the live ledger.

Only the sim-trade API namespace is reachable through this adapter. A successful
submission is an acknowledgement, never evidence of a fill. No request retries.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

API_HOST = "https://webapi.moomoo.com"
PAPER_PREFIX = "/api/v1.0/sim-trade/"
STATE_DIR = Path.home() / ".local/state/prism-moomoo"
REDIRECT_URI = "http://localhost:60355/callback"


class MoomooError(RuntimeError):
    """Safe diagnostic without tokens or untrusted server response bodies."""


class OutcomeUnknown(MoomooError):
    """An order write may have reached the broker; reconcile before resubmitting."""


def save_private(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".moomoo-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_private(path: Path) -> dict:
    try:
        mode = path.stat().st_mode
        if path.is_symlink() or not stat.S_ISREG(mode) or mode & 0o077:
            raise MoomooError("Credential file must be a private regular file (chmod 600).")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise MoomooError("Credentials missing or invalid; run the login command.") from None


@contextmanager
def credential_lock(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "oauth.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MoomooError("Another process is updating credentials; try again later.") from None
        yield
    finally:
        os.close(fd)


async def oauth_request(client: httpx.AsyncClient, path: str, **kwargs) -> dict:
    try:
        response = await client.post(API_HOST + path, follow_redirects=False, timeout=20, **kwargs)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("error"):
            raise ValueError
        return value
    except (httpx.HTTPError, ValueError):
        raise MoomooError("moomoo authorization failed; restart login if needed.") from None


class OAuthSession:
    def __init__(self, client: httpx.AsyncClient, directory: Path = STATE_DIR):
        self.client = client
        self.directory = directory
        self._lock = asyncio.Lock()

    async def begin_login(self) -> str:
        with credential_lock(self.directory):
            registration_path = self.directory / "oauth-client.json"
            if registration_path.exists():
                registration = read_private(registration_path)
            else:
                registration = await oauth_request(self.client, "/oauth2/register", json={
                    "redirect_uris": [REDIRECT_URI],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "client_name": "PRISM moomoo paper trading",
                })
                if not registration.get("client_id"):
                    raise MoomooError("Registration did not return a client ID.")
                save_private(registration_path, registration)
            verifier = secrets.token_urlsafe(48)
            state = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            save_private(self.directory / "pending-login.json", {
                "state": state, "verifier": verifier,
                "client_id": registration["client_id"], "created_at": time.time(),
            })
        return API_HOST + "/oauth2/authorize/confirm?" + urlencode({
            "client_id": registration["client_id"], "redirect_uri": REDIRECT_URI,
            "response_type": "code", "code_challenge_method": "S256",
            "code_challenge": challenge, "state": state,
            "scope": "trade:read trade:write",
        })

    async def finish_login(self, callback: str) -> None:
        with credential_lock(self.directory):
            pending_path = self.directory / "pending-login.json"
            pending = read_private(pending_path)
            url = urlsplit(callback)
            params = parse_qs(url.query)
            expected = urlsplit(REDIRECT_URI)
            if (url.scheme != expected.scheme or url.netloc != expected.netloc
                    or url.path != expected.path or url.fragment
                    or len(params.get("state", [])) != 1
                    or len(params.get("code", [])) != 1
                    or not secrets.compare_digest(params["state"][0], pending["state"])
                    or not 0 <= time.time() - pending["created_at"] <= 1200):
                raise MoomooError("Invalid or expired authorization callback.")
            if "error" in params:
                raise MoomooError("Authorization was declined.")
            # Codes are single-use. Never replay an exchange with an unknown result.
            pending_path.unlink()
            tokens = await oauth_request(self.client, "/oauth2/token", data={
                "grant_type": "authorization_code", "code": params["code"][0],
                "client_id": pending["client_id"], "redirect_uri": REDIRECT_URI,
                "code_verifier": pending["verifier"],
            })
            self._save_tokens(tokens, pending["client_id"])

    def _save_tokens(self, tokens: dict, client_id: str) -> None:
        try:
            lifetime = float(tokens["expires_in"])
            if (not 0 < lifetime <= 86400 or not isinstance(tokens.get("access_token"), str)
                    or not tokens["access_token"]):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise MoomooError("Authorization returned invalid token metadata.") from None
        save_private(self.directory / "tokens.json", {
            **tokens, "client_id": client_id, "expires_at": time.time() + lifetime,
        })

    async def access_token(self) -> str:
        async with self._lock:
            with credential_lock(self.directory):
                tokens = read_private(self.directory / "tokens.json")
                if tokens.get("expires_at", 0) <= time.time() + 60:
                    if not tokens.get("refresh_token"):
                        raise MoomooError("Session expired; run login again.")
                    refreshed = await oauth_request(self.client, "/oauth2/token", data={
                        "grant_type": "refresh_token", "client_id": tokens["client_id"],
                        "refresh_token": tokens["refresh_token"],
                    })
                    # Some providers omit the refresh token when it is unchanged.
                    refreshed.setdefault("refresh_token", tokens["refresh_token"])
                    self._save_tokens(refreshed, tokens["client_id"])
                    tokens = read_private(self.directory / "tokens.json")
                token = tokens.get("access_token")
                if not isinstance(token, str) or not token:
                    raise MoomooError("Access token missing; run login again.")
                return token


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise MoomooError("Invalid paper account or order ID.")
    return value


def limit_order(symbol: str, side: str, quantity: int, price: str) -> dict:
    symbol = symbol.upper().removeprefix("US.")
    if not re.fullmatch(r"[A-Z]{1,5}(?:[.-][A-Z])?", symbol):
        raise MoomooError("Only US stock/ETF ticker symbols are supported.")
    if side not in {"BUY", "SELL"} or type(quantity) is not int or quantity <= 0:
        raise MoomooError("Use BUY or SELL and a positive whole-share quantity.")
    try:
        amount = Decimal(price)
        if not amount.is_finite() or amount <= 0:
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise MoomooError("Limit price must be finite and positive.") from None
    return {
        "market": 100, "symbol": "US." + symbol, "order_type": 1,
        "order_side": 1 if side == "BUY" else 2, "qty": str(quantity),
        "price": format(amount, "f"), "order_trade_time_type": 1,
    }


class MoomooPaperBroker:
    """Explicit paper-only interface; intentionally not a KIS drop-in replacement."""

    def __init__(self, client: httpx.AsyncClient, auth: OAuthSession):
        self.client = client
        self.auth = auth

    async def _request(self, method: str, resource: str, **kwargs) -> dict:
        if not re.fullmatch(r"accounts|[0-9]+/(?:cash-info|positions|orders(?:/[0-9]+/cancel)?)", resource):
            raise MoomooError("Only paper trading endpoints are allowed.")
        token = await self.auth.access_token()
        writing = method == "POST"
        try:
            response = await self.client.request(
                method, API_HOST + PAPER_PREFIX + resource,
                headers={"Authorization": "Bearer " + token},
                follow_redirects=False, timeout=20, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or type(payload.get("ret_code")) is not int:
                raise ValueError
            if payload["ret_code"] != 0:
                raise MoomooError(f"Paper API rejected request (code {payload['ret_code']}).")
            if not isinstance(payload.get("data"), dict):
                raise ValueError
            return payload["data"]
        except (httpx.HTTPError, ValueError):
            if writing:
                raise OutcomeUnknown("Paper order outcome unknown; query orders before any retry.") from None
            raise MoomooError("Paper API read failed; account data remains unknown.") from None

    async def accounts(self) -> list[dict]:
        rows = (await self._request("GET", "accounts")).get("accounts")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise MoomooError("Paper account list is missing or invalid.")
        return [row for row in rows if row.get("market_id") == 100 and row.get("account_type") == 1]

    async def select_account(self, account_id: str | None = None) -> str:
        rows = await self.accounts()
        if account_id is not None:
            rows = [row for row in rows if row.get("account_id") == identifier(account_id)]
        if len(rows) != 1:
            raise MoomooError("Select exactly one US personal paper account; create one in moomoo if absent.")
        return identifier(rows[0].get("account_id"))

    async def snapshot(self, account_id: str | None = None) -> dict:
        account = await self.select_account(account_id)
        cash = await self._request("GET", account + "/cash-info")
        positions = await self._request("GET", account + "/positions", params={"market": 100})
        orders = await self._request("GET", account + "/orders", params={"market": 100})
        if not isinstance(positions.get("positions"), list) or not isinstance(orders.get("orders"), list):
            raise MoomooError("Position/order data missing; not an empty account.")
        return {"broker": "moomoo", "environment": "SIMULATE", "currency": "USD",
                "account_id": account, "cash": cash, **positions, **orders}

    async def submit_limit(self, account_id: str, symbol: str, side: str,
                           quantity: int, price: str) -> dict:
        body = limit_order(symbol, side, quantity, price)
        account = await self.select_account(account_id)
        result = await self._request("POST", account + "/orders", json=body)
        try:
            order_id = identifier(result.get("order_id"))
        except MoomooError:
            raise OutcomeUnknown("Paper submission has no order ID; query orders before retrying.") from None
        return {"environment": "SIMULATE", "submission": "ACKNOWLEDGED",
                "order_id": order_id, "filled_quantity": None, "fill_status": "MISSING"}

    async def cancel(self, account_id: str, order_id: str) -> dict:
        identifier(order_id)
        account = await self.select_account(account_id)
        result = await self._request("POST", f"{account}/orders/{order_id}/cancel", json={"market": 100})
        if result.get("order_id") != order_id:
            raise OutcomeUnknown("Paper cancellation acknowledgement missing; query orders before retrying.")
        return {"environment": "SIMULATE", "cancellation": "ACKNOWLEDGED", "order_id": order_id}
