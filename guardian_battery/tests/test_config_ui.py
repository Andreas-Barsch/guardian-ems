import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from config_ui import DEFAULTS, validate

def test_defaults_valid():
    assert validate(dict(DEFAULTS)) == []

def test_module_count_range():
    cfg=dict(DEFAULTS); cfg['module_count']=7
    assert any('Installierte Batteriemodule' in x for x in validate(cfg))

def test_phase_order_validation():
    cfg=dict(DEFAULTS); cfg['cell_diag_high_warning_deviation_mv']=50; cfg['cell_diag_high_critical_deviation_mv']=40
    assert any('High-SOC' in x for x in validate(cfg))

def test_confidence_order_validation():
    cfg=dict(DEFAULTS); cfg['cell_diag_confidence_medium_samples']=700
    assert any('Confidence-Reihenfolge' in x for x in validate(cfg))
