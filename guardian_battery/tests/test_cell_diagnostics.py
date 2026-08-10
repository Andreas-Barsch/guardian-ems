import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'app'))
from cell_diagnostics import CellDiagnosticStore,CellSample
import types
paho=types.ModuleType("paho"); mqttpkg=types.ModuleType("paho.mqtt"); client=types.ModuleType("paho.mqtt.client")
client.CallbackAPIVersion=types.SimpleNamespace(VERSION2=2); client.Client=object
sys.modules["paho"]=paho; sys.modules["paho.mqtt"]=mqttpkg; sys.modules["paho.mqtt.client"]=client
serial=types.ModuleType("serial"); serial.Serial=object; serial.EIGHTBITS=8; serial.PARITY_NONE="N"; serial.STOPBITS_ONE=1
sys.modules["serial"]=serial
from main import parse_bat,parse_stat
BAT='''0 3329 0 26200 Idle Normal Normal Normal 23% 10837 mAH N
1 3294 0 26200 Idle Normal Normal Normal 23% 10837 mAH N
2 3331 0 26200 Idle Normal Normal Normal 23% 10837 mAH N
3 3331 0 26200 Idle Normal Normal Normal 23% 10837 mAH N
4 3331 0 26200 Idle Normal Normal Normal 23% 10837 mAH N
5 3331 0 26700 Idle Normal Normal Normal 23% 10837 mAH N
6 3331 0 26700 Idle Normal Normal Normal 23% 10837 mAH N
7 3331 0 26700 Idle Normal Normal Normal 23% 10837 mAH N
8 3330 0 26700 Idle Normal Normal Normal 23% 10837 mAH N
9 3331 0 26700 Idle Normal Normal Normal 23% 10837 mAH N
10 3331 0 26300 Idle Normal Normal Normal 23% 10837 mAH N
11 3331 0 26300 Idle Normal Normal Normal 23% 10837 mAH N
12 3331 0 26300 Idle Normal Normal Normal 23% 10837 mAH N
13 3331 0 26300 Idle Normal Normal Normal 23% 10837 mAH N
14 3331 0 26300 Idle Normal Normal Normal 23% 10837 mAH N'''
def opts(): return {'cell_diag_low_soc_percent':30,'cell_diag_high_soc_percent':80,'cell_diag_charge_current_a':.8,'cell_diag_discharge_current_a':.8,'cell_diag_min_phase_samples':5,'cell_diag_confidence_medium_samples':10,'cell_diag_confidence_high_samples':20,'cell_diag_observe_deviation_mv':10,'cell_diag_warning_deviation_mv':20,'cell_diag_critical_deviation_mv':40}
def test_parse():
 r=parse_bat(BAT); assert len(r)==15 and r[1]['voltage_mv']==3294; assert parse_stat('SOH : 96\nCYCLE Times : 427\n')=={'soh_percent':96,'cycles':427}
def test_each_cell(tmp_path):
 s=CellDiagnosticStore(tmp_path/'x.json')
 for n in range(25):
  v=[3330]*15; v[1]=3295; s.add(CellSample(n,1,v,-2,25,[26]*15,[False]*15))
 r=s.analyse(1,opts()); assert len(r['cells'])==15; assert r['cells'][1]['status']=='AUFFÄLLIG'; assert r['cells'][1]['confidence']=='HIGH'; assert r['evidence_worst_cell']==2
