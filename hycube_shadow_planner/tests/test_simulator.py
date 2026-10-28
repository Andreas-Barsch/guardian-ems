import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from hycube_sim.cli import demo, archive, verify
from hycube_sim.model import Policy, InputError, simulate, required_energy, forward, resample, iso
from hycube_sim.sources import ReadClient, SourceError, NoRedirect, ha_forecast

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,28,tzinfo=timezone.utc)
        self.p,self.s=demo(self.now)
    def test_demo_and_complete_start_window(self):
        r=simulate(self.s,self.p)
        self.assertEqual(r['status'],'DEMO')
        self.assertEqual(len(r['scenarios'][0]['reserve_corridor']),25)
        self.assertEqual(len(r['scenarios'][0]['outage_now']['rows']),24)
    def test_secondary_never_helps_outage(self):
        a=simulate(self.s,self.p)['scenarios'][0]['outage_now']
        for row in self.s['forecasts']['secondary']['intervals']: row['energy_kwh']=1000
        b=simulate(self.s,self.p)['scenarios'][0]['outage_now']
        self.assertEqual(a,b)
        self.assertTrue(all(x['grid_kw']==0 and x['secondary_pv_kw']==0 for x in b['rows']))
    def test_missing_not_zero(self):
        self.s['forecasts']['hycube']['intervals'].pop(12)
        r=simulate(self.s,self.p)
        self.assertEqual(r['status'],'DATENLUECKE');self.assertEqual(r['scenarios'],[])
    def test_stale_forecast(self):
        self.s['forecasts']['hycube']['generated_at']=iso(self.now-timedelta(hours=7))
        self.assertEqual(simulate(self.s,self.p)['status'],'DATENLUECKE')
    def test_collection_failure_propagates(self):
        self.s['collection_issues']=['Messquelle ausgefallen']
        self.assertEqual(simulate(self.s,self.p)['status'],'DATENLUECKE')
    def test_multi_night_chronology(self):
        p=Policy.parse({'horizon_hours':4,'outage_hours':4,'start_window_hours':0,'capacity_kwh':5,
            'standby_kw':0,'charge_efficiency':1,'discharge_efficiency':1,'terminal_reserve_kwh':1})
        # A sunny last hour cannot cover the first dark hours retroactively.
        needed,_,_=required_energy([0,0,0,3],[1,1,1,1],p,1)
        self.assertEqual(needed,3)
    def test_power_failure_with_full_battery(self):
        p=Policy.parse({'capacity_kwh':100,'discharge_limit_kw':.1})
        edges=[self.now,self.now+timedelta(hours=1)]
        r=forward([0],[1],100,p,1,edges)
        self.assertGreater(r['unserved_ac_kwh'],.8)
    def test_energy_conserving_resampling(self):
        rows=[{'start':iso(self.now),'end':iso(self.now+timedelta(hours=2)),'energy_kwh':4}]
        self.assertEqual(resample(rows,[self.now,self.now+timedelta(hours=1),self.now+timedelta(hours=2)],'x'),[2,2])
    def test_short_horizon_and_nan_rejected(self):
        for config in ({'horizon_hours':47},{'capacity_kwh':float('nan')},{'charge_efficiency':0}):
            with self.assertRaises(InputError): Policy.parse(config)
    def test_future_telemetry_rejected(self):
        self.s['battery']['observed_at']=iso(self.now+timedelta(hours=1))
        self.assertEqual(simulate(self.s,self.p)['status'],'DATENLUECKE')
    def test_archive_and_verify(self):
        with tempfile.TemporaryDirectory() as d:
            r=simulate(self.s,self.p)
            folder=archive(d,self.s,r)
            self.assertTrue((Path(d)/'index.html').exists())
            self.assertTrue((Path(d)/'index.md').exists())
            self.assertIn('Aktuelle Notstromreservedauer',(Path(d)/'index.md').read_text())
            csv=Path(d)/'actual.csv'
            csv.write_text('start,end,hycube_kwh,secondary_kwh\n2026-10-28T00:00:00+00:00,2026-10-28T01:00:00+00:00,0,0\n')
            result=verify(folder,csv,Path(d)/'verification.json')
            # At least exercise persistent roundtrip; future intervals are excluded by evaluator.
            self.assertIn('hycube',result['comparisons'])

class ReadOnlyTests(unittest.TestCase):
    def test_allowlist_blocks_all_control_routes(self):
        for role in ('guardian','ha'):
            client=ReadClient('http://example.invalid',role)
            for route in ('/api/services/switch/turn_on','/smartCharging','/api/states/switch.wallbox','/api/states/sensor.x/../switch.x'):
                with self.assertRaises(SourceError): client.read(route)
    def test_get_and_auth_not_in_query(self):
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def read(self,n): return b'{}'
        class Fake:
            def open(self,request,**kwargs):
                self.request=request
                return Response()
        fake=Fake();ReadClient('http://example.invalid','ha','secret',opener=fake).read('/api/states/sensor.test')
        self.assertEqual(fake.request.method,'GET');self.assertNotIn('secret',fake.request.full_url)
    def test_redirect_rejected(self):
        with self.assertRaises(SourceError): NoRedirect().redirect_request(None,None,302,'',{},'http://other/')
    def test_credentials_in_url_rejected(self):
        with self.assertRaises(InputError): ReadClient('http://user:password@host','ha')

