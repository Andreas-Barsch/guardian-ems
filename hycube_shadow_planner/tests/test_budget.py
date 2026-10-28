import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from hycube_sim.model import Policy,iso,simulate
from hycube_sim.budget import plan_budget
from hycube_sim.cli import demo

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,28,tzinfo=timezone.utc)
        self.p=Policy.parse({'capacity_kwh':12,'standby_kw':0,'charge_efficiency':1,'discharge_efficiency':1})
        self.edges=[self.now+timedelta(hours=i) for i in range(73)]
        self.battery={'nominal_capacity_kwh':14.4,'technical_floor_percent':5}
    def plan(self,pv,energy=10,need='NICHT_BEURTEILBAR'):
        return plan_budget(pv,[.3]*72,energy,self.p,self.edges,self.battery,{'full_charge_need':{'status':need}})
    def test_zero_solar_release_maintains_24h_stock(self):
        r=self.plan([0]*72)
        self.assertAlmostEqual(r['now']['conditional_floor_dc_kwh'],7.2)
        self.assertAlmostEqual(r['now']['maximum_release_now_dc_kwh'],2.8)
        self.assertFalse(r['global_grid_optimum_claimed']);self.assertFalse(r['normal_load_simulation'])
    def test_short_stock_manual_charge_lower_bound(self):
        r=self.plan([0]*72,energy=5)
        self.assertAlmostEqual(r['manual_reserve_charge']['lower_bound_dc_kwh'],2.2)
        self.assertEqual(r['manual_reserve_charge']['first_deadline'],iso(self.now))
        self.assertEqual(r['now']['maximum_release_now_dc_kwh'],0)
    def test_budget_can_meet_every_next_reserve_if_pv_allocated(self):
        pv=[2 if 9<=i%24<17 else 0 for i in range(72)]
        r=self.plan(pv);rows=r['target_curve']
        self.assertEqual(len(rows),48)
        for a,b in zip(rows,rows[1:]):
            self.assertGreaterEqual(a['conditional_floor_dc_kwh']+1e-6,a['reserve_24h_dc_kwh'])
            self.assertLessEqual(b['conditional_floor_dc_kwh']-a['conditional_floor_dc_kwh'],a['maximum_forecast_charge_dc_kwh']+1e-6)
        self.assertTrue(any(p['required_stored_solar_dc_kwh']>0 for p in r['phases']))
    def test_late_pv_cannot_cover_earlier_reserve_deadline(self):
        r=self.plan([0]*30+[5]*42,energy=1)
        self.assertGreater(r['manual_reserve_charge']['lower_bound_dc_kwh'],0)
        self.assertEqual(r['manual_reserve_charge']['first_deadline'],iso(self.now))
    def test_unknown_balancing_never_requests_full_charge(self):
        r=self.plan([0]*72)
        self.assertEqual(r['balancing_charge']['status'],'KEIN_LADEAUFTRAG')
        self.assertNotIn('grid_charge_lower_bound_dc_kwh',r['balancing_charge'])
    def test_historical_need_uses_solar_before_manual_topup(self):
        r=self.plan([0]*72,energy=10,need='VOLLLADUNG_PRUEFEN')
        self.assertEqual(r['balancing_charge']['status'],'MANUELLE_ERGAENZUNG_PRUEFEN')
        self.assertEqual(r['balancing_charge']['grid_charge_lower_bound_dc_kwh'],2)
        r=self.plan([2]*72,energy=10,need='VOLLLADUNG_PRUEFEN')
        self.assertEqual(r['balancing_charge']['status'],'SOLARE_VOLLLADUNG_BEDINGT_MOEGLICH')
    def test_impossible_target_has_no_discharge_floor_suggestion(self):
        self.p=replace(self.p,capacity_kwh=2)
        r=self.plan([0]*72,energy=2)
        self.assertEqual(r['status'],'24H_ZIEL_NICHT_DURCHGAENGIG_ERREICHBAR')
        self.assertIsNone(r['now']['conditional_floor_soc_percent'])
    def test_missing_soc_never_emits_energy_release_plan(self):
        p,s=demo(self.now);s['battery']['observed_at']=iso(self.now-timedelta(hours=1))
        self.assertNotIn('energy_plan',simulate(s,p))

    def test_maintenance_energy_not_released_for_house_twice(self):
        r=self.plan([0]*72,energy=10,need='VOLLLADUNG_PRUEFEN')
        self.assertEqual(r['now']['maximum_release_now_dc_kwh'],0)
        self.assertEqual(r['now']['additional_charge_needed_dc_kwh'],2)
