import asyncio
from datetime import date
import json
from pathlib import Path
import sys

import pytest

from cores.llm import openai_search_mcp_server as search
from cores.llm.config_loader import load_mcp_registry


def response(answer="A cited answer", *, sources=True):
    rows = [{"type": "url", "url": "https://example.com/news", "title": "Source"}] if sources else []
    return dict(status="completed", output=[
        dict(type="web_search_call", status="completed", action=dict(type="search", sources=rows)),
        dict(type="message", content=[dict(type="output_text", text=answer, annotations=[])]),
    ], usage={"total_tokens": 100})


def test_payload_requires_live_search_and_bounds_cost(monkeypatch):
    monkeypatch.setenv("OPENAI_WEB_SEARCH_MODEL", "gpt-5.4")
    payload = search.build_payload([search.Message(role="user", content="news as of 2026-11-13")],
                                   "week", ["sec.gov", "-example.com"], "low")
    assert payload["tools"][0] == dict(type="web_search", external_web_access=True,
        search_context_size="low", filters={"allowed_domains": ["sec.gov"], "blocked_domains": ["example.com"]})
    assert payload["tool_choice"] == "required" and payload["max_tool_calls"] == 2
    assert payload["store"] is False and payload["max_output_tokens"] == 3000
    assert "not a verified date filter" in payload["instructions"]
    assert "2026-11-13" in payload["input"][0]["content"]


@pytest.mark.parametrize("domains", [["https://sec.gov"], ["localhost"], ["127.0.0.1"], ["u:p@example.com"], ["example.com/path"]])
def test_invalid_domains_rejected(domains):
    with pytest.raises(ValueError):
        search.build_payload([search.Message(role="user", content="news")], domains=domains)


@pytest.mark.parametrize("data", [
    {}, {"status": "incomplete"}, {"status": "completed", "output": []},
    {"status": "completed", "output": [{"type": "web_search_call", "status": "failed"}]},
])
def test_incomplete_or_unsearched_answer_not_evidence(data):
    with pytest.raises(RuntimeError):
        search.parse_response(data)


def test_sources_come_only_from_tool_evidence_not_generated_answer():
    data = response("Read https://invented.example.org/story published yesterday")
    data["output"][1]["content"][0]["annotations"] = [
        dict(type="url_citation", url="https://example.com/news", title="Duplicate"),
        dict(type="url_citation", url="https://sec.gov/filing", title="Filing"),
        dict(type="url_citation", url="http://127.0.0.1/private", title="Private"),
    ]
    result = search.parse_response(data)
    assert [r["url"] for r in result["results"]] == ["https://example.com/news", "https://sec.gov/filing"]
    assert all(r["published_date"] is None for r in result["results"])


def test_ask_and_search_preserve_contract(monkeypatch):
    async def fake(payload):
        return response()
    monkeypatch.setattr(search, "_request", fake)
    answer = asyncio.run(search.perplexity_ask([search.Message(role="user", content="news")], search_recency_filter="day"))
    assert "https://example.com/news" in answer and "prompt only" in answer
    rows = asyncio.run(search.perplexity_search("news", 5, 256))
    assert rows["results"][0]["date"] is None
    assert rows["page_excerpts_available"] is False


def test_missing_key_fails_before_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(search.aiohttp, "ClientSession", lambda **kwargs: pytest.fail("network"))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(search._request({}))


def test_transport_does_not_expose_error_body_or_retry(monkeypatch):
    calls = []
    class Response:
        status = 401
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def text(self):
            pytest.fail("Do not read private provider errors")
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def post(self, url, **kwargs):
            calls.append(url)
            assert kwargs["allow_redirects"] is False
            return Response()
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setattr(search.aiohttp, "ClientSession", Session)
    with pytest.raises(RuntimeError, match="^OPENAI_SEARCH_HTTP_401$"):
        asyncio.run(search._request({}))
    assert calls == [search.URL]


