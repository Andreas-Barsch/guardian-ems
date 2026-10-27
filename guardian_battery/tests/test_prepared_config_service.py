import io
import json
from types import SimpleNamespace
import pytest
import config_ui
from config_ui import Handler, DEFAULTS
from prepared_config_service import PreparedConfigService
from application_writer import ApplicationWriter
from raw_registry import RawRegistry
from config_history import ConfigHistory


@pytest.mark.parametrize('scenario',['unchanged','invalid','reject','save_fail','mismatch','success'])
def test_existing_configuration_sequence_uses_only_injected_transport(tmp_path,monkeypatch,scenario):
    options=tmp_path/'options.json';options.write_text(json.dumps(DEFAULTS))
    backend=ApplicationWriter(RawRegistry.create(tmp_path/'registry'),tmp_path/'logs',stream='config',writer_epoch='test')
    history=ConfigHistory(tmp_path/'logs/config.jsonl',append_backend=backend)
    history.record_if_changed(DEFAULTS)
    before=history.path.read_bytes();calls=[];requested={};scheduled=[]
    def api(method,path,payload=None):
        calls.append((method,path,payload))
        if path.endswith('/validate'):return {'data':{'valid':scenario!='reject'}}
        if path.endswith('/options'):
            requested.update(payload['options'])
            return {'result':'error' if scenario=='save_fail' else 'ok'}
        if path.endswith('/info'):return {'data':{'options':{} if scenario=='mismatch' else requested}}
        if path.endswith('/restart'):return {'result':'ok'}
        pytest.fail(path)
    service=PreparedConfigService(options_path=options,config_history=history,supervisor_api=api)
    collector=SimpleNamespace(config_service=service)
    app=SimpleNamespace(collector=collector)
    handler=object.__new__(Handler)
    handler.server=SimpleNamespace(history_composition=object(),prepared_application=app)
    handler.path='/api/config';handler.headers={};handler._ingress_allowed=lambda:True
    responses=[];handler._send=lambda code,body,*a,**kw:responses.append((code,body))
    for name in ('_read_options','_last_record','_api'):
        monkeypatch.setattr(config_ui,name,lambda *a:pytest.fail('legacy configuration dependency'))
    monkeypatch.setattr(backend.registry,'recover',lambda:pytest.fail('GET performed writer recovery'))
    handler.do_GET()
    assert responses[-1][0]==200 and responses[-1][1]['current']==DEFAULTS
    assert responses[-1][1]['config_id']==json.loads(before)['config_id']
    proposed={} if scenario=='unchanged' else {'poll_interval_seconds':20}
    if scenario=='invalid':proposed={'module_count':7}
    body=json.dumps(proposed).encode();handler.rfile=io.BytesIO(body);handler.headers={'Content-Length':str(len(body))}
    monkeypatch.setattr(config_ui.threading,'Thread',lambda *,target,daemon:SimpleNamespace(start=lambda:scheduled.append(target)))
    handler.do_POST()
    assert responses[-1][0]=={'unchanged':200,'invalid':400,'reject':400,'save_fail':502,'mismatch':502,'success':200}[scenario]
    assert len(calls)=={'unchanged':0,'invalid':0,'reject':1,'save_fail':2,'mismatch':3,'success':3}[scenario]
    assert bool(scheduled)==(scenario=='success')
    if scheduled:
        monkeypatch.setattr(config_ui.time,'sleep',lambda seconds:None)
        scheduled[0]()
        assert calls[-1][1]=='/addons/self/restart'
    assert history.path.read_bytes()==before  # New effective epoch is recorded at startup, not on save.
