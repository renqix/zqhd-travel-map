#!/usr/bin/env python3
"""归一化换电行程，取得分段道路，并缓存完整视野所需的地图。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
from PIL import Image
from render_video import required_basemap_bounds

DEFAULT_MAP='https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}'
ATTRIBUTION=('Map © Esri. Sources: Esri, HERE, Garmin, Intermap, increment P Corp., GEBCO, '
             'USGS, FAO, NPS, NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, '
             'METI, Esri China (Hong Kong), © OpenStreetMap contributors, GIS User Community; roads: OSRM / OSM')

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def finite(value,field):
    if isinstance(value,bool):raise ValueError(f'{field}必须为数值')
    number=float(value)
    if not math.isfinite(number):raise ValueError(f'{field}必须为有限数值')
    return number

def parsed_time(value,tz):
    if not isinstance(value,str) or len(value.strip())<16 or not any(c in value for c in ('T',' ')):
        raise ValueError('日期时间必须包含完整日期，不能只写手机状态栏时间')
    dt=datetime.fromisoformat(value.strip().replace('Z','+00:00').replace('/','-'))
    dt=dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
    return dt

def point(raw,tz,kind):
    name=str(raw.get('name','')).strip()
    if not name:raise ValueError(f'{kind}缺少地点名称')
    lon=finite(raw.get('lon'),f'{name}.lon');lat=finite(raw.get('lat'),f'{name}.lat')
    if not (-180<=lon<=180 and -85.05112878<lat<85.05112878):
        raise ValueError(f'{name}不是可用的WGS84经纬度')
    stamp=parsed_time(raw.get('time'),tz)
    energy=finite(raw.get('energy_added_kwh'),f'{name}.energy_added_kwh') if kind=='swap' else 0.
    if energy<0:raise ValueError(f'{name}的新增电量不能小于零')
    result={**raw,'name':name,'time':stamp.replace(tzinfo=None).isoformat(timespec='seconds'),
            'lon':lon,'lat':lat,'energy_added_kwh':energy,'kind':kind}
    return result,stamp

def normalize(raw,overrides=None):
    overrides=overrides or {};tzname=raw.get('timezone','Asia/Shanghai');tz=ZoneInfo(tzname)
    start_raw={**raw.get('start',{})}
    for key in ('name','lon','lat','time'):
        if overrides.get('start_'+key) is not None:start_raw[key]=overrides['start_'+key]
    start,start_time=point(start_raw,tz,'start')
    rows=[];seen={};duplicates=[]
    for original in raw.get('orders',[]):
        row,stamp=point(original,tz,'swap')
        identity=('id',str(row['order_id'])) if row.get('order_id') else ('exact',row['name'],row['time'],row['lon'],row['lat'],row['energy_added_kwh'])
        content=(row['name'],row['time'],row['lon'],row['lat'],row['energy_added_kwh'])
        if identity in seen:
            if seen[identity]!=content:raise ValueError(f'重复订单号内容冲突：{row.get("order_id")}')
            duplicates.append({'name':row['name'],'time':row['time'],'reason':'同一订单重复输入'})
            continue
        seen[identity]=content;rows.append((stamp,row))
    if not rows:raise ValueError('至少需要一笔已读取并定位的换电订单')
    rows.sort(key=lambda item:item[0]);orders=[r for _,r in rows]
    if rows[0][0]<start_time:raise ValueError('第一笔换电早于手动出发时间，请核对完整日期')
    end_raw={**raw.get('end',{})}
    for key in ('name','lon','lat','time'):
        if overrides.get('end_'+key) is not None:end_raw[key]=overrides['end_'+key]
    assumptions=['道路为根据已核对点位重建的路线，非车辆原始GPS。',
                 '所有公里为道路计算估算；新增电量不作为行程耗电量。',
                 '区间时钟和位置经过插值，行程时间包含休息与等待。']
    if not end_raw:
        end_raw={k:orders[-1][k] for k in ('name','time','lon','lat')}
        assumptions.append('未指定终点，以最后一次换电站及订单时间结束。')
    end,end_time=point(end_raw,tz,'finish')
    if end_time<rows[-1][0]:raise ValueError('终点时间早于最后一笔换电，请核对完整日期')
    if end_time<=start_time:raise ValueError('结束时间必须晚于出发时间')
    events=[(start_time,start),*rows,(end_time,end)]
    for (ta,a),(tb,b) in zip(events,events[1:]):
        if ta==tb and any(abs(a[k]-b[k])>1e-9 for k in ('lon','lat')):
            raise ValueError('同时间的相邻事件必须在同一地点，请核对订单时间或坐标')
    if overrides.get('duration') is not None:duration=finite(overrides['duration'],'duration')
    else:duration=finite(raw.get('duration_seconds',15),'duration_seconds')
    if duration<2:raise ValueError('视频总长度至少2秒')
    if duplicates:assumptions.append(f'移除{len(duplicates)}笔明确重复订单。')
    result={'timezone':tzname,'start':start,'end':end,'orders':orders,'duration_seconds':duration,
            'duplicates_removed':duplicates,'energy_added_kwh':round(sum(o['energy_added_kwh'] for o in orders),6)}
    if raw.get('example_only'):
        result['example_only']=True
        assumptions.insert(0,'虚构订单示例，仅用于教学或验证，不作为真实换电行程交付。')
    return result,assumptions

def request_bytes(url,timeout=30):
    for attempt in range(3):
        try:
            request=urllib.request.Request(url,headers={'User-Agent':'zqhd-travel-map/1.0 (local travel cartography)'})
            with urllib.request.urlopen(request,timeout=timeout) as response:return response.read()
        except (urllib.error.URLError,TimeoutError) as exc:
            if attempt==2:raise RuntimeError(f'联网失败，已保留输入与缓存：{url}: {exc}') from exc
            time.sleep(1+attempt)

def road_route(document,points,max_snap=1000):
    if 'routes' not in document:
        route=document.get('route',document)
        if len(route.get('legs',[]))!=len(points)-1:raise ValueError('分段道路数量与点位不匹配')
        legs=[]
        for i,leg in enumerate(route['legs']):
            coords=leg.get('coordinates')
            if not coords or len(coords)<2:raise ValueError(f'第{i+1}段道路缺少完整几何')
            legs.append({'distance':finite(leg['distance'],'leg.distance'),'coordinates':coords})
        source={**route.get('source',{}),'loaded_from':'local-route','snap_distances_m':[]}
        source.setdefault('provider','local-route')
    else:
        if document.get('code','Ok')!='Ok' or not document['routes']:raise ValueError('道路服务没有返回可用路线')
        route=document['routes'][0]
        if len(route['legs'])!=len(points)-1:raise ValueError('OSRM道路段数量与点位不匹配')
        snaps=[finite(w.get('distance',0),'waypoint.distance') for w in document.get('waypoints',[])]
        if snaps and len(snaps)!=len(points):raise ValueError('道路吸附点数量不匹配')
        if any(d<0 or d>max_snap for d in snaps):raise ValueError(f'道路吸附超过{max_snap:g}米或无效，复核站点坐标：{snaps}')
        source={'provider':'OSRM','snap_distances_m':snaps};legs=[]
        for i,leg in enumerate(route['legs']):
            coords=[]
            for step in leg.get('steps',[]):
                part=step.get('geometry',{}).get('coordinates',[])
                if part:coords.extend(part[1:] if coords and coords[-1]==part[0] else part)
            if len(coords)<2:
                if float(leg['distance'])>1:raise ValueError(f'第{i+1}段缺少steps几何，需请求steps=true')
                coords=[[points[i]['lon'],points[i]['lat']],[points[i+1]['lon'],points[i+1]['lat']]]
            legs.append({'distance':finite(leg['distance'],'leg.distance'),'coordinates':coords})
    if any(l['distance']<0 for l in legs):raise ValueError('道路段距离不能为负数')
    for leg in legs:
        for coord in leg['coordinates']:
            if len(coord)!=2 or not (-180<=float(coord[0])<=180 and -85.05112878<float(coord[1])<85.05112878):raise ValueError('道路几何含无效坐标')
    for i,leg in enumerate(legs):
        for coord,pin in ((leg['coordinates'][0],points[i]),(leg['coordinates'][-1],points[i+1])):
            if haversine(coord,[pin['lon'],pin['lat']])>max_snap:
                raise ValueError(f'第{i+1}段道路端点离站点超过{max_snap:g}米，复核道路文件与点位')
        if points[i]['time']==points[i+1]['time'] and leg['distance']>1:
            raise ValueError('相邻事件同时间却有非零行驶距离，请核对时间和道路')
    total=sum(l['distance'] for l in legs)
    if abs(total-finite(route.get('distance',total),'route.distance'))>max(1,total*.001):raise ValueError('总道路距离与分段距离不一致')
    result={'distance':total,'legs':legs,'source':source}
    if document.get('synthetic_test_only') or route.get('synthetic_test_only') or source.get('synthetic_test_only'):
        result['synthetic_test_only']=True
    return result

def haversine(a,b):
    x1,y1,x2,y2=map(math.radians,(*a,*b))
    value=math.sin((y2-y1)/2)**2+math.cos(y1)*math.cos(y2)*math.sin((x2-x1)/2)**2
    return 6371008.8*2*math.asin(math.sqrt(min(1,max(0,value))))

def project(lon,lat,z):
    size=256*2**z
    return ((lon+180)/360*size,(1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*size)

def map_plan(points,route,width,height,radius,zoom=None):
    coords=[p for l in route['legs'] for p in l['coordinates']]+[[p['lon'],p['lat']] for p in points]
    xy=[project(lon,lat,6) for lon,lat in coords]
    extent=max(max(p[0] for p in xy)-min(p[0] for p in xy),max(p[1] for p in xy)-min(p[1] for p in xy),1)
    z=zoom if zoom is not None else max(4,min(12,int(6+math.log2(640/extent))))
    xy=[project(lon,lat,z) for lon,lat in coords]
    # 与绘图器共用唯一的视野计算，避免准备成功后仍出现缺图或硬切。
    bounds=required_basemap_bounds(points,route['legs'],z,width,height,radius)
    tile_bounds=[math.floor(bounds[0]/256)-1,math.floor(bounds[1]/256)-1,math.floor(bounds[2]/256)+1,math.floor(bounds[3]/256)+1]
    if min(tile_bounds)<0 or max(tile_bounds)>=2**z:raise ValueError('地图视野越过Mercator世界边界，请调整范围')
    if (tile_bounds[2]-tile_bounds[0]+1)*(tile_bounds[3]-tile_bounds[1]+1)>400:raise ValueError('底图请求过大，请降低zoom或核对异常点位')
    return z,tile_bounds,bounds

def prepare(args):
    source=Path(args.input).resolve();out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    trip,assumptions=normalize(load(source),vars(args))
    points=[trip['start'],*trip['orders'],trip['end']]
    (out/'trip.normalized.json').write_text(json.dumps(trip,ensure_ascii=False,indent=2),encoding='utf-8')
    cache=Path(args.cache).resolve() if args.cache else out/'cache';cache.mkdir(parents=True,exist_ok=True)
    if args.route_file:
        document=load(args.route_file);route_source=str(Path(args.route_file).resolve())
    else:
        positions=';'.join(f'{p["lon"]},{p["lat"]}' for p in points)
        url=args.osrm_base.rstrip('/')+'/route/v1/driving/'+positions+'?overview=full&geometries=geojson&steps=true&continue_straight=false'
        route_path=cache/('route-'+hashlib.sha256(url.encode()).hexdigest()[:20]+'.json')
        if not route_path.exists():route_path.write_bytes(request_bytes(url))
        document=load(route_path);route_source=url
    route=road_route(document,points,args.max_snap_m)
    route['source']['loaded_reference']=route_source
    route['source'].setdefault('reference',route_source)
    if route.get('synthetic_test_only'):
        assumptions=[note.replace('道路为根据已核对点位重建的路线','道路为合成测试线路') for note in assumptions]
        assumptions.insert(0,'合成测试道路，非真实道路定位成果，不能作为真实地图交付。')
    if args.basemap_json:
        map_meta_path=Path(args.basemap_json).resolve();basemap=load(map_meta_path)
        path=Path(basemap['path']);basemap['path']=str(path if path.is_absolute() else map_meta_path.parent/path)
        if not Path(basemap['path']).is_file():raise ValueError('本地底图文件不存在')
        z,_,bounds=map_plan(points,route,args.width,args.height,args.radius,int(basemap['zoom']))
        with Image.open(basemap['path']) as img:size=img.size
        origin=basemap['origin_px']
        if bounds[0]<origin[0] or bounds[1]<origin[1] or bounds[2]>origin[0]+size[0] or bounds[3]>origin[1]+size[1]:
            raise ValueError('本地底图不覆盖完整视野，请扩展拼图；不能用空白边填充真实地图')
    else:
        z,(xmin,ymin,xmax,ymax),_=map_plan(points,route,args.width,args.height,args.radius,args.map_zoom)
        tiles=cache/'tiles';tiles.mkdir(exist_ok=True)
        tasks=[(x,y) for x in range(xmin,xmax+1) for y in range(ymin,ymax+1)]
        service_hash=hashlib.sha256(args.tile_url.encode()).hexdigest()[:8]
        def fetch(task):
            x,y=task;path=tiles/f'{service_hash}-{z}-{x}-{y}.png'
            if not path.exists():
                content=request_bytes(args.tile_url.format(z=z,x=x,y=y))
                with Image.open(io.BytesIO(content)) as image:image.verify()
                path.write_bytes(content)
            return x,y,path
        print(f'准备底图：zoom {z}，{len(tasks)}个缓存瓦片',flush=True)
        canvas=Image.new('RGB',((xmax-xmin+1)*256,(ymax-ymin+1)*256))
        with ThreadPoolExecutor(max_workers=4) as pool:
            for x,y,path in pool.map(fetch,tasks):
                with Image.open(path) as tile:
                    if tile.size!=(256,256):raise ValueError('底图瓦片尺寸必须为256×256')
                    canvas.paste(tile.convert('RGB'),((x-xmin)*256,(y-ymin)*256))
        path=out/'basemap.jpg';canvas.save(path,quality=95)
        basemap={'path':str(path),'origin_px':[xmin*256,ymin*256],'zoom':z,'size':list(canvas.size),'source':args.tile_url}
    options={'width':args.width,'height':args.height,'radius':args.radius,'duration':trip['duration_seconds'],'fps':30}
    prepared={'trip':trip,'points':points,'route':route,'basemap':basemap,
              'render_options':options,'attribution':args.attribution,'assumptions':assumptions}
    destination=out/'prepared.json';destination.write_text(json.dumps(prepared,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'data-notes.md').write_text('# 数据说明\n\n'+''.join(f'- {s}\n' for s in assumptions)+f'\n换电次数：{len(trip["orders"])}；累计新增电量：{trip["energy_added_kwh"]:.1f}度。\n道路估算：{route["distance"]/1000:.1f}公里。\n\n{args.attribution}\n',encoding='utf-8')
    print(str(destination),flush=True);return prepared

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',required=True);p.add_argument('--output',required=True);p.add_argument('--cache')
    for endpoint in ('start','end'):
        for field in ('name','time','lon','lat'):
            p.add_argument(f'--{endpoint}-{field}',type=float if field in ('lon','lat') else str)
    p.add_argument('--duration',type=float);p.add_argument('--width',type=int,default=1080);p.add_argument('--height',type=int,default=1920)
    p.add_argument('--radius',type=int,default=540);p.add_argument('--map-zoom',type=int);p.add_argument('--route-file');p.add_argument('--basemap-json')
    p.add_argument('--osrm-base',default='https://router.project-osrm.org');p.add_argument('--max-snap-m',type=float,default=1000)
    p.add_argument('--tile-url',default=DEFAULT_MAP);p.add_argument('--attribution',default=ATTRIBUTION)
    args=p.parse_args()
    if args.width<=0 or args.height<=0 or not 320<=args.radius<=min(args.width,args.height):p.error('宽高必须为正数，半径须在320到短边范围内')
    if args.map_zoom is not None and not 0<=args.map_zoom<=18:p.error('地图zoom须为0—18')
    if not math.isfinite(args.max_snap_m) or args.max_snap_m<=0:p.error('道路吸附容差必须为正数')
    try:prepare(args)
    except (ValueError,TypeError,KeyError,RuntimeError,FileNotFoundError) as exc:p.exit(1,f'准备失败：{exc}\n')

if __name__=='__main__':main()
