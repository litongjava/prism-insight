"""Public-page text scraping through an isolated dsb browser, Firecrawl schema."""
import asyncio
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("firecrawl")


def validate_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.port not in (None,443) or len(url)>4096):
        raise ValueError("Only public HTTPS pages are supported")
    addresses = socket.getaddrinfo(parts.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Private/local addresses are not supported")
    return url


async def dsb(*args):
    cmd=[os.getenv("DSB_BIN", str(Path.home()/"go/bin/dsb")),"--port",os.getenv("PRISM_DSB_PORT","10051"),
         "--id","1120","--no-record","--json","--timeout","25",*args]
    proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout,_=await asyncio.wait_for(proc.communicate(),35)
    except asyncio.TimeoutError:
        proc.kill();await proc.wait();raise RuntimeError("Browser scrape timed out; no automatic retry") from None
    try:
        payload=json.loads(stdout)
    except ValueError:
        raise RuntimeError("Browser scrape failed") from None
    if proc.returncode or not payload.get("ok"):
        raise RuntimeError("Browser scrape unavailable")
    return payload.get("data",{})


@mcp.tool()
async def firecrawl_scrape(url: str, formats: list[str] | None=None, onlyMainContent: bool=True) -> dict:
    """Read public-page source text without synthesis. Returns source URL and capture time."""
    await asyncio.to_thread(validate_url,url)
    lockpath=Path(os.getenv("PRISM_DSB_LOCK",str(Path.home()/".local/state/prism-moomoo/scrape.lock")))
    lockpath.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(lockpath,os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError("Browser scraper busy") from None
        tasks = await dsb("tasks")
        existing = any(str(t.get("id")) == "1120" for t in tasks.get("tasks", []))
        if not existing:
            await dsb("start","--ephemeral","--headless")
        elif not tasks.get("browser", {}).get("ephemeral"):
            raise RuntimeError("Scraper requires an isolated temporary browser")
        await dsb("run","go_to_url","-p","url="+url,"-p","commandTimeoutMs=22000")
        await dsb("run", "wait_for_function", "-p", "expression=() => (document.querySelector('main,article')||document.body).innerText.length > 100", "-p", "timeoutMs=12000")
        data=await dsb("js",'() => ({url:location.href,title:document.title,text:(document.querySelector("main,article")||document.body).innerText.slice(0,80000)})')
        result=data.get("result")
        if isinstance(result,str):result=json.loads(result)
        if not isinstance(result,dict) or len(result.get("text", "")) < 100:
            raise RuntimeError("No page text available")
        await asyncio.to_thread(validate_url,result["url"])
        return {"markdown":result["text"],"metadata":{"title":result["title"],"sourceURL":result["url"],
                "capturedAt":datetime.now(timezone.utc).isoformat(),"provider":"dsb"}}
    finally:os.close(fd)

if __name__ == "__main__":mcp.run()
