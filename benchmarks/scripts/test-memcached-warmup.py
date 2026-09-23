#!/usr/bin/env python3
"""Integration test: warm real Memcached, inspect occupancy, retrieve trace keys."""
import json,os,shutil,socket,struct,subprocess,tempfile,time
from pathlib import Path
binary=shutil.which('memcached')
if not binary:raise SystemExit('Install memcached to run this integration test')
with tempfile.TemporaryDirectory() as d:
 path=Path(d);keys=path/'keys';keys.write_text(''.join(str(i).ljust(40,'0')+'\n' for i in range(10000,11024)))
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 server=subprocess.Popen([binary,'-p',str(port),'-U','0','-B','binary','-l','127.0.0.1','-m','16']+(['-u','root'] if os.getuid()==0 else []),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 try:
  for _ in range(100):
   try:
    with socket.create_connection(('127.0.0.1',port),timeout=.2):pass
    break
   except OSError:time.sleep(.02)
  result=subprocess.run(['python3',str(Path(__file__).with_name('memcached-warmup.py')),'--keys',str(keys),'--port',str(port),'--value-size','64'],capture_output=True,text=True,timeout=30,check=True)
  evidence=json.loads(result.stdout.splitlines()[-1]);assert evidence['sets']==1024 and int(evidence['stats']['curr_items'])==1024,evidence
  h=struct.Struct('!BBHBBHIIQ');key=b'1000000000000000000000000000000000000000'
  with socket.create_connection(('127.0.0.1',port),timeout=5) as s:
   s.sendall(h.pack(0x80,0,len(key),0,0,0,len(key),0,0)+key)
   f=s.makefile('rb');head=h.unpack(f.read(24));body=f.read(head[6]);assert head[5]==0 and body[4:]==b'x'*64
  print(json.dumps({'status':'PASS','inserted':1024,'server_items':int(evidence['stats']['curr_items']),'get_value_verified':True}))
 finally:server.terminate();server.wait(timeout=10)
