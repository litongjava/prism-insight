import socket
import pytest
from cores.llm import dsb_scrape_mcp_server as scraper


def test_public_only_url_boundary(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(None,None,None,None,('127.0.0.1',443))])
    for url in ['file:///etc/passwd', 'http://example.com', 'https://user:pass@example.com', 'https://localhost']:
        with pytest.raises(ValueError):scraper.validate_url(url)


@pytest.mark.asyncio
async def test_scrape_requires_isolation_and_preserves_actual_source(monkeypatch,tmp_path):
    monkeypatch.setenv('PRISM_DSB_LOCK',str(tmp_path/'lock'))
    monkeypatch.setattr(scraper,'validate_url',lambda url:url)
    calls=[]
    async def dsb(*args):
        calls.append(args)
        if args[0]=='tasks':return {'tasks':[{'id':1120}],'browser':{'ephemeral':True}}
        if args[0]=='js':return {'result':{'url':'https://example.com/actual','title':'actual','text':'source '*20}}
        return {}
    monkeypatch.setattr(scraper,'dsb',dsb)
    result=await scraper.firecrawl_scrape('https://example.com')
    assert result['markdown']=='source '*20
    assert result['metadata']['sourceURL']=='https://example.com/actual'
    assert any('wait_for_function' in call for call in calls)
