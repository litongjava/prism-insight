"""Broker boundary tests: no real networking, credentials, orders or publishing."""

import asyncio
import hashlib
import base64
import json
import stat
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from trading.moomoo_paper import (
    MoomooError, MoomooPaperBroker, OAuthSession, OutcomeUnknown,
    REDIRECT_URI, limit_order, read_private, save_private,
)
from tools.moomoo_paper import parser, run


def ok(data):
    return httpx.Response(200, json={"ret_code": 0, "data": data})


ACCOUNTS = {"accounts": [
    {"account_id": "123", "market_id": 100, "account_type": 1},
    {"account_id": "456", "market_id": 1, "account_type": 1},
    {"account_id": "789", "market_id": 100, "account_type": 2},
]}


def broker_case(tmp_path, handler, action):
    save_private(tmp_path / "tokens.json", {
        "access_token": "private-test-token", "expires_at": time.time() + 3600,
    })

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await action(MoomooPaperBroker(client, OAuthSession(client, tmp_path)))
    return asyncio.run(scenario())


def test_snapshot_uses_paper_account_market_and_preserves_missing(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "webapi.moomoo.com"
        assert request.url.path.startswith("/api/v1.0/sim-trade/")
        assert request.method == "GET"
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "accounts":
            return ok(ACCOUNTS)
        if path == "cash-info":
            return ok({"balance": "1000000", "realized_profit": None})
        assert request.url.params["market"] == "100"
        return ok({path: []})

    result = broker_case(tmp_path, handler, lambda broker: broker.snapshot())
    assert result["account_id"] == "123"
    assert result["cash"]["realized_profit"] is None
    assert len(seen) == 4


@pytest.mark.parametrize("accounts", [[], ACCOUNTS["accounts"][:1] * 2])
def test_missing_or_ambiguous_accounts_are_not_selected(tmp_path, accounts):
    with pytest.raises(MoomooError, match="exactly one"):
        broker_case(tmp_path, lambda req: ok({"accounts": accounts}), lambda b: b.select_account())


@pytest.mark.parametrize("account", ["456", "789", "999", "../trade", "123/../../trade"])
def test_non_us_competition_unknown_and_path_injection_accounts_rejected(tmp_path, account):
    seen = []

    def handler(request):
        seen.append(request.method)
        return ok(ACCOUNTS)

    with pytest.raises(MoomooError):
        broker_case(tmp_path, handler, lambda b: b.submit_limit(account, "SPY", "BUY", 1, "10"))
    assert "POST" not in seen


@pytest.mark.parametrize("symbol,qty,price", [
    ("AAPL261120C275000", 1, "1"), ("HK.00700", 1, "1"), ("SPY", 0, "1"),
    ("SPY", True, "1"), ("SPY", 1.5, "1"), ("SPY", 1, "NaN"),
    ("SPY", 1, "Infinity"), ("SPY", 1, "-1"),
])
def test_unsupported_order_inputs_rejected(symbol, qty, price):
    with pytest.raises(MoomooError):
        limit_order(symbol, "BUY", qty, price)


def test_order_acknowledgement_is_not_a_fill(tmp_path):
    def handler(request):
        if request.method == "GET":
            return ok(ACCOUNTS)
        assert request.url.path == "/api/v1.0/sim-trade/123/orders"
        assert json.loads(request.content) == {
            "market": 100, "symbol": "US.SPY", "order_type": 1,
            "order_side": 1, "qty": "1", "price": "10.50", "order_trade_time_type": 1,
        }
        return ok({"order_id": "321"})

    result = broker_case(tmp_path, handler, lambda b: b.submit_limit("123", "SPY", "BUY", 1, "10.50"))
    assert result["submission"] == "ACKNOWLEDGED"
    assert result["filled_quantity"] is None
    assert result["fill_status"] == "MISSING"


@pytest.mark.parametrize("failure", ["timeout", "malformed", "missing_id", "http500"])
def test_unknown_submission_never_retries(tmp_path, failure):
    writes = []

    def handler(request):
        if request.method == "GET":
            return ok(ACCOUNTS)
        writes.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-test-token", request=request)
        if failure == "malformed":
            return httpx.Response(200, text="private-test-token")
        if failure == "http500":
            return httpx.Response(500)
        return ok({})

    with pytest.raises(OutcomeUnknown) as error:
        broker_case(tmp_path, handler, lambda b: b.submit_limit("123", "SPY", "BUY", 1, "1"))
    assert "private-test-token" not in str(error.value)
    assert len(writes) == 1


def test_http200_business_error_is_not_success(tmp_path):
    with pytest.raises(MoomooError, match="code -5"):
        broker_case(tmp_path, lambda req: httpx.Response(200, json={"ret_code": -5}), lambda b: b.accounts())


def test_missing_positions_not_reported_as_zero(tmp_path):
    def handler(request):
        return ok(ACCOUNTS if request.url.path.endswith("accounts") else {})
    with pytest.raises(MoomooError, match="missing"):
        broker_case(tmp_path, handler, lambda b: b.snapshot())


def test_live_namespace_cannot_be_called(tmp_path):
    def handler(request):
        pytest.fail("Network request escaped boundary")
    with pytest.raises(MoomooError, match="Only paper"):
        broker_case(tmp_path, handler, lambda b: b._request("POST", "../trade/123/orders"))


def test_cancel_pins_paper_account_and_market(tmp_path):
    def handler(request):
        if request.method == "GET":
            return ok(ACCOUNTS)
        assert request.url.path == "/api/v1.0/sim-trade/123/orders/321/cancel"
        assert json.loads(request.content) == {"market": 100}
        return ok({"order_id": "321"})
    assert broker_case(tmp_path, handler, lambda b: b.cancel("123", "321"))["order_id"] == "321"


def test_oauth_pkce_callback_and_private_storage(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("register"):
            return httpx.Response(201, json={"client_id": "client"})
        body = parse_qs(request.content.decode())
        pending_verifier = verifier[0]
        assert body["code_verifier"] == [pending_verifier]
        return httpx.Response(200, json={
            "access_token": "secret", "refresh_token": "refresh", "expires_in": 7200,
        })

    verifier = []

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = OAuthSession(client, tmp_path)
            url = await auth.begin_login()
            params = parse_qs(urlsplit(url).query)
            pending = read_private(tmp_path / "pending-login.json")
            verifier.append(pending["verifier"])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier[0].encode()).digest()).rstrip(b"=").decode()
            assert params["code_challenge"] == [challenge]
            with pytest.raises(MoomooError):
                await auth.finish_login(REDIRECT_URI + "?code=secret&state=bad")
            assert len(calls) == 1
            await auth.finish_login(REDIRECT_URI + "?code=secret&state=" + pending["state"])
            assert await auth.access_token() == "secret"
            assert not (tmp_path / "pending-login.json").exists()
            assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600
    asyncio.run(scenario())


