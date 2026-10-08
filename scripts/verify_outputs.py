#!/usr/bin/env python3
"""实际解码导出视频，核对规格、透明边缘和到站标签。"""
import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import numpy as np


def binary(name,explicit=None):
    result=explicit or shutil.which(name)
    if not result:raise ValueError(f'未找到{name}，请指定可执行文件')
    return result


def run(args):
    result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode or result.stderr.strip():
        raise ValueError(result.stderr.decode('utf-8',errors='replace').strip() or '解码失败')
    return result.stdout


def sample_frames(ffmpeg,path,indices,width,height):
    indices=sorted(set(indices))
    expression='+'.join(f'eq(n\\,{index})' for index in indices)
    blob=run([ffmpeg,'-v','error','-i',str(path),'-vf',f'select={expression}',
              '-vsync','0','-pix_fmt','rgba','-f','rawvideo','pipe:1'])
    size=width*height*4
    if len(blob)!=size*len(indices):raise ValueError(f'{path.name}没有全部抽样帧')
    return {frame:np.frombuffer(blob[i*size:(i+1)*size],dtype=np.uint8).reshape(height,width,4)
            for i,frame in enumerate(indices)}


def verify(args):
    path=Path(args.manifest).resolve();root=path.parent
    manifest=json.loads(path.read_text(encoding='utf-8'))
    if not manifest.get('outputs'):raise ValueError('此清单只含关键帧，没有已编码视频')
    ffmpeg=binary('ffmpeg',args.ffmpeg);ffprobe=binary('ffprobe',args.ffprobe)
    frames=int(manifest['frame_count']);fps=float(manifest['fps'])
    canvas=manifest['canvas'];radius=int(manifest['corner_radius'])
    result={'checks':[],'files':{},'status':'passed_requires_visual_review'}
    for name in manifest['outputs']:
        file=root/name
        if not file.is_file():raise ValueError(f'输出不存在：{name}')
        # count_frames 会逐帧解码；不只看容器声明的时长。
        doc=json.loads(run([ffprobe,'-v','error','-select_streams','v:0','-count_frames',
                            '-show_entries','stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_read_frames,duration',
                            '-of','json',str(file)]))
        info=doc['streams'][0]
        expected=[radius,radius] if name=='corner-alpha.mov' else canvas
        if [info['width'],info['height']]!=expected:raise ValueError(f'{name}尺寸不匹配')
        if int(info['nb_read_frames'])!=frames:raise ValueError(f'{name}帧数不匹配')
        if abs(float(Fraction(info['r_frame_rate']))-fps)>1e-6:raise ValueError(f'{name}帧率不匹配')
        if abs(float(info['duration'])-frames/fps)>1/fps:raise ValueError(f'{name}时长不匹配')
        if name.endswith('alpha.mov') and 'a' not in info['pix_fmt']:
            raise ValueError(f'{name}像素格式无Alpha')
        result['files'][name]=info
    result['checks'].append('全部视频完整解码，尺寸、帧数、帧率和时长符合清单')
    if 'corner-alpha.mov' in manifest['outputs']:
        timeline=json.loads((root/'timeline.json').read_text(encoding='utf-8'))
        labels=manifest.get('labels',[])
        indices=[label['index'] for label in labels]
        swaps=int(manifest['swap_count'])
        required=set(range(1,swaps+1))
        if not manifest.get('endpoint_is_last_swap',False):required.add(swaps+1)
        if len(indices)!=len(set(indices)) or set(indices)!=required:
            raise ValueError('标签索引遗漏、重复或与订单数量不符，无法验收到站显现')
        arrivals={}
        for label in labels:
            stage=next(s for s in timeline if s['index']==label['index'] and s['kind'] in ('swap','finish'))
            arrival=int(label.get('arrival_frame',math.ceil(stage['start']*fps-1e-7)))
            if not 0<arrival<frames:raise ValueError(f'{label["name"]}无可检查的到站帧，请延长视频')
            arrivals[label['index']]=arrival
        indices=[0,frames//2,frames-1]
        for arrival in arrivals.values():indices.extend([arrival-1,arrival])
        actual=sample_frames(ffmpeg,root/'corner-alpha.mov',indices,radius,radius)
        yy,xx=np.indices((radius,radius));distance=np.hypot(xx-radius,yy)
        outside=distance>=radius
        for frame,image in actual.items():
            alpha=image[:,:,3]
            if outside.any() and np.any(alpha[outside]):raise ValueError(f'第{frame}帧圆外有残留Alpha')
            if alpha.max()<150:raise ValueError('主体透明度异常')
        edge=actual[0][:,:,3][(distance>radius*.83)&(distance<radius*.99)]
        if len(np.unique(edge))<20:raise ValueError('Alpha羽化带没有连续渐隐')
        result['checks'].append('实际MOV圆外Alpha为0，边缘含连续半透明渐隐')
        details=[]
        for label in labels:
            x0,y0,x1,y1=map(int,label['box']);pad=max(3,int(radius/180))
            arrival=arrivals[label['index']]
            ratios=[]
            for index in (arrival-1,arrival):
                alpha=actual[index][y0+pad:y1-pad,x0+pad:x1-pad,3]
                ratios.append(float((alpha>205).mean()))
            # 地图基底Alpha为195，出现的卡片为213，检测卡片内域以排除圆角和引线。
            if ratios[0]>.12 or ratios[1]<.9:
                raise ValueError(f'{label["name"]}到站前后显现异常：{ratios}')
            details.append({'index':label['index'],'arrival_frame':arrival,
                            'card_before':ratios[0],'card_after':ratios[1]})
        result['arrival_checks']=details
        result['checks'].append('每个站点和终点的标签到站前隐藏、到站后显现')
        if 'corner-canvas-alpha.mov' in manifest['outputs']:
            canvas_frames=sample_frames(ffmpeg,root/'corner-canvas-alpha.mov',[0,frames-1],*canvas)
            for index,image in canvas_frames.items():
                alpha=image[:,:,3];x=canvas[0]-radius
                if np.any(alpha[:,:x]) or np.any(alpha[radius:,:]):raise ValueError('定位版画布地图外存在残留Alpha')
                if np.max(np.abs(alpha[:radius,x:].astype(int)-actual[index][:,:,3].astype(int)))>2:
                    raise ValueError('定位版与紧凑版Alpha不一致')
            result['checks'].append('定位版只占画布右上，透明范围与紧凑版一致')
    destination=root/'verification.json';destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':result['checks'],'report':str(destination)},ensure_ascii=False,indent=2))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',required=True)
    p.add_argument('--ffmpeg');p.add_argument('--ffprobe')
    args=p.parse_args()
    try:verify(args)
    except (ValueError,KeyError,StopIteration,FileNotFoundError) as exc:
        print(f'验收失败：{exc}',file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
