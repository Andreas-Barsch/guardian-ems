import tempfile,unittest,json
from pathlib import Path
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from hycube_sim.solar_learning import Learner,integrate_hour,hour_floor
from hycube_sim.model import iso,simulate
from hycube_sim.cli import demo

UTC=timezone.utc
class SolarLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=datetime(2026,11,5,12,2,tzinfo=UTC)
        self.l=Learner(Path(self.tmp.name)/'learn.sqlite',{})
        self.addCleanup(self.l.close)
    def week(self,ratio=.6,missing_hours=0):
        end=hour_floor(self.now-timedelta(seconds=60));start=end-timedelta(days=7)
        forecasts=[];samples=[]
        for h in range(168):
            t=start+timedelta(hours=h)
            forecasts.append(('hycube','x',iso(t),iso(t-timedelta(hours=1)),iso(t-timedelta(minutes=30)),1))
            if h>=missing_hours:
                for minute in range(61):samples.append(('hycube','x',(t+timedelta(minutes=minute)).timestamp(),ratio))
        self.l.db.executemany('INSERT OR REPLACE INTO frozen_forecasts VALUES(?,?,?,?,?,?)',forecasts)
        self.l.db.executemany('INSERT OR REPLACE INTO measured_power VALUES(?,?,?,?)',samples)
        return {'factor':.8,'started_at':iso(self.now-timedelta(days=8)),'status':'INITIAL_LEARNING'}
    def test_learns_energy_ratio_after_seven_days(self):
        s=self.l.calculate('hycube','x',self.week(.6),self.now,.8)
        self.assertEqual(s['status'],'LEARNED');self.assertAlmostEqual(s['factor'],.6)
        self.assertEqual(s['valid_hours'],168)
    def test_initial_seven_days_keep_configured_factor(self):
        s=self.week(.6);s['started_at']=iso(self.now-timedelta(days=6,hours=23))
        s=self.l.calculate('hycube','x',s,self.now,.8)
        self.assertEqual(s['factor'],.8);self.assertEqual(s['status'],'INITIAL_LEARNING')
    def test_bad_coverage_keeps_initial_or_last_learned(self):
        s=self.week(.6,missing_hours=50)
        s=self.l.calculate('hycube','x',s,self.now,.8)
        self.assertEqual(s['factor'],.8);self.assertEqual(s['status'],'INSUFFICIENT_DATA_KEEP_INITIAL')
        s.pop('last_update_hour');s['last_learned_at']=iso(self.now-timedelta(days=1));s['factor']=.65
        s=self.l.calculate('hycube','x',s,self.now,.8)
        self.assertEqual(s['factor'],.65);self.assertEqual(s['status'],'INSUFFICIENT_DATA_KEEP_LEARNED')
    def test_actual_zero_in_daylight_is_valid(self):
        s=self.l.calculate('hycube','x',self.week(0),self.now,.8)
        self.assertEqual(s['status'],'LEARNED');self.assertEqual(s['factor'],0)
    def test_one_recalculation_per_hour_persisted(self):
        s=self.l.calculate('hycube','x',self.week(.6),self.now,.8)
        self.l.db.execute('UPDATE measured_power SET kw=1')
        again=self.l.calculate('hycube','x',s,self.now+timedelta(minutes=15),.8)
        self.assertEqual(again['factor'],s['factor'])
        self.l.save('hycube','x',again);self.l.db.commit()
        self.assertEqual(self.l.state('hycube','x',.8)['last_update_hour'],s['last_update_hour'])
    def test_no_retrospective_forecast_replacement(self):
        h=hour_floor(self.now)+timedelta(hours=1)
        def fc(value,issued):return {'generated_at':iso(issued),'intervals':[{'start':iso(h),'end':iso(h+timedelta(hours=1)),'energy_kwh':value}]}
        self.l.freeze('hycube','x',fc(1,self.now),self.now)
        self.l.freeze('hycube','x',fc(2,h+timedelta(minutes=5)),h+timedelta(minutes=5))
        row=self.l.db.execute('SELECT kwh FROM frozen_forecasts WHERE plant=? AND epoch=? AND hour=?',('hycube','x',iso(h))).fetchone()
        self.assertEqual(row[0],1)
    def test_pre_hour_forecast_can_be_updated(self):
        h=hour_floor(self.now)+timedelta(hours=1)
        for minutes,value in [(0,1),(10,2)]:
            now=self.now+timedelta(minutes=minutes)
            self.l.freeze('hycube','x',{'generated_at':iso(now),'intervals':[{'start':iso(h),'end':iso(h+timedelta(hours=1)),'energy_kwh':value}]},now)
        self.assertEqual(self.l.db.execute('SELECT kwh FROM frozen_forecasts WHERE plant=? AND epoch=? AND hour=?',('hycube','x',iso(h))).fetchone()[0],2)
    def test_missing_source_does_not_start_learning(self):
        r=self.l.run({'forecasts':{}},self.now,.8)
        self.assertEqual(r['secondary']['factor'],.8);self.assertIsNone(r['secondary']['started_at'])
        self.assertEqual(r['secondary']['status'],'WAITING_FOR_VERIFIED_MEASUREMENT')
    def test_sources_are_separate_and_revision_resets(self):
        a=self.l.epoch('hycube',{'site_id':'A'},{'field':'solarPower'})
        b=self.l.epoch('secondary',{'site_id':'B'},{'field':'solarPower'})
        changed=self.l.epoch('hycube',{'site_id':'A'},{'field':'other'})
        self.assertEqual(len({a,b,changed}),3)
    def test_long_gap_is_not_interpolated(self):
        a=self.now;points=[(a,1),(a+timedelta(seconds=30),1),(a+timedelta(hours=1),1)]
        energy,covered=integrate_hour(points,a,a+timedelta(hours=1),60)
        self.assertEqual(covered,30);self.assertAlmostEqual(energy,1/120)
    def test_hour_boundary_interpolation_without_extrapolation(self):
        a=hour_floor(self.now);points=[(a-timedelta(seconds=20),1),(a+timedelta(seconds=20),1)]
        energy,covered=integrate_hour(points,a,a+timedelta(hours=1),60)
        self.assertEqual(covered,20);self.assertAlmostEqual(energy,20/3600)
    def test_models_use_learned_factor_once(self):
        p,s=demo(self.now)
        s['solar_learning']={'hycube':{'factor':.5},'secondary':{'factor':.9}}
        r=simulate(s,p);self.assertEqual(r['effective_solar_factors'],{'hycube':.5,'secondary':.9})
        raw=s['forecasts']['hycube']['intervals']
        expected=[min(x['energy_kwh']*.5,p.island_pv_limit_kw) for x in raw]
        actual=[x['hycube_pv_kw'] for x in r['scenarios'][0]['continuous_outage']['rows']]
        for a,b in zip(actual,expected):self.assertAlmostEqual(a,b,places=5)
        self.assertEqual(r['scenarios'][0]['solar_factor'],.5)
    def test_each_plant_learns_its_own_factor(self):
        state=self.week(.6)
        self.l.db.execute("INSERT INTO frozen_forecasts SELECT 'secondary',epoch,hour,issued,captured,kwh FROM frozen_forecasts WHERE plant='hycube'")
        self.l.db.execute("INSERT INTO measured_power SELECT 'secondary',epoch,t,kw*2 FROM measured_power WHERE plant='hycube'")
        a=self.l.calculate('hycube','x',dict(state),self.now,.8)
        b=self.l.calculate('secondary','x',dict(state),self.now,.8)
        self.assertAlmostEqual(a['factor'],.6);self.assertAlmostEqual(b['factor'],1.2)
    def test_night_zero_forecast_not_a_division_or_learning_point(self):
        state=self.week(.6)
        self.l.db.execute("UPDATE frozen_forecasts SET kwh=0 WHERE hour LIKE '%T00:%'")
        result=self.l.calculate('hycube','x',state,self.now,.8)
        self.assertEqual(result['valid_hours'],161);self.assertAlmostEqual(result['factor'],.6)
    def test_invalid_measurement_splits_coverage(self):
        a=hour_floor(self.now);points=[(a,1),(a+timedelta(seconds=20),None),(a+timedelta(seconds=40),1),(a+timedelta(seconds=60),1)]
        energy,coverage=integrate_hour(points,a,a+timedelta(hours=1),60)
        self.assertEqual(coverage,20)
    def test_observer_can_run_without_a_five_day_deadline(self):
        from unittest.mock import patch
        from dataclasses import asdict
        from hycube_sim import cli
        root=Path(self.tmp.name);p,s=demo(datetime.now(UTC));cfg=root/'config.json';cfg.write_text(json.dumps({'policy':asdict(p)}))
        with patch('sys.argv',['sim','observe','--config',str(cfg),'--output',str(root/'run'),'--duration-hours','0']),patch.object(cli,'collect',return_value=s),patch.object(cli.time,'sleep',side_effect=KeyboardInterrupt):
            cli.main()
        session=json.loads((root/'run/session.json').read_text())
        self.assertIsNone(session['planned_end']);self.assertEqual(session['state'],'INTERRUPTED')
