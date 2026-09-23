#!/usr/bin/env python3
"""Warm the cache with recorded CacheLib keys over Guest-local binary TCP.
Keys are newline-delimited, already padded exactly as the trace generator.
SETQ errors are checked at every NOOP barrier; no random/padding keys are added.
"""
import argparse,json,socket,struct,time
H=struct.Struct('!BBHBBHIIQ')
def recv(s,n):
 b=bytearray()
 while len(b)<n:
  v=s.recv(n-len(b))
  if not v: raise RuntimeError('Memcached closed connection')
  b.extend(v)
 return bytes(b)
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--keys',required=True);p.add_argument('--value-size',type=int,default=4096);p.add_argument('--port',type=int,default=11212);a=p.parse_args()
 if not 0<a.value_size<=65217:p.error('invalid value size')
 value=b'x'*a.value_size;extra=struct.pack('!II',0,0);count=0;start=time.monotonic()
 with socket.create_connection(('127.0.0.1',a.port),timeout=120) as s,open(a.keys,'rb') as f:
  packets=[]
  def flush():
   if not packets:return
   s.sendall(b''.join(packets)+H.pack(0x80,0x0a,0,0,0,0,0,0,0))
   while True:
    magic,op,k,e,d,status,n,opaque,cas=H.unpack(recv(s,H.size));body=recv(s,n)
    if magic!=0x81 or status:raise RuntimeError('SETQ/NOOP error: '+repr((op,status,body)))
    if op==0x0a:break
   packets.clear()
  for line in f:
   key=line.rstrip(b'\n')
   if not 0<len(key)<=250:raise ValueError('Invalid trace key')
   packets.append(H.pack(0x80,0x11,len(key),8,0,0,len(key)+8+len(value),0,0)+extra+key+value);count+=1
   if count%128==0:flush()
   if count%100000==0:print(json.dumps({'sets':count,'seconds':time.monotonic()-start}),flush=True)
  flush()
  # Capture actual cache contents and evictions after the warmup.
  s.sendall(H.pack(0x80,0x10,0,0,0,0,0,0,0));stats={}
  while True:
   magic,op,k,e,d,status,n,opaque,cas=H.unpack(recv(s,H.size));b=recv(s,n)
   if status:raise RuntimeError('stats failed')
   if not k:break
   stats[b[:k].decode()]=b[k:].decode()
  print(json.dumps({'status':'PASS','sets':count,'value_size':a.value_size,'seconds':time.monotonic()-start,'stats':stats}),flush=True)
if __name__=='__main__':main()
