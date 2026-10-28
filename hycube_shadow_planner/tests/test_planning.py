import copy
import unittest
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from hycube_sim.cli import demo,report,markdown_report
from hycube_sim.model import simulate,iso
from hycube_sim.planning import balancing

class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,28,tzinfo=timezone.utc)
        self.p,self.s=demo(self.now)
    def dark(self):
        for f in self.s['forecasts'].values():
            for r in f['intervals']: r['energy_kwh']=0
        self.s['battery']['energy_kwh']=1
        self.p=replace(self.p,standby_kw=0,discharge_efficiency=1,charge_efficiency=1,minimum_reserve_kwh=0,terminal_reserve_kwh=0,safety_margin_kwh=0)
        for r in self.s['loads']['minimum']:r['energy_kwh']=.3
    def test_first_gap_inside_interval(self):
        self.dark();r=simulate(self.s,self.p)
        a=r['scenarios'][0]['autonomy']
        self.assertAlmostEqual(a['hours'],1/.3,places=4)
        self.assertFalse(a['forecast_limited'])
    def test_late_sun_does_not_repair_first_interruption(self):
        self.dark()
        for f in self.s['forecasts']['hycube']['intervals'][10:]:f['energy_kwh']=10
        self.assertAlmostEqual(simulate(self.s,self.p)['scenarios'][0]['autonomy']['hours'],1/.3,places=4)
    def test_reach_beyond_24h(self):
        self.dark();self.s['battery']['energy_kwh']=10
        self.assertAlmostEqual(simulate(self.s,self.p)['scenarios'][0]['autonomy']['hours'],10/.3,places=4)
    def test_forecast_limited_not_infinite(self):
        for r in self.s['forecasts']['hycube']['intervals']:r['energy_kwh']=5
        a=simulate(self.s,self.p)['scenarios'][0]['autonomy']
        self.assertTrue(a['forecast_limited']);self.assertEqual(a['hours'],72)
    def test_power_gap_immediate(self):
        self.dark();self.p=replace(self.p,discharge_limit_kw=.1)
        self.assertEqual(simulate(self.s,self.p)['scenarios'][0]['autonomy']['hours'],0)
    def test_percent_above_technical_floor(self):
        self.dark();self.s['battery'].update(nominal_capacity_kwh=14.4,technical_floor_percent=5)
        r=simulate(self.s,self.p)['reserve_advice']['scenarios'][0]
        self.assertEqual(r['normal_discharge_floor_percent'],55)
    def test_stale_soc_hides_autonomy_keeps_requirement(self):
        self.s['battery']['observed_at']=iso(self.now-timedelta(hours=1))
        r=simulate(self.s,self.p)
        self.assertFalse(r['scenarios']);self.assertTrue(r['reserve_advice']['scenarios'])
    def history(self,delta=20):
        frames=[]
        for i in range(121):
            frames.append({'at':iso(self.now-timedelta(minutes=120-i)), 'pack_soc_percent':100,
                'modules':[{'serial':'A','cells_mv':[3400]*14+[3400+delta], 'temperature_ok':True,'protection_clear':True}]})
        return {'sample_interval_seconds':60,'module_serials':['A'],'frames':frames}
    def test_balancing_success(self):
        r=balancing(self.history(),self.now)
        self.assertEqual(r['status'],'ZIEL_ERREICHT');self.assertTrue(r['full_charge_observed'])
    def test_98_not_full_charge(self):
        h=self.history()
        for f in h['frames']:f['pack_soc_percent']=98
        self.assertEqual(balancing(h,self.now)['status'],'NICHT_BEURTEILBAR')
    def test_data_gap_resets_hold(self):
        h=self.history();del h['frames'][50:60]
        self.assertEqual(balancing(h,self.now)['status'],'NICHT_BEURTEILBAR')
    def test_missing_cell_and_alarm_no_success(self):
        h=self.history()
        for f in h['frames'][-20:]:f['modules'][0]['cells_mv'].pop()
        self.assertEqual(balancing(h,self.now)['status'],'NICHT_BEURTEILBAR')
        h=self.history();h['frames'][30]['modules'][0]['protection_clear']=False
        self.assertEqual(balancing(h,self.now)['status'],'NICHT_BEURTEILBAR')
    def test_module_identity_not_average(self):
        h=self.history();h['module_serials']+=['B']
        for f in h['frames']:
            m=copy.deepcopy(f['modules'][0]);m['serial']='B';m['cells_mv'][-1]+=100;f['modules'].append(m)
        r=balancing(h,self.now)
        self.assertNotEqual(r['status'],'ZIEL_ERREICHT');self.assertTrue(r['modules'][1]['technical_review'])
    def test_reports_show_new_priorities(self):
        r=simulate(self.s,self.p)
        self.assertIn('Aktuelle Notstromreservedauer',report(r));self.assertIn('Notstrom – für mindestens 24 Stunden',markdown_report(r))
        self.assertEqual(balancing(None,self.now)['status'],'NICHT_BEURTEILBAR')
    def test_missing_samples_do_not_count_as_hold_time(self):
        h=self.history();del h['frames'][60]
        self.assertEqual(balancing(h,self.now)['status'],'NICHT_BEURTEILBAR')
    def test_progress_not_success(self):
        h=self.history(80)
        for f in h['frames'][-20:]:f['modules'][0]['cells_mv'][-1]=3450
        r=balancing(h,self.now)
        self.assertEqual(r['modules'][0]['status'],'FORTSCHRITT')
    def test_below_95_resets_window(self):
        h=self.history();h['frames'][60]['pack_soc_percent']=94
        self.assertEqual(balancing(h,self.now)['status'],'NICHT_BEURTEILBAR')
    def test_unreachable_reserve_has_no_percent_command(self):
        self.dark();self.p=replace(self.p,capacity_kwh=2)
        r=simulate(self.s,self.p)['reserve_advice']['scenarios'][0]
        self.assertFalse(r['target_feasible']);self.assertIsNone(r['normal_discharge_floor_percent'])
    def test_full_charge_plan_without_normal_is_unknown(self):
        self.s['loads']['normal']=[]
        self.assertNotIn('balancing_plan',simulate(self.s,self.p))
