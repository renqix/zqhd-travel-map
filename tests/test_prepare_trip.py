"""以虚构输入检查订单归一化和道路数据边界。"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from prepare_trip import normalize, road_route, map_plan
from render_video import required_basemap_bounds


def fixture():
    return {'start': {'name':'测试起点','time':'2027-01-01T23:00:00','lon':112.9,'lat':26.7},
            'end': {'name':'测试终点','time':'2027-01-02T02:00:00','lon':112.1,'lat':26.1},
            'orders':[
                {'name':'虚构乙服务区','time':'2027-01-02T00:15:00','lon':112.5,'lat':26.38,'energy_added_kwh':24.5,'order_id':'B'},
                {'name':'虚构甲服务区','time':'2027-01-01T23:50:00','lon':112.75,'lat':26.55,'energy_added_kwh':12.3,'order_id':'A'}]}


class NormalizeTests(unittest.TestCase):
    def test_cross_day_sort_and_energy(self):
        raw=fixture();before=copy.deepcopy(raw)
        trip,_=normalize(raw)
        self.assertEqual([r['order_id'] for r in trip['orders']],['A','B'])
        self.assertAlmostEqual(trip['energy_added_kwh'],36.8)
        self.assertEqual(raw,before)

    def test_explicit_overrides(self):
        trip,_=normalize(fixture(),{'start_name':'手动起点','start_time':'2027-01-01T23:30:00',
                                   'start_lon':112.8,'start_lat':26.6,'duration':6})
        self.assertEqual(trip['start']['name'],'手动起点')
        self.assertEqual(trip['start']['time'],'2027-01-01T23:30:00')
        self.assertEqual((trip['start']['lon'],trip['start']['lat']),(112.8,26.6))
        self.assertEqual(trip['duration_seconds'],6)

    def test_dedup_and_conflicting_id(self):
        raw=fixture();raw['orders'].append(copy.deepcopy(raw['orders'][0]))
        trip,_=normalize(raw);self.assertEqual(len(trip['orders']),2)
        self.assertEqual(len(trip['duplicates_removed']),1)
        raw['orders'][-1]['energy_added_kwh']=25
        with self.assertRaisesRegex(ValueError,'冲突'):normalize(raw)

    def test_last_swap_can_be_end(self):
        raw=fixture();del raw['end']
        trip,notes=normalize(raw)
        for key in ('name','time','lon','lat'):self.assertEqual(trip['end'][key],trip['orders'][-1][key])
        self.assertEqual(len(trip['orders']),2)
        self.assertTrue(any('最后一次' in note for note in notes))

    def test_datetime_requires_day_and_clock(self):
        for stamp in ('09:37','2027-01-01',None):
            raw=fixture();raw['start']['time']=stamp
            with self.subTest(stamp=stamp),self.assertRaises(ValueError):normalize(raw)

    def test_zero_energy_allowed_missing_or_negative_not_allowed(self):
        raw=fixture();raw['orders'][0]['energy_added_kwh']=0
        self.assertAlmostEqual(normalize(raw)[0]['energy_added_kwh'],12.3)
        for value in (-1,float('nan'),float('inf'),None):
            raw['orders'][0]['energy_added_kwh']=value
            with self.subTest(value=value),self.assertRaises((ValueError,TypeError)):normalize(raw)

    def test_same_time_requires_same_place(self):
        raw=fixture();raw['orders'][0]['time']=raw['orders'][1]['time']
        with self.assertRaisesRegex(ValueError,'同时间'):normalize(raw)
        raw['orders'][0]['lon']=raw['orders'][1]['lon'];raw['orders'][0]['lat']=raw['orders'][1]['lat']
        self.assertEqual(len(normalize(raw)[0]['orders']),2)

    def test_time_range_and_duration(self):
        with self.assertRaises(ValueError):normalize(fixture(),{'start_time':'2027-01-02T01:00:00'})
        with self.assertRaises(ValueError):normalize(fixture(),{'end_time':'2027-01-01T23:59:00'})
        with self.assertRaises(ValueError):normalize(fixture(),{'duration':1.9})

    def test_example_flag_is_preserved(self):
        raw=fixture();raw['example_only']=True
        trip,notes=normalize(raw)
        self.assertTrue(trip['example_only']);self.assertTrue(any('虚构' in note for note in notes))


class RoadTests(unittest.TestCase):
    def setUp(self):
        trip,_=normalize(fixture());self.points=[trip['start'],*trip['orders'],trip['end']]
        legs=[{'distance':10000,'coordinates':[[a['lon'],a['lat']],[b['lon'],b['lat']]]}
              for a,b in zip(self.points,self.points[1:])]
        self.document={'distance':30000,'legs':legs}

    def test_missing_leg_bad_total_and_wrong_endpoints(self):
        document=copy.deepcopy(self.document);document['legs'].pop()
        with self.assertRaises(ValueError):road_route(document,self.points)
        document=copy.deepcopy(self.document);document['distance']=90000
        with self.assertRaises(ValueError):road_route(document,self.points)
        document=copy.deepcopy(self.document);document['legs'][0]['coordinates'][0]=[1,1]
        with self.assertRaisesRegex(ValueError,'端点'):road_route(document,self.points)

    def test_preparer_uses_renderer_bounds(self):
        route=road_route(self.document,self.points)
        z,tiles,bounds=map_plan(self.points,route,1080,1920,540,6)
        self.assertEqual(bounds,required_basemap_bounds(self.points,route['legs'],6,1080,1920,540))
        self.assertLess(tiles[0]*256,bounds[0]);self.assertGreater((tiles[2]+1)*256,bounds[2])

    def test_missing_end_zero_leg(self):
        raw=fixture();del raw['end'];trip,_=normalize(raw)
        points=[trip['start'],*trip['orders'],trip['end']]
        document=copy.deepcopy(self.document)
        final=[trip['end']['lon'],trip['end']['lat']]
        document['legs'][-1]={'distance':0,'coordinates':[final,final]};document['distance']=20000
        self.assertEqual(road_route(document,points)['legs'][-1]['distance'],0)

    def test_route_sources_and_synthetic_flag_are_preserved(self):
        document=copy.deepcopy(self.document);document['synthetic_test_only']=True
        document['source']={'provider':'synthetic-fixture','reference':'synthetic:test-only'}
        result=road_route(document,self.points)
        self.assertTrue(result['synthetic_test_only'])
        self.assertEqual(result['source']['provider'],'synthetic-fixture')
        self.assertEqual(result['source']['reference'],'synthetic:test-only')


if __name__=='__main__':unittest.main()
