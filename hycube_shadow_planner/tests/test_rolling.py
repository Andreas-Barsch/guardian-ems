import copy
import unittest
from datetime import datetime,timedelta,timezone
from hycube_sim.model import Policy,iso
from hycube_sim.rolling import guarded_requirement,Triggers
from hycube_sim.cli import display_values
from hycube_sim.ha_display import payloads

class RollingTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,28,12,tzinfo=timezone.utc)
        self.policy=Policy.parse({'capacity_kwh':13.68,'standby_kw':0,'discharge_efficiency':1,'charge_efficiency':1})
    def snapshot(self,solar):
        return {'rolling_minimum_kw':.3,'forecasts':{'hycube':{'intervals':[{'start':iso(self.now+timedelta(hours=i/2)),'end':iso(self.now+timedelta(hours=(i+1)/2)),'energy_kwh':solar(i)*.5} for i in range(150)]}}}
    def test_no_sun_is_24h_not_an_extra_hour_of_consumption(self):
        r=guarded_requirement(self.snapshot(lambda i:0),self.policy,self.now,3660)
        self.assertAlmostEqual(r['required_now_kwh'],7.2)
        self.assertTrue(r['target_feasible'])
    def test_reserve_protects_rise_before_next_calculation(self):
        s=self.snapshot(lambda i:1 if i<2 else 0)
        now=guarded_requirement(s,self.policy,self.now,0)['required_now_kwh']
        guard=guarded_requirement(s,self.policy,self.now,3600)['required_now_kwh']
        self.assertGreater(guard,now)
        self.assertAlmostEqual(guard,7.2)
    def test_half_hour_boundaries_not_hour_averages(self):
        s=self.snapshot(lambda i:0 if i==0 else 1)
        r=guarded_requirement(s,self.policy,self.now,3600)
        self.assertAlmostEqual(r['required_now_kwh'],.15)
    def test_missing_forecast_is_not_zero(self):
        s=self.snapshot(lambda i:0);s['forecasts']['hycube']['intervals'].pop(2)
        with self.assertRaises(ValueError):guarded_requirement(s,self.policy,self.now,3660)
    def obs(self,house=1000,ab1=200,ab2=400):
        return {k:{'value':v} for k,v in [('house_power',house),('hycube_pv_power',ab1),('external_power',-ab2)]}
    def test_deficit_debounce_and_both_plants(self):
        t=Triggers();cfg={};planned=self.now
        self.assertNotIn('DEFIZIT_BEGINN',t.check(self.obs(),10,5,cfg,self.now,planned))
        self.assertNotIn('DEFIZIT_BEGINN',t.check(self.obs(),10,5,cfg,self.now+timedelta(seconds=60),planned))
        self.assertIn('DEFIZIT_BEGINN',t.check(self.obs(),10,5,cfg,self.now+timedelta(seconds=120),planned))
        t=Triggers();t.check(self.obs(house=500),10,5,cfg,self.now,planned)
        self.assertFalse(t.deficit)
    def test_hourly_fallback_and_reserve_crossing_latched(self):
        t=Triggers();r=t.check(self.obs(),4,5,{},self.now,self.now)
        self.assertIn('RESERVEGRENZE_ERREICHT',r)
        self.assertNotIn('RESERVEGRENZE_ERREICHT',t.check(self.obs(),4,5,{},self.now,self.now))
        self.assertIn('STUENDLICHE_PLANUNG',t.check(self.obs(),4,5,{},self.now+timedelta(hours=1),self.now))
    def test_new_forecast_and_config_replan(self):
        t=Triggers();t.check(self.obs(),10,5,{},self.now,self.now,'a')
        r=t.check(self.obs(),10,5,{'rolling':{}},self.now,self.now,'b')
        self.assertIn('NEUE_SOLARPROGNOSE',r);self.assertIn('KONFIGURATION_GEAENDERT',r)
    def test_display_uses_guard_not_instantaneous_floor(self):
        r={'battery':{'nominal_capacity_kwh':14.4,'technical_floor_percent':5},'reserve_settings':{'passive_percent':3},
           'reserve_advice':{'scenarios':[{'required_now_kwh':1,'target_feasible':True}]},
           'rolling_reserve':{'required_now_kwh':7.2,'target_feasible':True},'scenarios':[{'autonomy':{'hours':60}}]}
        self.assertEqual(display_values(r)[1][1],'50,0 %')
        self.assertEqual(payloads(r)[1]['state'],'50.0')
        r['issues']=['Veraltet']
        self.assertTrue(all(p['state']=='unavailable' for p in payloads(r)))
    def test_ab2_never_counts_in_outage_and_factor_once(self):
        s=self.snapshot(lambda i:.25)
        s['solar_learning']={'hycube':{'factor':.8},'secondary':{'factor':2}}
        s['forecasts']['secondary']={'intervals':s['forecasts']['hycube']['intervals']}
        r=guarded_requirement(s,self.policy,self.now,3600)
        self.assertAlmostEqual(r['required_now_kwh'],2.4)
    def test_recovery_triggers_full_recalculation(self):
        t=Triggers();t.invalid=True
        self.assertIn('MESSWERTE_WIEDER_VERFUEGBAR',t.check(self.obs(),10,5,{},self.now,self.now))
    def test_display_publisher_can_only_write_its_two_states(self):
        from hycube_sim.ha_display import publish,ENTITIES
        from unittest.mock import patch
        class Response:
            status=201
            def __enter__(self):return self
            def __exit__(self,*args):pass
        class Opener:
            def __init__(self):self.requests=[]
            def open(self,r,timeout):self.requests.append(r);return Response()
        o=Opener()
        with patch.dict('os.environ',{'TEST_HA_TOKEN':'private'}):
            publish({'issues':['fehlend']},{'ha_display':{'enabled':True},'sources':{'home_assistant':{'base_url':'http://supervisor/core','token_env':'TEST_HA_TOKEN'}}},o)
        self.assertEqual([r.full_url for r in o.requests],['http://supervisor/core/api/states/'+e for e in ENTITIES])
        self.assertTrue(all(r.method=='POST' and '/services/' not in r.full_url for r in o.requests))

    def test_calculated_and_effective_factors_are_separate(self):
        r={'solar_learning':{'hycube':{'raw_forecast_kwh':2,'actual_kwh':1,'valid_hours':1,'status':'INITIAL_LEARNING'},
                            'secondary':{'raw_forecast_kwh':0,'actual_kwh':0}},
           'effective_solar_factors':{'hycube':.8,'secondary':.8}}
        f=payloads(r)[1]['attributes']['solar_factors']
        self.assertEqual(f['AB1']['calculated_percent'],50)
        self.assertEqual(f['AB1']['effective_percent'],80)
        self.assertTrue(f['AB1']['provisional'])
        self.assertIsNone(f['AB2']['calculated_percent'])
        r['solar_learning']['hycube'].update(raw_forecast_kwh=2,actual_kwh=0)
        self.assertEqual(payloads(r)[1]['attributes']['solar_factors']['AB1']['calculated_percent'],0)
