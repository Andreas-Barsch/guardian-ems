import unittest
from hycube_sim.cli import report,markdown_report,display_values
class MinimalReportTests(unittest.TestCase):
    def result(self):
        return {'battery':{'nominal_capacity_kwh':14.4,'technical_floor_percent':5},'scenarios':[{'autonomy':{'hours':60.25,'forecast_limited':False}}],
            'reserve_advice':{'scenarios':[{'required_now_kwh':5.935813,'target_feasible':True}]},'reserve_settings':{'passive_percent':3}}
    def test_only_two_metrics_and_no_hidden_details(self):
        r=self.result();body=report(r)
        self.assertEqual(body.count('<section>'),2)
        self.assertNotIn('<details>',body);self.assertNotIn('Zellabgleich',body);self.assertNotIn('Energieplan',body)
        self.assertEqual(markdown_report(r).count('## '),2)
    def test_emergency_share_does_not_include_fixed_protection_or_passive(self):
        values=display_values(self.result())
        self.assertEqual(values[0][1],'60,2 Stunden');self.assertEqual(values[1][1],'41,3 %')
    def test_bad_freshness_does_not_show_a_supply_duration(self):
        r=self.result();r['issues']=['Batterieenergie veraltet']
        self.assertEqual(display_values(r)[0][1],'Nicht berechenbar')
    def test_forecast_end_is_a_lower_bound(self):
        r=self.result();r['scenarios'][0]['autonomy']={'hours':72,'forecast_limited':True}
        self.assertEqual(display_values(r)[0][1],'Mindestens 72,0 Stunden')
    def test_unachievable_reserve_never_shows_setting(self):
        r=self.result();r['reserve_advice']['scenarios'][0]['target_feasible']=False
        self.assertEqual(display_values(r)[1][1],'Nicht berechenbar')
