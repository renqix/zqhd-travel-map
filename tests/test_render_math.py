"""无地图与编码依赖的时间轴检查，保护短片节奏和到站帧。"""
import datetime as dt
import math
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from render_video import Renderer


def renderer_for_math(duration=2,swaps=9):
    r=Renderer.__new__(Renderer);r.duration=duration;r.swap_count=swaps
    r.fps=30;r.frames=round(duration*r.fps)
    r.points=[{'name':f'虚构点{i}'} for i in range(swaps+2)]
    r.times=[dt.datetime(2027,1,1)+dt.timedelta(hours=i) for i in range(swaps+2)]
    r.legs=[]
    for i in range(swaps+1):
        xy=np.array([[float(i),float(i)],[float(i+1),float(i+1)]])
        r.legs.append({'distance':(i+1)*1000.,'projected':xy,'display':xy,'lengths':np.array([0.,100.])})
    r.cumulative=np.r_[0.,np.cumsum([l['distance'] for l in r.legs])]
    r.total=r.cumulative[-1];r.added=np.r_[0.,np.arange(1,swaps+1)*10.,swaps*10.]
    r.full=np.vstack([l['display'] for l in r.legs]);r.center=r.full.mean(axis=0)
    r.global_scale=1;r.follow_scale=3;r.timeline=r.make_timeline()
    return r


class TimelineTests(unittest.TestCase):
    def test_two_seconds_nine_swaps_keep_motion_and_distinct_frames(self):
        r=renderer_for_math();stops=[s for s in r.timeline if s['kind']=='swap']
        self.assertLessEqual(sum(s['end']-s['start'] for s in stops),r.duration*.2+1e-9)
        frames=[math.ceil(s['start']*r.fps-1e-7) for s in stops]
        self.assertEqual(len(set(frames)),9)
        for station,arrival in enumerate(frames,1):
            self.assertEqual(r.state((arrival-1)/r.fps)[7],station-1)
            self.assertEqual(r.state(arrival/r.fps)[7],station)
        finish=r.timeline[-1];arrival=math.ceil(finish['start']*r.fps-1e-7)
        self.assertNotEqual(r.state((arrival-1)/r.fps)[0]['kind'],'finish')
        self.assertEqual(r.state(arrival/r.fps)[0]['kind'],'finish')
        self.assertEqual(r.state((r.frames-1)/r.fps)[0]['kind'],'finish')

    def test_timeline_scales_with_total_duration(self):
        for seconds in (2,6,15,30):
            r=renderer_for_math(seconds,3)
            self.assertAlmostEqual(r.timeline[0]['start'],0)
            self.assertAlmostEqual(r.timeline[-1]['end'],seconds)
            self.assertTrue(all(s['end']>s['start'] for s in r.timeline))
            self.assertAlmostEqual(sum(s['end']-s['start'] for s in r.timeline),seconds)

    def test_zero_distance_leg_is_stable(self):
        r=renderer_for_math(6,3);leg=r.legs[-1]
        leg['distance']=0;leg['lengths']=np.array([0.,0.]);leg['projected'][:]=[1.,1.]
        point,index=r.position(len(r.legs)-1,.5)
        self.assertEqual(index,0);self.assertTrue(np.array_equal(point,[1.,1.]))


if __name__=='__main__':unittest.main()