if __name__=='__main__': unittest.main()

class EvaluationTests(unittest.TestCase):
    def test_no_bridging_missing_telemetry(self):
        from hycube_sim.evaluation import observations_from_history
        with tempfile.TemporaryDirectory() as d:
            base=datetime(2026,10,20,tzinfo=timezone.utc)
            for i,minutes in enumerate((0,15,90)):
                folder=Path(d)/str(i);folder.mkdir()
                t=iso(base+timedelta(minutes=minutes))
                (folder/'input.json').write_text(json.dumps({'kind':'live','captured_at':t,'observations':{
                    'hycube_pv_power':{'value':2,'unit':'kW','observed_at':t,'source_entity':'sensor.pv'}}}))
            output=Path(d)/'actual.csv'
            result=observations_from_history(d,output)
            self.assertEqual(result['interval_count'],1)
            self.assertIn('0.5',output.read_text())

class ValidationTests(unittest.TestCase):
    def test_no_lookahead_in_comparison(self):
        p,s=demo(datetime(2026,10,20,tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as d:
            folder=archive(d,s,simulate(s,p))
            csv=Path(d)/'actual.csv'
            csv.write_text('start,end,hycube_kwh,secondary_kwh\n'
                '2026-10-19T23:00:00Z,2026-10-20T00:00:00Z,1,1\n'
                '2026-10-20T00:00:00Z,2026-10-20T01:00:00Z,0,0\n'
                '2099-01-01T00:00:00Z,2099-01-01T01:00:00Z,0,0\n')
            answer=verify(folder,csv,Path(d)/'out.json')
            self.assertEqual(answer['comparisons']['hycube']['count'],1)
            self.assertEqual(answer['comparisons']['hycube']['mae_kwh'],0)
    def test_intermediate_capacity_infeasible(self):
        p=Policy.parse({'capacity_kwh':1,'horizon_hours':4,'outage_hours':4,'start_window_hours':0,
                        'standby_kw':0,'charge_efficiency':1,'discharge_efficiency':1})
        needed,maximum,_=required_energy([4,0,0,0],[0,1,1,1],p,1)
        self.assertGreater(maximum,p.capacity_kwh)
    def test_single_configurable_solar_factor(self):
        from dataclasses import replace
        p,s=demo(datetime(2026,10,20,tzinfo=timezone.utc))
        result=simulate(s,p)
        self.assertEqual(len(result['scenarios']),1)
        self.assertEqual(len(result['reserve_requirements_only']),1)
        self.assertEqual(result['scenarios'][0]['solar_factor'],.8)
        low=simulate(s,replace(p,solar_correction_factor=.4))['scenarios'][0]
        self.assertGreaterEqual(low['required_now_kwh'],result['scenarios'][0]['required_now_kwh'])
        self.assertEqual(low['solar_factor'],.4)
        with self.assertRaises(InputError):Policy.parse({'solar_correction_factor':1.1})
        with self.assertRaises(InputError):Policy.parse({'stress_solar_factor':.4})

class UserParametersTests(unittest.TestCase):
    def test_confirmed_300_watts_24_hours_budget(self):
        from hycube_sim.sources import collect
        now=datetime(2026,10,28,tzinfo=timezone.utc)
        policy=Policy.parse({'outage_hours':24,'profile_confirmed':True})
        snapshot=collect({'load_profiles':{'minimum_kw':.3}},policy,now)
        result=simulate(snapshot,policy,now)
        self.assertEqual(result['minimum_load_budget']['house_ac_kwh'],7.2)
        self.assertEqual(result['status'],'DATENLUECKE')
    def test_both_parameters_remain_configurable(self):
        from hycube_sim.sources import collect
        now=datetime(2026,10,28,tzinfo=timezone.utc)
        policy=Policy.parse({'outage_hours':12})
        result=simulate(collect({'load_profiles':{'minimum_kw':.5}},policy,now),policy,now)
        self.assertEqual(result['minimum_load_budget']['house_ac_kwh'],6)

class PriorityTests(unittest.TestCase):
    def test_guardian_live_priority_not_retried(self):
        from urllib.error import HTTPError
        class Busy:
            calls=0
            def open(self, request, **kwargs):
                self.calls+=1
                raise HTTPError(request.full_url,503,'Busy',{},None)
        busy=Busy()
        with self.assertRaises(SourceError):
            ReadClient('http://example.invalid','guardian',opener=busy).read('/api/research/status')
        self.assertEqual(busy.calls,1)

class SolcastAdapterTests(unittest.TestCase):
    def fixture(self):
        from hycube_sim.sources import ha_solcast_forecast
        return ha_solcast_forecast, [{'entity_id':'sensor.forecast_today','attributes':{
            'detailedForecast_site_1':[{'period_start':'2026-10-28T12:00:00+01:00',
                'pv_estimate':2,'pv_estimate10':1,'pv_estimate90':3}]}}], {'attributes':{'resource_id':'site-1'}}, {'state':'2026-10-28T10:00:00Z'}, {'site_id':'site-1'}
    def test_half_hour_power_to_energy_once(self):
        adapter,days,metadata,issued,spec=self.fixture()
        result=adapter(days,metadata,issued,spec)
        self.assertEqual(result['intervals'][0]['energy_kwh'],1)
        self.assertEqual(result['intervals_p10'][0]['energy_kwh'],.5)
        self.assertEqual(result['generated_at'],'2026-10-28T10:00:00+00:00')
    def test_wrong_site_mapping_rejected(self):
        adapter,days,metadata,issued,spec=self.fixture();metadata['attributes']['resource_id']='site-2'
        with self.assertRaises(InputError): adapter(days,metadata,issued,spec)
    def test_missing_per_site_does_not_use_combined(self):
        adapter,days,metadata,issued,spec=self.fixture()
        days[0]['attributes']['detailedForecast']=days[0]['attributes'].pop('detailedForecast_site_1')
        with self.assertRaises(InputError): adapter(days,metadata,issued,spec)
    def test_missing_capacity_still_has_no_supply_confirmation(self):
        p,s=demo(datetime(2026,10,28,tzinfo=timezone.utc))
        p=Policy.parse({'capacity_kwh':None})
        r=simulate(s,p)
        self.assertEqual(r['status'],'DATENLUECKE')
        self.assertFalse(r['reserve_requirements_only'][0]['supply_confirmed'])
        self.assertEqual(r['scenarios'],[])

class SocAndEmergencyScopeTests(unittest.TestCase):
    def observations(self):
        return {'hycube_soc':{'value':57,'unit':'%','observed_at':'2026-10-28T00:00:00Z'},
                'outage_protection_threshold':{'value':5,'unit':'%','observed_at':'2026-10-28T00:00:00Z'}}
    def mapping(self):
        return {'nominal_capacity_kwh':14.4,'technical_floor_percent':5}
    def test_soc_estimate_subtracts_floor_once(self):
        from hycube_sim.sources import estimate_soc_energy
        b=estimate_soc_energy(self.observations(),self.mapping())
        self.assertAlmostEqual(b['energy_kwh'],7.488)
        self.assertAlmostEqual(b['estimated_capacity_kwh'],13.68)
        self.assertTrue(b['estimate'])
    def test_soc_below_floor_is_zero_not_negative(self):
        from hycube_sim.sources import estimate_soc_energy
        observed=self.observations();observed['hycube_soc']['value']=3
        self.assertEqual(estimate_soc_energy(observed,self.mapping())['energy_kwh'],0)
    def test_changed_protection_floor_needs_consistent_model(self):
        from hycube_sim.sources import estimate_soc_energy
        observed=self.observations();observed['outage_protection_threshold']['value']=10
        with self.assertRaises(InputError): estimate_soc_energy(observed,self.mapping())
    def test_no_normal_profile_does_not_block_emergency_model(self):
        from hycube_sim.sources import estimate_soc_energy
        now=datetime(2026,10,28,tzinfo=timezone.utc)
        _,snapshot=demo(now)
        snapshot['kind']='live'
        snapshot['loads']['normal']=[]
        snapshot['battery']=estimate_soc_energy(self.observations(),self.mapping())
        policy=Policy.parse({'capacity_kwh':13.68,'profile_confirmed':True})
        result=simulate(snapshot,policy,now)
        self.assertEqual(result['issues'],[])
        self.assertEqual(result['status'],'VORLAEUFIG')
        self.assertEqual(len(result['scenarios']),1)
        scenario=result['scenarios'][0]
        self.assertEqual(scenario['normal_baseline'],[])
        self.assertIsNone(scenario['maximum_future_gap_kwh'])
        self.assertIsNone(scenario['reserve_corridor'][1]['reserve_gap_kwh'])
        self.assertEqual(len(scenario['outage_now']['rows']),24)
    def test_old_soc_does_not_become_fresh_by_conversion(self):
        from hycube_sim.sources import estimate_soc_energy
        now=datetime(2026,10,28,tzinfo=timezone.utc)
        _,snapshot=demo(now);snapshot['kind']='live'
        observed=self.observations();observed['hycube_soc']['observed_at']='2026-10-27T22:00:00Z'
        snapshot['battery']=estimate_soc_energy(observed,self.mapping())
        result=simulate(snapshot,Policy.parse({'capacity_kwh':13.68}),now)
        self.assertEqual(result['status'],'DATENLUECKE')
