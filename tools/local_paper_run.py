#!/usr/bin/env python3
"""Launch a PRISM local paper command with private inherited credentials."""
import os
from pathlib import Path
import sys


def configure():
    from dotenv import dotenv_values
    directory=Path.home()/'.config/prism-insight'
    for name in ('openai.env','lark.env','runtime.env'):
        p=directory/name
        if not p.is_file() or p.stat().st_mode & 0o077:
            raise RuntimeError('Private runtime configuration missing or insecure')
        for k,v in dotenv_values(p).items():
            if v is not None:os.environ[k]=v
    if (os.environ.get('MOOMOO_TRANSPORT')!='rest' or os.environ.get('MOOMOO_TRADING_MODE')!='demo'
            or os.environ.get('MOOMOO_ALLOW_LIVE')!='false' or os.environ.get('PRISM_US_BROKER')!='moomoo'):
        raise RuntimeError('Local launcher requires REST simulation only')
    if not 0<float(os.environ['MOOMOO_BUY_AMOUNT_USD'])<=5000:
        raise RuntimeError('Local paper buy cap invalid')
    node=str(Path.home()/'.nvm/versions/node/v26.13.0/bin')
    os.environ['PATH']=node+':'+str(Path.home()/'.local/bin')+':'+os.environ.get('PATH','')
    os.environ['PRISM_DISABLE_SIGNAL_PUBLISH']='1'
    os.chdir(Path(__file__).resolve().parents[1])


def main():
    configure()
    if len(sys.argv)<2:raise SystemExit('A Python script and its arguments are required')
    os.execv(sys.executable,[sys.executable,*sys.argv[1:]])

if __name__=='__main__':main()