def test_provider_switch_keeps_firecrawl_and_default_perplexity(monkeypatch):
    monkeypatch.delenv("PRISM_WEB_SEARCH_PROVIDER", raising=False)
    default = load_mcp_registry()
    monkeypatch.setenv("PRISM_WEB_SEARCH_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    selected = load_mcp_registry()
    assert default.get("firecrawl") == selected.get("firecrawl")
    assert "@perplexity-ai/mcp-server@1.2.0" in default.get("perplexity").args
    spec = selected.get("perplexity")
    assert spec.args == ("-m", "cores.llm.openai_search_mcp_server")
    assert spec.env["OPENAI_API_KEY"] == "test-key"
    assert "PERPLEXITY_API_KEY" not in spec.env


def test_prefetch_consumes_openai_sources_then_firecrawl(monkeypatch, tmp_path):
    from prism_core import report_research_prefetch as prefetch
    calls = []
    async def fake_request(payload):
        return response()
    monkeypatch.setattr(search, "_request", fake_request)
    async def transport(server, tool, arguments):
        calls.append((server, tool))
        if server == "perplexity":
            return await search.perplexity_search(**arguments)
        return dict(success=True, data=dict(markdown="Example Corp revenue and business segment results. " * 30,
                                            metadata={"publishedTime": "2025-01-01"}))
    packet = asyncio.run(prefetch.prefetch_report_research(
        "US", "EXM", "20250918", "Example Corp", _transport=transport,
        _config={"version": prefetch.VERSION, "enabled": True}, _cache_dir=tmp_path))
    assert calls == [("perplexity", "perplexity_search"), ("firecrawl", "firecrawl_scrape")]
    assert packet["receipt"]["usable_sources"] == 1
    assert 'SOURCE_METADATA_UNVERIFIED' in packet["section_notes"]["news_analysis"]


def test_prefetch_cache_is_separate_by_provider_and_model(monkeypatch, tmp_path):
    from prism_core import report_research_prefetch as prefetch
    calls = []
    async def transport(*args):
        calls.append(args)
        return {"results": []}
    def run():
        return asyncio.run(prefetch.prefetch_report_research(
            "US", "EXM", "20250918", "Example Corp", _transport=transport,
            _config={"version": prefetch.VERSION, "enabled": True}, _cache_dir=tmp_path))
    monkeypatch.setenv("PRISM_WEB_SEARCH_PROVIDER", "perplexity")
    run()
    assert len(calls) == 1
    monkeypatch.setenv("PRISM_WEB_SEARCH_PROVIDER", "openai")
    assert run()["receipt"]["search_provider"] == "openai"
    assert len(calls) == 2
    run()
    assert len(calls) == 2
    monkeypatch.setenv("OPENAI_WEB_SEARCH_MODEL", "another-supported-model")
    assert run()["receipt"]["search_model"] == "another-supported-model"
    assert len(calls) == 3


def test_peer_candidates_honor_openai_override_without_perplexity_key(monkeypatch):
    from prism_core import us_peer_comparison as peers
    monkeypatch.setenv("PRISM_WEB_SEARCH_PROVIDER", "openai")
    monkeypatch.setattr(peers, "_perplexity_key", lambda: pytest.fail("Perplexity credentials"))
    async def fake(messages):
        return {"answer": json.dumps({"peers": [{"ticker": "AMD", "name": "AMD", "overlap": "GPUs"}]})}
    monkeypatch.setattr(search, "search_messages", fake)
    rows = asyncio.run(peers.perplexity_candidates("NVDA", "Nvidia", date(2026, 11, 13)))
    assert rows[0]["ticker"] == "AMD"


def test_real_stdio_server_exposes_both_search_tools():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "cores.llm.openai_search_mcp_server"], cwd=str(Path(__file__).resolve().parents[1]))
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                result = await client.list_tools()
                assert {tool.name for tool in result.tools} == {"perplexity_ask", "perplexity_search"}
    asyncio.run(run())
