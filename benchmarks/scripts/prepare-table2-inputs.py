#!/usr/bin/env python3
"""Materialize recorded workload prefixes in the Guest, without padding/repetition."""
import argparse, importlib.util, json, subprocess, shlex
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('guest',ROOT/'hyperalloc-6.18/scripts/guestctl.py');g=importlib.util.module_from_spec(s);s.loader.exec_module(g)
def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--config',type=Path,default=ROOT/'benchmarks/config/chameleon-table2-workloads.json')
 p.add_argument('--vm',default='guest-tools-final');p.add_argument('--cases',nargs='+')
 a=p.parse_args();config=json.loads(a.config.read_text());access=g.load_access(ROOT/'hyperalloc-6.18/build/guests'/a.vm/'access.json')
 def remote(args): return subprocess.run(g.ssh_command(access,args),capture_output=True,text=True,check=True).stdout
 with g.control_lock(access):
  for case,item in config['applications'].items():
   if a.cases and case not in a.cases:continue
   d=item.get('prepare')
   if not d:continue
   dest='/home/'+access['user']+'/chameleon-inputs/'+d['output'];manifest=dest+'.json'
   old=remote(['sh','-c','cat '+shlex.quote(manifest)+' 2>/dev/null || true'])
   if old and json.loads(old)['recipe']==d:
    size=remote(['sh','-c','stat -c %s '+shlex.quote(dest)+' 2>/dev/null || true']).strip()
    if size and int(size)==json.loads(old)['bytes']:
     print(case+' already prepared',flush=True);continue
   print('Preparing '+case+' '+json.dumps(d),flush=True)
   if d['kind']=='cachelib-keys':
    import csv
    seen=set();count=0
    dst=subprocess.Popen(g.ssh_command(access,['sh','-c','cat > '+shlex.quote(dest+'.partial')]),stdin=subprocess.PIPE)
    try:
     for source in sorted((ROOT/d['source']).glob('kvcache_traces_*.csv')):
      with source.open() as f:
       for row in csv.DictReader(f):
        if row['op']!='SET':continue
        key=row['key'].ljust(max(len(row['key']),int(row['key_size'])),'0')
        if key in seen:continue
        if not 0<len(key)<=250:raise ValueError('invalid trace key')
        seen.add(key);dst.stdin.write(key.encode()+b'\n');count+=1
        if count%1000000==0:print(case+' keys='+str(count),flush=True)
        if count==d['records']:break
      if count==d['records']:break
     dst.stdin.close()
     if dst.wait()!=0 or count!=d['records']:raise RuntimeError('Incomplete unique SET keys: '+str(count))
    finally:
     if dst.poll() is None:dst.terminate();dst.wait()
   elif d['kind']=='pvc':
    remote(['/home/'+access['user']+'/chameleon-benchmarks/apps/pvc/build/pvc_generate','--output',dest+'.partial','--bytes',d['bytes']])
   else:
    source=Path(d['source']);source=source if source.is_absolute() else ROOT/source
    command={'xz-prefix':['xz','-dc',str(source)],'gzip-prefix':['gzip','-dc',str(source)],'prefix':['cat',str(source)]}[d['kind']]
    src=subprocess.Popen(command,stdout=subprocess.PIPE)
    dst=subprocess.Popen(g.ssh_command(access,['sh','-c','cat > '+shlex.quote(dest+'.partial')]),stdin=subprocess.PIPE)
    count=0;block=[]
    try:
     for line in src.stdout:
      if line.startswith((b'#',b'%')) or not line.strip():continue
      block.append(line);count+=1
      if len(block)==16384:dst.stdin.write(b''.join(block));block=[]
      if count==d['records']:break
     if block:dst.stdin.write(b''.join(block))
     dst.stdin.close()
     if dst.wait()!=0 or count!=d['records']:raise RuntimeError('Incomplete input: '+str(count))
    finally:
     src.stdout.close()
     if src.poll() is None:src.terminate()
     src.wait()
    print(case+' records='+str(count),flush=True)
   remote(['mv',dest+'.partial',dest])
   info={'recipe':d,'guest_path':dest,'bytes':int(remote(['stat','-c','%s',dest]).strip())}
   subprocess.run(g.ssh_command(access,['sh','-c','cat > '+shlex.quote(manifest)]),input=json.dumps(info,indent=2)+'\n',text=True,check=True)
   print(json.dumps(info),flush=True)
if __name__=='__main__':main()
