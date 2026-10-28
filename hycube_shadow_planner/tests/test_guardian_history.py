import json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from hycube_sim.guardian_history import current,read_history
from hycube_sim.model import iso,InputError
from hycube_sim.planning import balancing

class SharedGuardianTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'guardian';self.root.mkdir();self.now=datetime(2026,10,28,12,tzinfo=timezone.utc)
        self.spec={'root':str(self.root),'cache':str(Path(self.tmp.name)/'own/cache.sqlite'),'module_serials':['A'],'time_budget_seconds':5}
        for d in ['hycube_history','hycube_history_projection','cell_history']:(self.root/d).mkdir()
    def write(self,d,rows):
        f=self.root/d/'2026-10-28.jsonl';f.write_text(''.join(json.dumps(r)+'\n' for r in rows));return f
    def test_fresh_unchanged_soc_and_signed_flows(self):
        self.write('hycube_history',[{'received_at':iso(self.now),'BatteryCapacity':60,'GridPower':-50,'HomePower':300}])
        soc,flows=current(self.spec,self.now)
        self.assertEqual(soc['observed_at'],iso(self.now));self.assertEqual(flows['grid_power']['value'],-50)
    def test_stale_raw_soc_rejected(self):
        self.write('hycube_history',[{'received_at':iso(self.now-timedelta(hours=1)),'BatteryCapacity':60}])
        with self.assertRaises(InputError):current(self.spec,self.now)
    def test_cache_only_writes_own_directory_incremental(self):
        packs=[];cells=[]
        for i in range(121):
            t=self.now-timedelta(minutes=120-i)
            packs.append({'received_at':iso(t),'battery_capacity':100})
            cells.append({'timestamp':t.timestamp(),'module_serial':'A','voltages_mv':[3400]*14+[3420],'temperatures_c':[25]*15,'soc_percent':100})
        a=self.write('hycube_history_projection',packs);b=self.write('cell_history',cells)
        original=[a.read_bytes(),b.read_bytes()]
        h=read_history(self.spec,self.now)
        self.assertEqual(h['acquisition']['pending_files'],0);self.assertEqual(len(h['frames']),121)
        self.assertEqual([a.read_bytes(),b.read_bytes()],original)
        self.assertEqual(read_history(self.spec,self.now)['acquisition']['bytes_read'],0)
        result=balancing(h,self.now)
        self.assertEqual(result['status'],'NICHT_BEURTEILBAR') # no invented alarm/temperature clearance
        self.assertTrue(result['full_charge_observed'])
    def test_cache_cannot_be_in_guardian(self):
        self.spec['cache']=str(self.root/'bad.sqlite')
        with self.assertRaises(InputError):read_history(self.spec,self.now)
    def test_missing_history_never_means_no_full_charge(self):
        h=read_history(self.spec,self.now)
        self.assertFalse(h['maintenance_evidence']['more_than_14_days_without_full_charge_proven'])
        self.assertEqual(balancing(h,self.now)['full_charge_need']['status'],'NICHT_BEURTEILBAR')
    def test_backfill_budget_and_resume(self):
        self.write('hycube_history_projection',[{'received_at':iso(self.now-timedelta(seconds=i*20)),'battery_capacity':60} for i in range(100)])
        self.spec['byte_budget']=100
        a=read_history(self.spec,self.now);self.assertGreater(a['acquisition']['pending_files'],0)
        self.spec['byte_budget']=100000
        b=read_history(self.spec,self.now);self.assertEqual(b['acquisition']['pending_files'],0)
        self.assertEqual(b['acquisition']['pack_points'],100)
