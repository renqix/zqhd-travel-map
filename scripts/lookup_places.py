#!/usr/bin/env python3
"""查询地图候选，不自动决定同名服务区的实际位置。"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('query',help='包含省市、高速编号与服务区名的查询')
    p.add_argument('--cache',required=True);p.add_argument('--base-url',default='https://nominatim.openstreetmap.org')
    p.add_argument('--contact',help='可选联系方式，放在User-Agent中')
    args=p.parse_args();cache=Path(args.cache).resolve();cache.mkdir(parents=True,exist_ok=True)
    params=urllib.parse.urlencode({'q':args.query,'format':'jsonv2','addressdetails':1,'limit':5})
    url=args.base_url.rstrip('/')+'/search?'+params
    target=cache/(hashlib.sha256(url.encode()).hexdigest()+'.json')
    stamp=cache/'last-request.txt'
    if not target.exists():
        previous=float(stamp.read_text()) if stamp.exists() else 0
        time.sleep(max(0,1.1-(time.time()-previous)))
        agent='zqhd-travel-map/1.0 (manual candidate review)'+(' '+args.contact if args.contact else '')
        request=urllib.request.Request(url,headers={'User-Agent':agent})
        try:
            with urllib.request.urlopen(request,timeout=30) as response:blob=response.read()
        finally:stamp.write_text(str(time.time()))
        json.loads(blob);target.write_bytes(blob)
    items=json.loads(target.read_text())
    result=[{'name':i['display_name'],'lon':float(i['lon']),'lat':float(i['lat']),
             'osm_type':i.get('osm_type'),'osm_id':i.get('osm_id'),'address':i.get('address'),
             'source':f'https://www.openstreetmap.org/{i.get("osm_type")}/{i.get("osm_id")}'} for i in items]
    print(json.dumps({'query':args.query,'candidates':result,'note':'需核对方向、高速编号、城市和订单原文后选择'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
