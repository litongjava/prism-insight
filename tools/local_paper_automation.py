#!/usr/bin/env python3
"""Local REST paper scheduling, exact broker reconciliation and Lark notices.

Run via local_paper_run.py. All schedules use the actual NYSE session, including
holidays/DST/early closes. Claims survive process death; a batch is never replayed
merely because its previous process failed or its result is unknown.
"""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STATE = Path.home() / '.local/state/prism-moomoo/automation'


def session(at):
    import pandas_market_calendars as calendars
    date = at.astimezone(ZoneInfo('America/New_York')).date()
    rows = calendars.get_calendar('NYSE').schedule(date, date)
    if rows.empty:
        return None
    row = rows.iloc[0]
    return row.market_open.to_pydatetime(), row.market_close.to_pydatetime()


def due(at):
    times = session(at)
    if times is None:
        return []
    opening, closing = times
    windows = [('morning', opening + timedelta(minutes=15)),
               ('afternoon', closing - timedelta(minutes=90))]
    # Do not catch up missed entry batches after their execution window.
    return [mode for mode, start in windows if start <= at < start + timedelta(minutes=15)]


@contextmanager
def database():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(STATE / 'automation.sqlite3')
    try:
        db.execute('CREATE TABLE IF NOT EXISTS runs (key TEXT PRIMARY KEY, state TEXT, updated TEXT, code INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS notices (key TEXT PRIMARY KEY, state TEXT, updated TEXT)')
        with db:
            yield db
    finally:
        db.close()


def claim(key):
    with database() as db:
        return db.execute('INSERT OR IGNORE INTO runs VALUES (?, ?, ?, NULL)',
                          (key, 'STARTED', datetime.now(timezone.utc).isoformat())).rowcount == 1


def finish(key, state, code=None):
    with database() as db:
        db.execute('UPDATE runs SET state=?, updated=?, code=? WHERE key=?',
                   (state, datetime.now(timezone.utc).isoformat(), code, key))


async def notice(key, text):
    from messaging.lark_delivery import make_bot
    with database() as db:
        inserted = db.execute('INSERT OR IGNORE INTO notices VALUES (?, ?, ?)',
                    (key, 'SENDING', datetime.now(timezone.utc).isoformat())).rowcount
    if not inserted:
        return
    try:
        await make_bot().send_message(os.environ['LARK_CHAT_ID'], text,
            message_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, 'prism-paper:' + key)))
    except Exception:
        # An ambiguous delivery is not automatically repeated on the next tick.
        state = 'UNKNOWN'
    else:
        state = 'SENT'
    with database() as db:
        db.execute('UPDATE notices SET state=?,updated=? WHERE key=?',
                   (state, datetime.now(timezone.utc).isoformat(), key))
    if state != 'SENT':
        print('Lark delivery UNKNOWN; inspect notification state before resending', flush=True)


async def reconcile():
    from prism_core.moomoo_rest_trading import paper, manager_module
    import httpx
    directory = Path(os.getenv('MOOMOO_REST_STATE_DIR', str(paper.STATE_DIR)))
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        broker = paper.MoomooPaperBroker(client, paper.OAuthSession(client, directory))
        result = await manager_module.PaperManager(broker, directory).reconcile(os.environ['MOOMOO_ACCOUNT_ID'])
    for row in result['intents']:
        event = [row['intent'], row['state'], row['filled'], row['avg_price']]
        key = 'order:' + hashlib.sha256(json.dumps(event).encode()).hexdigest()
        filled = row['filled'] if row['filled'] is not None else '待核对'
        price = row['avg_price'] if row['avg_price'] is not None else '待核对'
        await notice(key, f"PRISM 模拟交易 · {row['symbol']} {row['side']}\n"
            f"订单状态：{row['state']}；申请 {row['qty']} 股；已确认成交 {filled} 股；均价 {price} USD。\n"
            '以上为券商订单核对结果，订单受理不代表成交。')
    print(json.dumps({'paper':True, 'positions':len(result['positions']),
                      'intents':len(result['intents']), 'observed_at':result['observed_at']}), flush=True)
    return result


def run_process(key, script, *args, timeout=7500):
    log = STATE / (key.replace(':', '-') + '.log')
    with log.open('a') as stream:
        try:
            result = subprocess.run([sys.executable, str(ROOT / script), *args], cwd=ROOT,
                                    stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
    # Exit 0 is not evidence of a trade or successful strategy decision.
    finish(key, 'PROCESS_EXITED' if code == 0 else 'ERROR', code)
    return code


async def tick(kind, at):
    date = at.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    times = session(at)
    if kind == 'batch':
        for mode in due(at):
            key = date + ':' + mode
            if claim(key):
                await notice(key + ':start', f'PRISM 模拟交易：{date} {mode} 定时分析已开始，单次买入上限 5,000 USD。')
                code = await asyncio.to_thread(run_process, key, 'prism-us/us_stock_analysis_orchestrator.py',
                                               '--mode', mode, '--language', 'en')
                await notice(key + ':end', f'PRISM 模拟交易：{mode} 分析进程已结束，退出码 {code}。成交与持仓以券商核对通知为准。')
    elif kind == 'monitor':
        if not times or not times[0] - timedelta(minutes=10) <= at <= times[1] + timedelta(minutes=30):
            return
        await reconcile()
        if times[0] <= at < times[1] and Path(os.environ['STOCK_TRACKING_DB']).exists():
            key = date + ':protection:' + at.strftime('%H%M')
            if claim(key):
                code = await asyncio.to_thread(run_process, key, 'tools/hardstop_seller.py', '--market', 'us', '--once', timeout=240)
                if code:
                    await notice(date + ':protection-error', 'PRISM 模拟持仓保护进程报错，请检查运行日志；不能视为保护成功。')
        if at >= times[1] + timedelta(minutes=10) and Path(os.environ['STOCK_TRACKING_DB']).exists():
            key = date + ':fills'
            if claim(key):
                await asyncio.to_thread(run_process, key, 'tools/reconcile_fills.py', '--market', 'US', timeout=240)
    elif kind == 'reconcile':
        await reconcile()
    elif kind == 'status':
        with database() as db:
            print(json.dumps({'runs':db.execute('SELECT key,state,code FROM runs ORDER BY updated DESC LIMIT 12').fetchall(),
                              'notifications':db.execute('SELECT state,count(*) FROM notices GROUP BY state').fetchall()}))


def main():
    from local_paper_run import configure
    configure()
    os.umask(0o077)
    # This existing protection worker uses this optional destination override.
    os.environ['HARDSTOP_CHAT_ID'] = os.environ['LARK_CHAT_ID']
    parser = argparse.ArgumentParser()
    parser.add_argument('job', choices=['batch', 'monitor', 'reconcile', 'status'])
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / (args.job + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            asyncio.run(tick(args.job, datetime.now(timezone.utc)))
        except Exception:
            # Raw broker/SDK exceptions must not leak private response data.
            print('Paper job failed; no automatic order replay', flush=True)
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()
