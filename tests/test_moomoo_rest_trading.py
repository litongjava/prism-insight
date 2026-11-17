from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import httpx
import pytest
from prism_core.moomoo_rest_trading import MoomooRestTrading, paper
from prism_core.moomoo_trading import MoomooConfig
from prism_core.execution_service import ExecutionService
from prism_core.order_intents import IntentStore, OrderIntent


@pytest.fixture
def rest(monkeypatch,tmp_path):
    state={'orders':[],'positions':[],'posts':0,'market':False}
    def handler(req):
        path=req.url.path
        if path.endswith('/accounts'):
            data={'accounts':[{'account_id':'123','market_id':100,'account_type':1}]}
        elif path.endswith('/cash-info'):data={'balance':'100000','max_power_long':'100000','total_asset':'100000'}
        elif path.endswith('/positions'):data={'positions':state['positions']}
        elif path.endswith('/history-orders'):
            return httpx.Response(200,json={'ret_code':0,'data':{'orders':state['orders']},'pagination':{'has_more':False,'next_key':'0'}})
        elif path.endswith('/stock-quote'):
            data={'quote_list':[{'code':'US.AAPL','last_price':100,'data_time':int(datetime.now(timezone.utc).timestamp()*1000),'suspension':False}]}
        elif path.endswith('/orders') and req.method=='POST':
            state['posts']+=1;body=json.loads(req.content)
            state['orders'].append(dict(order_id='987',symbol='AAPL',market=2,side=body['order_side'],qty=body['qty'],price=body.get('price','0'),order_type=body['order_type'],cum_qty='0',avg_fill_price='0',status=2,text=body['text'],create_time=str(int(datetime.now(timezone.utc).timestamp()*1000000))))
            data={'order_id':'987'}
        elif path.endswith('/orders'):data={'orders':state['orders']}
        else:raise AssertionError(path)
        return httpx.Response(200,json={'ret_code':0,'data':data})
    class Auth:
        async def access_token(self):return 'test'
    @asynccontextmanager
    async def conn(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            auth=Auth();yield paper.MoomooPaperBroker(client,auth),client,auth
    monkeypatch.setattr(MoomooRestTrading,'_connection',conn)
    monkeypatch.setenv('MOOMOO_REST_STATE_DIR',str(tmp_path/'private'))
    trader=MoomooRestTrading(MoomooConfig(account_id=123,buy_amount=5000))
    return trader,state


def test_buy_requires_stable_intent_and_cap(rest):
    t,s=rest
    assert not t._buy('AAPL',buy_amount=100)['success']
    assert not t._buy('AAPL',buy_amount=5100,paper_intent_id='large')['success']
    assert s['posts']==0
    result=t._buy('AAPL',buy_amount=5000,paper_intent_id='buy')
    assert result['success'] and result['quantity']==50 and result['filled_quantity'] is None
    t._buy('AAPL',buy_amount=5000,paper_intent_id='buy')
    assert s['posts']==1


def test_rest_evidence_adapter_and_market_exit(rest,monkeypatch):
    from prism_core.moomoo_evidence import order_facts
    t,s=rest
    s['positions']=[dict(symbol='AAPL',market=2,pstn_type=0,qty='2',qty_avbl='2',cost_price='95',cur_price='100')]
    monkeypatch.setattr(t,'is_market_open',lambda:True)
    result=t._sell('AAPL',quantity=2,paper_intent_id='exit')
    assert result['success'] and s['orders'][0]['order_type']==3
    s['orders'][0].update(status=4,cum_qty='2',avg_fill_price='99')
    facts=order_facts(t.get_order('987'))
    assert facts['filled_qty']==2 and facts['terminal'] and facts['side']=='01'


@pytest.mark.asyncio
async def test_execution_service_forwards_stable_intent(rest,tmp_path):
    t,s=rest
    store=IntentStore(tmp_path/'execution.sqlite')
    intent=OrderIntent.create(market='US',account_id=t.account_key,symbol='AAPL',side='BUY',
         source='test',source_decision_id='decision',quantity=2,limit_price=100,order_style='LIMIT')
    service=ExecutionService(t,intent_store=store)
    async with service:
        result=await service.execute_buy(ticker='AAPL',buy_amount=200,limit_price=100,intent=intent)
    assert result['success'] and s['posts']==1
