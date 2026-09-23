#!/usr/bin/env python3
"""Validate YCSB reads; retain user-tolerated INSERT errors as load warnings."""
import argparse,json,math,re
from pathlib import Path

def validate(path,phase,expected,allow_insert_errors=True):
 text=path.read_text(errors='replace')
 returns=[{'operation':op,'code':code,'count':int(n)} for op,code,n in re.findall(r'^\[([^]]+)\], Return=(\S+), (\d+)$',text,re.M)]
 errors=[v for v in returns if v['code']!='OK' and v['count']]
 times=re.findall(r'^\[OVERALL\], RunTime\(ms\), (\S+)$',text,re.M)
 rates=re.findall(r'^\[OVERALL\], Throughput\(ops/sec\), (\S+)$',text,re.M)
 completed=None
 if len(times)==len(rates)==1:
  product=float(times[0])*float(rates[0])/1000
  if math.isfinite(product):completed=round(product)
 inserted=sum(v['count'] for v in returns if v['operation']=='INSERT' and v['code']=='OK')
 valid=bool(returns) and not errors and completed==expected and (phase!='load' or inserted==expected)
 tolerated=(allow_insert_errors and phase=='load' and bool(errors) and
            all(v['operation']=='INSERT' for v in returns) and inserted>0 and
            completed is not None and 0<completed<=expected and inserted<=completed)
 return {'status':'PASS' if valid or tolerated else 'FAIL','phase':phase,'expected':expected,
         'completed':completed,'insert_ok':inserted if phase=='load' else None,
         'missing_insert_ok':max(0,expected-inserted) if phase=='load' else None,
         'return_counts':returns,'errors':errors,
         'acceptance_policy':'load-insert-errors-nonfatal-v1' if allow_insert_errors else 'strict-v1',
         'warnings':['INSERT errors tolerated by user; successful insertion count may be below requested dataset size'] if tolerated else []}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('log',type=Path);p.add_argument('--phase',choices=['load','run'],required=True);p.add_argument('--expected',type=int,required=True);p.add_argument('--strict-load',action='store_true');a=p.parse_args()
 r=validate(a.log,a.phase,a.expected,not a.strict_load);print(json.dumps(r,indent=2));return 0 if r['status']=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())
