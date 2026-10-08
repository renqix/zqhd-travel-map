#!/usr/bin/env python3
"""将已定位的换电订单和真实道路几何绘成全屏视频与透明角落图层。

只依赖 Pillow、numpy 和本机 FFmpeg；不下载地图，不读取私人项目。
底图为已准备的 Web Mercator 瓦片拼图，缺图时明确停止。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


def project(coordinates, zoom=6):
    """经纬度转成指定级别的全球瓦片像素，地图始终北向朝上。"""
    p = np.asarray(coordinates, dtype=float)
    lat = np.clip(p[:, 1], -85.05112878, 85.05112878)
    return np.column_stack(((p[:, 0] + 180) / 360 * 256 * 2**zoom,
                            (1 - np.arcsinh(np.tan(np.radians(lat))) / np.pi)
                            / 2 * 256 * 2**zoom))


def simplify(points, tolerance=.6):
    """仅压缩显示几何；插值和里程仍使用完整路线。"""
    if len(points) < 3:
        return points
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        section = points[a:b+1]
        vector = section[-1] - section[0]
        norm = float(vector @ vector)
        if norm:
            u = np.clip((section - section[0]) @ vector / norm, 0, 1)
            distances = np.linalg.norm(section - section[0] - u[:, None] * vector, axis=1)
        else:
            distances = np.linalg.norm(section - section[0], axis=1)
        relative = int(np.argmax(distances))
        if distances[relative] > tolerance:
            split = a + relative
            keep.add(split)
            stack.extend(((a, split), (split, b)))
    return points[sorted(keep)]


def geometric_lengths(coords):
    a = np.radians(np.asarray(coords, dtype=float))
    d = np.diff(a, axis=0)
    v = np.sin(d[:, 1]/2)**2 + np.cos(a[:-1, 1])*np.cos(a[1:, 1])*np.sin(d[:, 0]/2)**2
    v = np.clip(v, 0, 1)
    return np.r_[0, np.cumsum(6371008.8 * 2 * np.arctan2(np.sqrt(v), np.sqrt(1-v)))]


def required_basemap_bounds(points, route_legs, zoom=6, width=1080, height=1920,
                            radius=540):
    """返回拼图必需覆盖的全球瓦片像素矩形 [x0,y0,x1,y1]。

    prepare 阶段把矩形向外取整到瓦片边界，再加一格瓦片即可。
    route_legs 每项有 coordinates；points 每项有 lon、lat。
    同时覆盖全屏全景、局部跟随和圆角固定全景。
    """
    full = project(np.vstack([leg['coordinates'] for leg in route_legs]), zoom)
    pins = project([[p['lon'], p['lat']] for p in points], zoom)
    values = np.vstack((full, pins))
    lo, hi = values.min(axis=0), values.max(axis=0)
    span = np.maximum(hi-lo, 1.)
    center = (lo+hi)/2
    global_scale = min(.79*width/span[0], .38*height/span[1])
    offset = np.array([width*.5, height*.542])
    # 交叉淡入镜头处 center 和 scale 同时变化，使用更宽的保守覆盖。
    x0, y0 = lo-offset/global_scale
    x1, y1 = hi+(np.array([width, height])-offset)/global_scale
    overlay_scale = min(.602*radius/span[0], .241*radius/span[1])
    offset = np.array([.632*radius, .315*radius])
    o0 = center - offset/overlay_scale
    o1 = center + (radius-offset)/overlay_scale
    return [float(min(x0, o0[0])), float(min(y0, o0[1])),
            float(max(x1, o1[0])), float(max(y1, o1[1]))]


def find_font(explicit=None, bold=False):
    """优先中文字体；可通过参数和环境变量提供便携字体路径。"""
    configured = explicit or os.environ.get('TRAVEL_MAP_BOLD_FONT' if bold else 'TRAVEL_MAP_FONT')
    if configured:
        if not Path(configured).is_file():
            raise ValueError(f'字体文件不存在：{configured}')
        return str(configured)
    names = ['NotoSansCJK-Bold.ttc', 'NotoSansCJK-Regular.ttc'] if bold else ['NotoSansCJK-Regular.ttc']
    candidates = [
        '/Library/Fonts/NotoSansHans-Bold.otf' if bold else '/Library/Fonts/NotoSansHans-Regular.otf',
        '/System/Library/Fonts/PingFang.ttc',
        '/usr/share/fonts/opentype/noto/' + names[0],
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        'C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc',
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise ValueError('未发现中文字体。请安装 Noto Sans CJK 或用 --font、--bold-font 指定字体文件。')


def overlap(a, b, gap=7):
    return not (a[2]+gap <= b[0] or a[0] >= b[2]+gap or
                a[3]+gap <= b[1] or a[1] >= b[3]+gap)


def ease(x):
    return x*x*(3-2*x)


class Renderer:
    INK = (17, 39, 53)
    ACCENT = (0, 145, 164)
    BLUE = (33, 114, 230)
    WHITE = (255, 255, 255)
    YELLOW = (255, 205, 55)

    def __init__(self, prepared, prepared_path, duration=15, width=1080, height=1920,
                 radius=540, fps=30, font=None, bold_font=None, mode='all'):
        self.data = prepared
        self.width, self.height, self.radius, self.fps = width, height, radius, fps
        self.font_path, self.bold_path = find_font(font), find_font(bold_font or font, True)
        self.fonts = {}
        self.points = prepared['points']
        self.swap_count = len(self.points)-2
        if self.swap_count < 1:
            raise ValueError('至少需要一笔换电订单。')
        self.times = [dt.datetime.fromisoformat(p['time']) for p in self.points]
        for i, (a, b) in enumerate(zip(self.times, self.times[1:])):
            if b<a:
                raise ValueError('起点、换电订单和终点时间不能倒序。')
            if b==a and not all(abs(float(self.points[i+1][key])-float(self.points[i][key]))<1e-9
                                for key in ('lon', 'lat')):
                raise ValueError('相邻事件时间相同时必须位于同一地点。')
        if any(t.tzinfo != self.times[0].tzinfo for t in self.times):
            raise ValueError('所有事件时间必须使用一致时区。')
        self.elapsed = (self.times[-1]-self.times[0]).total_seconds()
        if self.elapsed<=0:
            raise ValueError('整个行程的现场经过时间必须大于零。')
        self.terminal_is_last_stop=(self.times[-1]==self.times[-2] and
                                   self.points[-1]['name']==self.points[-2]['name'])
        self.synthetic_test_only=bool(prepared.get('synthetic_test_only') or
                                      prepared.get('route', {}).get('synthetic_test_only'))
        self.example_only=bool(prepared.get('trip', {}).get('example_only'))
        self.added = np.cumsum([float(p.get('energy_added_kwh', 0)) for p in self.points])
        if not np.isfinite(self.added).all() or any(float(p.get('energy_added_kwh', 0)) < 0 for p in self.points):
            raise ValueError('换电新增电量必须是非负有限数。')
        basemap = prepared['basemap']
        self.zoom = int(basemap['zoom'])
        base_path = Path(basemap['path'])
        if not base_path.is_absolute():
            base_path = Path(prepared_path).parent/base_path
        if not base_path.is_file():
            raise ValueError(f'底图拼图文件不存在：{base_path}')
        self.base = Image.open(base_path).convert('RGB')
        self.base = ImageEnhance.Color(self.base).enhance(.65)
        self.base = ImageEnhance.Contrast(self.base).enhance(.87)
        self.origin = np.array(basemap['origin_px'], dtype=float)
        raw_legs = prepared['route']['legs']
        if len(raw_legs) != len(self.points)-1:
            raise ValueError('道路区间数量必须等于地点数量减一。')
        self.legs = []
        self.cumulative = [0.]
        for raw in raw_legs:
            coords = np.asarray(raw['coordinates'], dtype=float)
            distance = float(raw['distance'])
            if coords.ndim != 2 or coords.shape[1] != 2 or len(coords) < 2 or not np.isfinite(coords).all():
                raise ValueError('每个区间至少需要两组有效经纬度。')
            if not math.isfinite(distance) or distance < 0:
                raise ValueError('道路距离必须是非负有限数，单位米。')
            projected = project(coords, self.zoom)
            self.legs.append({'projected': projected, 'display': simplify(projected),
                              'lengths': geometric_lengths(coords), 'distance': distance})
            self.cumulative.append(self.cumulative[-1]+distance)
        self.total = self.cumulative[-1]
        if any(a==b and self.legs[i]['distance']>1e-6 for i, (a, b) in enumerate(zip(self.times, self.times[1:]))):
            raise ValueError('相邻事件同时间时对应道路区间距离必须为零。')
        self.full = np.vstack([leg['display'] for leg in self.legs])
        self.pins = project([[p['lon'], p['lat']] for p in self.points], self.zoom)
        values = np.vstack((self.full, self.pins))
        self.minimum, self.maximum = values.min(axis=0), values.max(axis=0)
        self.span = np.maximum(self.maximum-self.minimum, 1.)
        self.center = (self.minimum+self.maximum)/2
        self.global_scale = min(.79*width/self.span[0], .38*height/self.span[1])
        self.follow_scale = self.global_scale*3
        self.map_offset = np.array([width*.5, height*.542])
        self.duration = round(duration*fps)/fps
        self.frames = round(self.duration*fps)
        self.timeline = self.make_timeline()
        arrivals=[s for s in self.timeline if s['kind'] in ('swap', 'finish')]
        self.arrival_frames={s['index']: math.ceil(s['start']*fps-1e-7) for s in arrivals}
        arrival_values=list(self.arrival_frames.values())
        if (any(b<=a for a, b in zip(arrival_values, arrival_values[1:])) or
                any(frame>=self.frames for frame in arrival_values)):
            raise ValueError('当前视频时长和帧率无法逐站显示到达事件，请增加 --duration 或 --fps。')
        self.attribution = prepared.get('attribution', 'Map: OpenStreetMap contributors')
        # 在绘图前检查所有镜头的采样范围；禁止以黑块或单色掩盖缺失地图。
        required = required_basemap_bounds(self.points, raw_legs, self.zoom, width, height, radius)
        extent = [self.origin[0], self.origin[1],
                  self.origin[0]+self.base.width, self.origin[1]+self.base.height]
        if any(required[i] < extent[i]+2 for i in (0, 1)) or any(required[i] > extent[i]-2 for i in (2, 3)):
            raise ValueError(f'底图覆盖不足，请重新准备拼图。所需全球像素范围 {required}；已有 {extent}')
        self.overlay_labels = []
        if mode in ('all', 'overlay'):
            self.prepare_overlay()
            for label in self.overlay_labels:
                label['arrival_frame']=self.arrival_frames[label['index']]

    def font(self, size, bold=False):
        size = max(8, round(size))
        key = (size, bold)
        if key not in self.fonts:
            self.fonts[key] = ImageFont.truetype(self.bold_path if bold else self.font_path, size)
        return self.fonts[key]

    def text(self, draw, xy, value, size, bold=False, fill=None, anchor='la'):
        draw.text(xy, str(value), font=self.font(size, bold), fill=fill or self.INK, anchor=anchor)

    def fitted_text(self, draw, xy, value, size, max_width, bold=False, fill=None, anchor='la'):
        while size > 9 and draw.textlength(str(value), font=self.font(size, bold)) > max_width:
            size -= 1
        self.text(draw, xy, value, size, bold, fill, anchor)

    def make_timeline(self):
        intro, finish = self.duration*.0433333333, self.duration*.12
        dwell = min(.35, self.duration*.025)
        # 即使订单较多，也为移动轨迹保留至少一半视频时长。
        dwell = min(dwell, self.duration*.2/self.swap_count)
        available = self.duration-intro-finish-dwell*self.swap_count
        weights = np.sqrt(np.maximum([leg['distance'] for leg in self.legs], 1.))
        durations = available*weights/weights.sum()
        timeline = [{'kind': 'intro', 'index': 0, 'start': 0., 'end': intro}]
        time = intro
        for i, duration in enumerate(durations):
            timeline.append({'kind': 'drive', 'index': i, 'start': time, 'end': time+float(duration)})
            time += float(duration)
            if i < self.swap_count:
                timeline.append({'kind': 'swap', 'index': i+1, 'start': time, 'end': time+dwell})
                time += dwell
        timeline.append({'kind': 'finish', 'index': len(self.points)-1, 'start': time, 'end': self.duration})
        return timeline

    def position(self, i, fraction):
        leg = self.legs[i]
        target = float(fraction)*leg['lengths'][-1]
        j = min(len(leg['lengths'])-2, max(0, int(np.searchsorted(leg['lengths'], target)-1)))
        span = leg['lengths'][j+1]-leg['lengths'][j]
        u = (target-leg['lengths'][j])/span if span else 0
        return leg['projected'][j]*(1-u)+leg['projected'][j+1]*u, j

    def state(self, time):
        # 与 arrival_frame 的浮点容差保持一致，避免理论整数帧晚亮一帧。
        epsilon=1e-7/self.fps
        stage = next((s for s in self.timeline if s['start'] <= time+epsilon < s['end']), self.timeline[-1])
        u = float(np.clip((time-stage['start'])/(stage['end']-stage['start']), 0, 1))
        kind, i = stage['kind'], stage['index']
        if kind == 'intro':
            p = self.legs[0]['projected'][0]
            blend = ease(float(np.clip((u-.18)/.82, 0, 1)))
            center = self.center*(1-blend)+p*blend
            scale = self.global_scale*(1-blend)+self.follow_scale*blend
            return stage, u, p, center, scale, 0., self.times[0], 0, 0., -1
        if kind == 'drive':
            p, j = self.position(i, u)
            return stage, u, p, p, self.follow_scale, self.cumulative[i]+u*self.legs[i]['distance'], \
                self.times[i]+(self.times[i+1]-self.times[i])*u, min(i, self.swap_count), self.added[i], j
        if kind == 'swap':
            p = self.legs[i-1]['projected'][-1]
            return stage, u, p, p, self.follow_scale, self.cumulative[i], self.times[i], i, self.added[i], -1
        p = self.legs[-1]['projected'][-1]
        blend = ease(float(np.clip((u-.13)/.42, 0, 1)))
        return stage, u, p, p*(1-blend)+self.center*blend, \
            self.follow_scale*(1-blend)+self.global_scale*blend, self.total, self.times[-1], \
            self.swap_count, self.added[-1], -1

    def done_path(self, state):
        stage, _, p, *_, j = state
        kind, i = stage['kind'], stage['index']
        if kind == 'finish':
            return self.full
        if kind == 'intro':
            return np.empty((0, 2))
        if kind == 'swap':
            return np.vstack([leg['display'] for leg in self.legs[:i]])
        prior = [leg['display'] for leg in self.legs[:i]]
        return np.vstack(prior+[simplify(self.legs[i]['projected'][:j+1]), p[None, :]])

    def sample_map(self, size, center, scale, offset):
        a = 1/scale
        c, f = center-self.origin-np.asarray(offset)*a
        return self.base.transform(size, Image.Transform.AFFINE, (a, 0, c, 0, a, f), Image.Resampling.BILINEAR)

    def draw_vehicle(self, draw, position, state, screen, radius, color):
        stage, u, p = state[:3]
        if stage['kind'] == 'drive':
            q, _ = self.position(stage['index'], min(1, u+.008))
            delta = q-p
        else:
            delta = np.array([-1., .3])
        angle = math.atan2(delta[1], delta[0])
        direction = np.array([math.cos(angle), math.sin(angle)])
        normal = np.array([-direction[1], direction[0]])
        x, y = position
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=self.WHITE, outline=color,
                     width=max(1, round(radius*.14)))
        triangle = [position+direction*radius*.71, position-direction*radius*.46+normal*radius*.46,
                    position-direction*radius*.31, position-direction*radius*.46-normal*radius*.46]
        draw.polygon([tuple(v) for v in triangle], fill=color)

    def fullscreen(self, time):
        state = self.state(time)
        stage, u, p, center, scale, distance, clock, swaps, added, _ = state
        image = self.sample_map((self.width, self.height), center, scale, self.map_offset)
        draw = ImageDraw.Draw(image)
        screen = lambda points: (points-center)*scale+self.map_offset
        sx, sy = self.width/1080, self.height/1920
        unit = min(sx, sy)
        def box(coords, fill=self.WHITE, outline=None):
            bounds = (coords[0]*sx, coords[1]*sy, coords[2]*sx, coords[3]*sy)
            draw.rounded_rectangle(bounds, radius=round(22*unit), fill=fill, outline=outline,
                                   width=max(1, round(3*unit)))
        def txt(x, y, value, size, bold=False, fill=None, anchor='la', max_width=None):
            if max_width:
                self.fitted_text(draw, (x*sx, y*sy), value, round(size*unit), max_width*sx, bold, fill, anchor)
            else:
                self.text(draw, (x*sx, y*sy), value, size*unit, bold, fill, anchor)
        def line(points, color, width):
            if len(points)>1:
                draw.line([tuple(v) for v in screen(points)], fill=color, width=max(1, round(width*unit)), joint='curve')
        line(self.full, self.WHITE, 13)
        line(self.full, (142, 165, 171), 6)
        line(self.done_path(state), self.WHITE, 18)
        line(self.done_path(state), self.ACCENT, 11)
        pins = screen(self.pins)
        for i, (x, y) in enumerate(pins):
            if self.terminal_is_last_stop and i==len(self.points)-1:
                continue
            if -30 < x < self.width+30 and self.height*.33 < y < self.height*.8:
                radius = round((14 if scale < self.global_scale*1.25 else 20)*unit)
                draw.ellipse((x-radius-3, y-radius-3, x+radius+3, y+radius+3), fill=self.WHITE)
                draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=self.ACCENT if i<=swaps else (139, 159, 165))
                if 0 < i <= self.swap_count:
                    self.text(draw, (x, y), i, 24*unit, True, self.WHITE, 'mm')
        if stage['kind'] == 'finish' and scale <= self.global_scale*1.25:
            cx, cy=screen(p[None, :])[0]
            occupied = [(cx-44*unit, cy-44*unit, cx+44*unit, cy+44*unit)]
            for i in range(1, swaps+1):
                x, y = pins[i]
                label = f'{i:02d} '+self.points[i]['name']
                fontsize = 24*unit
                labelwidth = min(self.width*.45, draw.textlength(label, font=self.font(fontsize, True))+24*unit)
                for dy in (-64, 40, -116, 92, -176, 150, -230, 210):
                    bx = float(np.clip(x-labelwidth/2, 48*sx, self.width-labelwidth-48*sx))
                    by = float(np.clip(y+dy*unit, 720*sy, 1430*sy))
                    bounds = (bx, by, bx+labelwidth, by+42*unit)
                    if all(not overlap(bounds, other, 6*unit) for other in occupied):
                        break
                occupied.append(bounds)
                draw.line((x, y, (bounds[0]+bounds[2])/2, bounds[1]+21*unit), fill=self.ACCENT, width=max(1, round(2*unit)))
                draw.rounded_rectangle(bounds, radius=round(10*unit), fill=self.WHITE)
                self.fitted_text(draw, (bounds[0]+12*unit, bounds[1]+5*unit), label, fontsize, labelwidth-24*unit, True)
        self.draw_vehicle(draw, screen(p[None, :])[0], state, screen, max(9, round(35*unit)), self.ACCENT)
        box((32, 38, 1048, 185))
        txt(58, 52, '咱去换电 · 加电之旅', 29, True, self.ACCENT)
        txt(54, 98, self.points[0]['name']+' → '+self.points[-1]['name'], 60, True, max_width=960)
        box((40, 214, 650, 598), self.INK)
        seconds = max(0, round((clock-self.times[0]).total_seconds()))
        rows = [('累计里程（估算）', f'{distance/1000:,.0f} km'),
                ('行程用时', f'{seconds//3600:02d}时{seconds//60%60:02d}分'),
                ('累计补充电量', f'{added:.1f} 度'),
                ('换电次数', f'{swaps} / {self.swap_count} 次')]
        for row, (label, value) in enumerate(rows):
            y = 275+row*87
            txt(66, y, label, 26, fill=(180, 205, 213), anchor='lm')
            txt(621, y, value, 40, True, self.YELLOW if row==3 else self.WHITE, 'rm', max_width=300)
            if row<3:
                draw.line((66*sx, (y+44)*sy, 621*sx, (y+44)*sy), fill=(46, 69, 81), width=1)
        txt(48, 634, clock.strftime('%m月%d日  %H:%M'), 40, True)
        box((932, 235, 1035, 370))
        txt(984, 251, 'N', 32, True, anchor='ma')
        draw.polygon([(984*sx, 297*sy), (965*sx, 338*sy), (984*sx, 325*sy), (1003*sx, 338*sy)], fill=self.INK)
        departing = stage['kind']=='drive' and stage['index']>0 and time-stage['start']<min(.35, self.duration*.025)
        if stage['kind']=='swap' or departing:
            i=stage['index']
            box((48, 1168, 1032, 1438), outline=self.ACCENT)
            txt(78, 1190, f'第 {i:02d} 次换电 · {self.times[i]:%m月%d日 %H:%M}', 30, True, self.ACCENT)
            txt(76, 1240, self.points[i]['name'], 62, True, max_width=705)
            txt(78, 1332, f'上一区间 {self.legs[i-1]["distance"]/1000:.1f} km（估算）', 28)
            txt(999, 1247, f'+{self.points[i].get("energy_added_kwh", 0):.1f}', 58, True, self.ACCENT, 'ra')
            txt(997, 1320, '度新增电量', 26, anchor='ra')
        elif stage['kind'] in ('intro', 'finish'):
            index=0 if stage['kind']=='intro' else len(self.points)-1
            box((48, 1438, 1032, 1548))
            txt(76, 1453, ('出发 · ' if index==0 else '抵达 · ')+self.points[index]['name'], 46, True, max_width=650)
            txt(999, 1489, self.times[index].strftime('%m月%d日 %H:%M'), 30, True, self.ACCENT, 'ra')
        else:
            box((48, 1448, 1032, 1548))
            txt(75, 1470, '前往 '+self.points[stage['index']+1]['name'], 40, True, max_width=900)
        box((40, 1590, 1040, 1805), self.INK)
        txt(68, 1610, '累计换电新增电量', 29, fill=(180, 205, 213))
        txt(1010, 1601, f'{added:.1f} 度', 52, True, self.WHITE, 'ra')
        draw.rounded_rectangle((68*sx, 1684*sy, 1012*sx, 1695*sy), radius=round(5*unit), fill=(76, 100, 109))
        fraction=float(np.clip(seconds/self.elapsed, 0, 1))
        if fraction>0:
            draw.rounded_rectangle((68*sx, 1684*sy, (68+944*fraction)*sx, 1695*sy), radius=round(5*unit), fill=self.YELLOW)
        txt(68, 1720, self.times[0].strftime('%m.%d %H:%M')+' 出发', 27, True, self.WHITE)
        txt(1012, 1720, self.times[-1].strftime('%m.%d %H:%M')+' 抵达', 27, True, self.WHITE, 'ra')
        txt(68, 1761, '补充电量取自订单 · 区间位置按道路插值', 24, fill=(180, 205, 213))
        draw.rectangle((0, 1814*sy, self.width, self.height), fill=self.WHITE)
        route_description='合成测试线路' if self.synthetic_test_only else '真实道路重建'
        if self.example_only:
            route_description+=' · 虚构订单示例'
        txt(540, 1821, route_description+' · 里程估算', 25, anchor='ma', max_width=1000)
        # 供应商完整来源较长，使用实际换行而不是压成一条被裁切的小字。
        credit_size=max(8, round(13*unit))
        credit_lines=['']
        for char in self.attribution:
            if char=='\n':
                credit_lines.append('')
            elif (credit_lines[-1] and
                  draw.textlength(credit_lines[-1]+char, font=self.font(credit_size))>1000*sx):
                credit_lines.append(char)
            else:
                credit_lines[-1]+=char
        if len(credit_lines)*max(11*unit, credit_size+3)>(self.height-1857*sy):
            raise ValueError('全屏地图来源过长，底部署名区无法完整显示；请增加画布高度或缩小来源字体。')
        for row, credit in enumerate(credit_lines):
            self.text(draw, (self.width/2, 1857*sy+row*max(11*unit, credit_size+3)),
                      credit, credit_size, anchor='ma')
        if len(credit_lines)==1:
            txt(540, 1884, '路线与时钟为事件间插值 · 非原始 GPS 轨迹', 20, anchor='ma')
        return image

    def prepare_overlay(self):
        r=self.radius
        self.overlay_scale=min(.602*r/self.span[0], .241*r/self.span[1])
        self.overlay_offset=np.array([.632*r, .315*r])
        self.overlay_screen=lambda points: (points-self.center)*self.overlay_scale+self.overlay_offset
        self.overlay_pins=self.overlay_screen(self.pins)
        yy, xx=np.indices((r, r))
        progress=np.clip((r-np.hypot(xx-r, yy))/(r*.2), 0, 1)
        self.feather=(progress**3*(10-15*progress+6*progress**2)).astype('float32')
        image=self.sample_map((r, r), self.center, self.overlay_scale, self.overlay_offset)
        image=ImageEnhance.Color(image).enhance(.25)
        image=Image.blend(image, Image.new('RGB', (r, r), (247, 250, 252)), .2)
        blurred=image.filter(ImageFilter.GaussianBlur(5*r/540))
        image=Image.composite(image, blurred, Image.fromarray((self.feather*255).astype('uint8'))).convert('RGBA')
        image.putalpha(195)
        self.overlay_base=image
        # 来源署名留在不羽化的顶边区域，全文换行，不能随地图采样而被裁出。
        self.credit_font_size=max(8, round(8*r/540))
        self.credit_position=(round(120*r/540), max(1, round(3*r/540)))
        self.credit_line_height=self.credit_font_size+max(2, round(2*r/540))
        scratch=ImageDraw.Draw(Image.new('RGB', (r, r)))
        self.credit_lines=['']
        max_width=r-self.credit_position[0]-max(4, round(8*r/540))
        for char in self.attribution:
            if char=='\n':
                self.credit_lines.append('')
            elif (self.credit_lines[-1] and
                  scratch.textlength(self.credit_lines[-1]+char, font=self.font(self.credit_font_size))>max_width):
                self.credit_lines.append(char)
            else:
                self.credit_lines[-1]+=char
        self.credit_box=(self.credit_position[0], self.credit_position[1],
                         r-max(4, round(8*r/540)),
                         self.credit_position[1]+len(self.credit_lines)*self.credit_line_height+2)
        self.overlay_labels=self.layout_labels()

    def layout_labels(self):
        r=self.radius
        unit=r/540
        scratch=ImageDraw.Draw(Image.new('RGB', (r, r)))
        labels=[]
        for i in range(1, self.swap_count+1):
            is_stop=True
            detail=f'区间 {self.legs[i-1]["distance"]/1000:.1f}km'
            if is_stop:
                detail+=f' · {self.points[i].get("energy_added_kwh", 0):.1f}度'
            name=self.points[i]['name']
            lines=['']
            max_width=r*.38
            for char in name:
                if scratch.textlength(lines[-1]+char, font=self.font(16*unit, True))>max_width and lines[-1]:
                    lines.append('')
                lines[-1]+=char
            width=math.ceil(max([scratch.textlength(s, font=self.font(16*unit, True)) for s in lines]
                                +[scratch.textlength(detail, font=self.font(12*unit))]))+round(12*unit)
            height=round((len(lines)*19+20)*unit)
            labels.append({'index': i, 'name': name, 'lines': lines, 'detail': detail,
                           'distance_km': self.legs[i-1]['distance']/1000,
                           'energy_added_kwh': self.points[i].get('energy_added_kwh', 0),
                           'w': width, 'h': height, 'font_size': round(16*unit)})
        blocked=Image.new('L', (r, r))
        d=ImageDraw.Draw(blocked)
        d.rectangle(self.credit_box, fill=255)
        d.line([tuple(v) for v in self.overlay_screen(self.full)], fill=255, width=max(4, round(14*unit)))
        for x, y in self.overlay_pins:
            d.ellipse((x-9*unit, y-9*unit, x+9*unit, y+9*unit), fill=255)
        x, y=self.overlay_pins[0]
        self.start_label_anchor=(min(r-10*unit, x+12*unit), y-19*unit)
        self.start_label_max_width=r*(.16 if len(self.credit_lines)>1 else .24)
        d.rectangle((self.start_label_anchor[0]-self.start_label_max_width-4*unit, y-42*unit,
                     self.start_label_anchor[0]+4*unit, y-12*unit), fill=255)
        finish_box=tuple(round(v*unit) for v in (368, 328, 517, 371))
        if not self.terminal_is_last_stop:
            d.rectangle(finish_box, fill=255)
        integral=np.pad((np.array(blocked)>0).astype('int32'), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        def count(box):
            x0, y0, x1, y1=map(int, box)
            return integral[y1, x1]-integral[y0, x1]-integral[y1, x0]+integral[y0, x0]
        candidates={}
        step=max(4, round(8*unit))
        for label in labels:
            i, w, h=label['index'], label['w'], label['h']
            px, py=self.overlay_pins[i]
            items=[]
            for y in range(max(3, round(8*unit)), round(r*.735)-h, step):
                for x in range(round(r*.17), r-w-max(3, round(7*unit))+1, step):
                    bounds=(x, y, x+w, y+h)
                    if max(math.hypot(r-xc, yc) for xc in (x, x+w) for yc in (y, y+h)) > r*.8-2:
                        continue
                    if count(bounds)>0:
                        continue
                    ax, ay=np.clip(px, x, x+w), np.clip(py, y, y+h)
                    score=math.hypot(px-ax, py-ay)+.08*abs(y+h/2-py)
                    items.append((score, bounds))
            if not items:
                raise ValueError(f'站点“{label["name"]}”无可用标注位置，请简化站名或增大 --radius。')
            candidates[i]=sorted(items, key=lambda item:item[0])
        beam=[(0., {})]
        for i in sorted(candidates, key=lambda i:len(candidates[i])):
            expanded=[]
            for score, placed in beam:
                valid=0
                for cost, bounds in candidates[i]:
                    if any(overlap(bounds, other, 7*unit) for other in placed.values()):
                        continue
                    expanded.append((score+cost, {**placed, i:bounds}))
                    valid+=1
                    if valid>=60:
                        break
            if not expanded:
                raise ValueError('圆角地图标注太密，无法在不重叠的情况下显示全部站点。请缩短站名或增大 --radius。')
            beam=sorted(expanded, key=lambda item:item[0])[:400]
        layout=beam[0][1]
        for label in labels:
            label['box']=layout[label['index']]
        # 终点预留固定区域，避免第十张卡片挤占全部换电站的避让空间。
        if not self.terminal_is_last_stop:
            labels.append({'index': len(self.points)-1, 'name': self.points[-1]['name'],
                           'lines': [self.points[-1]['name']],
                           'detail': f'区间 {self.legs[-1]["distance"]/1000:.1f}km',
                           'distance_km': self.legs[-1]['distance']/1000, 'energy_added_kwh': 0,
                           'w': finish_box[2]-finish_box[0], 'h': finish_box[3]-finish_box[1],
                           'box': finish_box, 'font_size': round(16*unit), 'endpoint': True})
        return labels

    def overlay(self, time):
        state=self.state(time)
        stage, u, p, _, _, _, _, swaps, _, _=state
        image=self.overlay_base.copy()
        draw=ImageDraw.Draw(image)
        unit=self.radius/540
        def line(points, color, width):
            if len(points)>1:
                draw.line([tuple(v) for v in self.overlay_screen(points)], fill=color,
                          width=max(1, round(width*unit)), joint='curve')
        line(self.full, (255, 255, 255, 235), 8)
        line(self.full, (129, 145, 162, 220), 4)
        line(self.done_path(state), (255, 255, 255, 250), 8)
        line(self.done_path(state), (*self.BLUE, 255), 4)
        visible_labels=[]
        for label in self.overlay_labels:
            i=label['index']
            if i>swaps and not (i==len(self.points)-1 and stage['kind']=='finish'):
                continue
            visible_labels.append(label)
        # 所有引线先画，卡片随后覆盖，避免后到站的引线划过早先文字。
        for label in visible_labels:
            i=label['index']
            x0, y0, x1, y1=label['box']
            px, py=self.overlay_pins[i]
            ax, ay=float(np.clip(px, x0+5*unit, x1-5*unit)), float(np.clip(py, y0+5*unit, y1-5*unit))
            draw.line((px, py, ax, ay), fill=(*self.BLUE, 170), width=max(1, round(unit)))
        for label in visible_labels:
            x0, y0, x1, y1=label['box']
            draw.rounded_rectangle((x0, y0, x1, y1), radius=max(2, round(5*unit)), fill=(255, 255, 255, 213))
            for row, text in enumerate(label['lines']):
                self.fitted_text(draw, (x0+6*unit, y0+(1+row*19)*unit), text, 16*unit,
                                 x1-x0-12*unit, True, (*self.BLUE, 255))
            self.text(draw, (x0+6*unit, y0+(len(label['lines'])*19+3)*unit), label['detail'], 12*unit, fill=(*self.INK, 255))
        for i, (x, y) in enumerate(self.overlay_pins):
            if self.terminal_is_last_stop and i==len(self.points)-1:
                continue
            radius=(5 if i in (0, len(self.points)-1) else 4)*unit
            color=self.BLUE if i<=swaps or (i==len(self.points)-1 and stage['kind']=='finish') else (92, 107, 124)
            if stage['kind']=='swap' and i==stage['index']:
                pulse=(8+math.sin(u*math.pi)*4)*unit
                draw.ellipse((x-pulse, y-pulse, x+pulse, y+pulse), outline=(*self.BLUE, 150), width=max(1, round(2*unit)))
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=(*color, 255),
                         outline=(255, 255, 255, 255), width=max(1, round(unit)))
        # 起点文字简短，完整名称已在元数据和全屏版保存。
        self.fitted_text(draw, self.start_label_anchor, self.points[0]['name'], 15*unit,
                         self.start_label_max_width, True, (*self.INK, 255), 'rb')
        self.draw_vehicle(draw, self.overlay_screen(p[None, :])[0], state,
                          self.overlay_screen, max(5, round(9*unit)), (*self.BLUE, 255))
        for row, credit in enumerate(self.credit_lines):
            self.text(draw, (self.credit_position[0], self.credit_position[1]+row*self.credit_line_height),
                      credit, self.credit_font_size, fill=(43, 56, 66, 255))
        alpha=(np.array(image.getchannel('A'), dtype='float32')*self.feather).round().astype('uint8')
        image.putalpha(Image.fromarray(alpha))
        return image

    def backdrop(self):
        yy, xx=np.indices((self.height, self.width))
        blend=(xx/self.width*.55+yy/self.height*.45)[..., None]
        rgb=((1-blend)*np.array([48, 65, 88])+blend*np.array([171, 192, 199])).astype('uint8')
        return Image.fromarray(rgb).convert('RGBA')

    def corner_canvas(self, corner):
        canvas=Image.new('RGBA', (self.width, self.height), (0, 0, 0, 0))
        canvas.paste(corner, (self.width-self.radius, 0))
        return canvas


def encoder(ffmpeg, path, size, fps, alpha=False):
    command=[ffmpeg, '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgba' if alpha else 'rgb24',
             '-s', f'{size[0]}x{size[1]}', '-r', str(fps), '-i', '-', '-an']
    if alpha:
        command+=['-c:v', 'prores_ks', '-profile:v', '4', '-pix_fmt', 'yuva444p10le',
                  '-alpha_bits', '16', '-vendor', 'apl0']
    else:
        command+=['-c:v', 'libx264', '-preset', 'fast', '-crf', '19', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
    return subprocess.Popen(command+[str(path)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mode', choices=('all', 'fullscreen', 'overlay'), default='all')
    parser.add_argument('--duration', type=float)
    parser.add_argument('--width', type=int)
    parser.add_argument('--height', type=int)
    parser.add_argument('--radius', type=int)
    parser.add_argument('--fps', type=int)
    parser.add_argument('--font')
    parser.add_argument('--bold-font')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--preview-only', action='store_true')
    parser.add_argument('--overwrite', action='store_true')
    args=parser.parse_args(argv)
    prepared_path=Path(args.prepared)
    prepared=json.loads(prepared_path.read_text(encoding='utf-8'))
    options=prepared.get('render_options', {})
    for key, default, cast in [('duration', 15, float), ('width', 1080, int),
                              ('height', 1920, int), ('radius', 540, int), ('fps', 30, int)]:
        value=getattr(args, key)
        if value is None:
            option=options.get(key)
            setattr(args, key, cast(default if option is None else option))
    if not math.isfinite(args.duration) or args.duration<2:
        parser.error('--duration 至少为 2 秒。')
    if args.width<320 or args.height<320 or args.width%2 or args.height%2:
        parser.error('宽高须为至少 320 的偶数。')
    if not 320<=args.radius<=min(args.width, args.height):
        parser.error('--radius 须至少 320，且不超出画布宽高。')
    if not 1<=args.fps<=120:
        parser.error('--fps 须在 1 到 120 之间。')
    ffmpeg=args.ffmpeg or os.environ.get('TRAVEL_MAP_FFMPEG') or shutil.which('ffmpeg')
    if not args.preview_only and not ffmpeg:
        parser.error('未发现 FFmpeg，请安装或使用 --ffmpeg 指定。')
    output=Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    names=['manifest.json', 'timeline.json', 'fullscreen.mp4', 'corner-alpha.mov',
           'corner-canvas-alpha.mov', 'corner-preview.mp4']
    if not args.overwrite and any((output/name).exists() for name in names):
        parser.error('输出目录已有同名成果，请改用新目录或加 --overwrite。')
    renderer=Renderer(prepared, prepared_path, args.duration, args.width, args.height,
                      args.radius, args.fps, args.font, args.bold_font, args.mode)
    (output/'timeline.json').write_text(json.dumps(renderer.timeline, ensure_ascii=False, indent=2), encoding='utf-8')
    previews=output/'previews'
    previews.mkdir(exist_ok=True)
    times=[0., renderer.duration*.5, (renderer.frames-1)/args.fps]
    times += [(s['start']+s['end'])/2 for s in renderer.timeline if s['kind']=='swap']
    if args.mode in ('all', 'fullscreen'):
        for i, time in enumerate(times):
            renderer.fullscreen(time).save(previews/f'fullscreen-{i:02d}.jpg', quality=93)
    background=renderer.backdrop() if args.mode in ('all', 'overlay') else None
    if args.mode in ('all', 'overlay'):
        for i, time in enumerate(times):
            image=renderer.overlay(time)
            image.save(previews/f'corner-{i:02d}.png')
            Image.alpha_composite(background, renderer.corner_canvas(image)).convert('RGB').save(previews/f'composite-{i:02d}.jpg', quality=93)
    manifest={'prepared_source': prepared_path.name,
              'duration_seconds': renderer.duration, 'fps': args.fps, 'frame_count': renderer.frames,
              'canvas': [args.width, args.height], 'corner_center': [args.width, 0],
              'corner_radius': args.radius, 'feather_px': args.radius*.2,
              'swap_count': renderer.swap_count, 'energy_added_kwh': round(float(renderer.added[-1]), 4),
              'endpoint_is_last_swap': renderer.terminal_is_last_stop,
              'synthetic_test_only': renderer.synthetic_test_only, 'example_only': renderer.example_only,
              'road_estimate_km': round(renderer.total/1000, 4), 'elapsed_hours': renderer.elapsed/3600,
              'distance_type': '道路估算', 'energy_type': '订单新增电量',
              'trajectory_type': ('合成测试线路' if renderer.synthetic_test_only else '真实道路几何上的事件间插值'),
              'attribution': renderer.attribution, 'assumptions': prepared.get('assumptions', []),
              'labels': renderer.overlay_labels, 'outputs': [], 'status': 'preview_only'}
    if args.preview_only:
        (output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'关键帧已生成：{output}', flush=True)
        return 0
    processes=[]
    try:
        if args.mode in ('all', 'fullscreen'):
            processes.append(('fullscreen', encoder(ffmpeg, output/'fullscreen.mp4', (args.width, args.height), args.fps)))
        if args.mode in ('all', 'overlay'):
            processes.append(('alpha', encoder(ffmpeg, output/'corner-alpha.mov', (args.radius, args.radius), args.fps, True)))
            processes.append(('preview', encoder(ffmpeg, output/'corner-preview.mp4', (args.width, args.height), args.fps)))
        for frame in range(renderer.frames):
            time=frame/args.fps
            corner=renderer.overlay(time) if args.mode in ('all', 'overlay') else None
            for kind, process in processes:
                if kind=='fullscreen':
                    image=renderer.fullscreen(time)
                elif kind=='alpha':
                    image=corner
                else:
                    image=Image.alpha_composite(background, renderer.corner_canvas(corner)).convert('RGB')
                process.stdin.write(image.tobytes())
            if frame%max(args.fps*5, 1)==0:
                print(f'绘制 {time:.1f} / {renderer.duration:.1f} 秒', flush=True)
        for kind, process in processes:
            process.stdin.close()
            error=process.stderr.read().decode('utf-8', errors='replace')
            if process.wait()!=0:
                raise RuntimeError(f'{kind} 编码失败：{error.strip()}')
    except BaseException as error:
        messages=[]
        for kind, process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
            if isinstance(error, BrokenPipeError):
                message=process.stderr.read().decode('utf-8', errors='replace').strip()
                if message:
                    messages.append(kind+': '+message)
        if isinstance(error, BrokenPipeError):
            raise RuntimeError('FFmpeg 视频管道中断。'+' '.join(messages)) from None
        raise
    manifest['outputs']=['fullscreen.mp4'] if args.mode=='fullscreen' else ['corner-alpha.mov', 'corner-canvas-alpha.mov', 'corner-preview.mp4']
    if args.mode=='all':
        manifest['outputs'].insert(0, 'fullscreen.mp4')
    if args.mode in ('all', 'overlay'):
        command=[ffmpeg, '-y', '-v', 'error', '-i', str(output/'corner-alpha.mov'), '-vf',
                 f'pad={args.width}:{args.height}:{args.width-args.radius}:0:color=black@0',
                 '-an', '-c:v', 'prores_ks', '-profile:v', '4', '-pix_fmt', 'yuva444p10le',
                 '-alpha_bits', '16', '-vendor', 'apl0', str(output/'corner-canvas-alpha.mov')]
        subprocess.run(command, check=True)
    manifest['status']='encoded_requires_visual_review'
    (output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'视频已导出，请检查关键帧、完整解码和透明通道：{output}', flush=True)
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'错误：{error}', file=sys.stderr)
        sys.exit(1)