def test_expired_token_refresh_and_rotation(tmp_path):
    save_private(tmp_path / "tokens.json", {
        "access_token": "expired", "refresh_token": "old-refresh", "client_id": "client", "expires_at": 0,
    })
    calls = []

    def handler(request):
        calls.append(request)
        assert parse_qs(request.content.decode())["refresh_token"] == ["old-refresh"]
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 7200})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth = OAuthSession(client, tmp_path)
            assert await auth.access_token() == "new"
            assert await auth.access_token() == "new"
    asyncio.run(scenario())
    assert len(calls) == 1
    assert read_private(tmp_path / "tokens.json")["refresh_token"] == "rotated"


def test_cli_preview_works_without_login_or_network(tmp_path, capsys):
    args = parser().parse_args(["--state-dir", str(tmp_path), "order", "--account-id", "123",
                               "--symbol", "SPY", "--side", "BUY", "--quantity", "1", "--limit-price", "1"])
    asyncio.run(run(args))
    assert json.loads(capsys.readouterr().out)["submitted"] is False
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("payload", [{}, {"expires_in": 7200}, {"access_token": "new"}])
def test_invalid_refresh_does_not_reuse_old_access_token(tmp_path, payload):
    save_private(tmp_path / "tokens.json", {
        "access_token": "expired", "refresh_token": "refresh", "client_id": "client", "expires_at": 0,
    })

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload))) as client:
            with pytest.raises(MoomooError, match="invalid token"):
                await OAuthSession(client, tmp_path).access_token()
    asyncio.run(scenario())
    assert read_private(tmp_path / "tokens.json")["access_token"] == "expired"


def test_redirect_is_not_followed_even_if_client_allows_it(tmp_path):
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(307, headers={"Location": "https://other.example/collect"})

        save_private(tmp_path / "tokens.json", {"access_token": "secret", "expires_at": time.time() + 3600})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with pytest.raises(MoomooError):
                await MoomooPaperBroker(client, OAuthSession(client, tmp_path)).accounts()
        assert len(calls) == 1
    asyncio.run(scenario())


def test_cli_status_pipeline_is_read_only_and_sanitized(tmp_path, capsys, monkeypatch):
    from tools import moomoo_paper as cli

    save_private(tmp_path / "tokens.json", {"access_token": "secret", "expires_at": time.time() + 3600})
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        path = request.url.path.rsplit("/", 1)[-1]
        return ok(ACCOUNTS if path == "accounts" else
                  {"balance": "1000000"} if path == "cash-info" else {path: []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    asyncio.run(run(parser().parse_args(["--state-dir", str(tmp_path), "status"])))
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["connected"] is True
    assert result["environment"] == "SIMULATE"
    assert result["scheduled_trading"] is False
    assert len(calls) == 4
    assert "secret" not in output and "1000000" not in output


def test_credential_file_permissions_enforced(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text('{"access_token": "secret"}')
    path.chmod(0o644)
    with pytest.raises(MoomooError, match="chmod 600"):
        read_private(path)
