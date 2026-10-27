"""Opt-in synthetic local capacity profile; never an HA acceptance test.

Run with the test guard and app/tests on PYTHONPATH. No network/device access.
CatalogOnly historical inputs deliberately do NOT prove the RawRegistry chain;
test_prepared_candidate separately covers that chain. --science uses original
Daily methods and an actual prepared collector with synthetic console replies.
"""
from pathlib import Path
from datetime import timedelta
import argparse,gc,json,time,resource,sys,tempfile,threading,faulthandler,subprocess
from source_catalog import SourceCatalog
from daily_diagnostics import guardian_day,DailyDiagnosticSources,run_daily_diagnostic
from daily_indexed_inputs import DailyIndexedInputs
from test_daily_diagnostics import cell
from test_source_catalog import registration,index_all


def rss():
    value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform=='darwin' else value*1024


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval',type=int,choices=(10,60),required=True)
    parser.add_argument('--existing',type=Path)
    parser.add_argument('--science',action='store_true')
    args=parser.parse_args()
    root=args.existing or Path(tempfile.mkdtemp(prefix='guardian-local-capacity-'))
    day=guardian_day('2026-08-31')
    first=guardian_day(day.start.date()-timedelta(days=13)).start.timestamp()
    count=14*86400//args.interval*6
    catalog=SourceCatalog(root/'catalog')
    if args.existing is None:
        for d in range(14):
            path=root/(str(d)+'.jsonl')
            with path.open('wb') as output:
                for stamp in range(int(first+d*86400),int(first+(d+1)*86400),args.interval):
                    for module in range(1,7):
                        output.write((json.dumps(dict(cell(stamp,serial='SYNTHETIC-'+str(module),module=module),schema_version=1),separators=(',',':'))+'\n').encode())
            catalog.register(**registration(path,source=str(d)));index_all(catalog,str(d))
            print(json.dumps({'prepared_day':d+1}),flush=True)
    backend=DailyIndexedInputs(dict(cell=catalog,rs485=None,position_history=None,config_history=None,maintenance_history=None))
    start=time.monotonic();cpu=time.process_time()
    slices,records=backend.read(day)
    assert len(records)==count and backend.metrics['cell']['decodes']==count
    result=dict(kind='synthetic_local_CatalogOnly',interval_seconds=args.interval,records=count,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=Path(__file__).resolve().parents[2]).decode().strip(),
        raw_file_bytes=sum((root/(str(d)+'.jsonl')).stat().st_size for d in range(14)),
        read_wall_seconds=time.monotonic()-start,read_cpu_seconds=time.process_time()-cpu,
        read_peak_rss_bytes=rss(),metrics=backend.metrics,physical_io_bytes=None,ha_live_timing=None,fixture=str(root))
    print('READ_RESULT '+json.dumps(result,sort_keys=True),flush=True)
    del slices,records;gc.collect()
    start=time.monotonic();proof=backend.confirmed_guard(day)
    assert proof['sources']['cell']['selected_records']==count
    result['guard_wall_seconds']=time.monotonic()-start
    if args.science:
        from test_prepared_candidate import candidate_fixture
        fixture=Path(tempfile.mkdtemp(prefix='guardian-local-profile-collector-')).resolve()
        create,layout,clock,inspected=candidate_fixture(fixture);inspected.close();app=create()
        stop=threading.Event();observations=[];errors=[]
        def live():
            next_due=time.monotonic()
            while not stop.is_set():
                start=time.monotonic()
                try:
                    clock[0]+=10
                    outcome=app.collector.cycle()
                    observations.append(dict(started_monotonic=start,lateness_seconds=max(0,start-next_due),duration_seconds=time.monotonic()-start,modules=outcome['modules']))
                except BaseException as exc:errors.append(repr(exc));stop.set();return
                next_due+=10
                stop.wait(max(0,next_due-time.monotonic()))
        app.start();worker=threading.Thread(target=live);worker.start()
        start=time.monotonic();cpu=time.process_time()
        trace_path=fixture/'science-stacks.log';trace=trace_path.open('w')
        faulthandler.dump_traceback_later(30,repeat=True,file=trace)
        print('SCIENCE_START '+json.dumps({'source_commit':result['source_commit'],'stack_log':str(trace_path),'monotonic':start}),flush=True)
        try:
            outcome=app.scheduler.execute('capacity-full-daily',lambda:run_daily_diagnostic(day.diagnostic_date,
                DailyDiagnosticSources(input_backend=backend),fixture/'profile-daily'))
            result.update(science_wall_seconds=time.monotonic()-start,science_cpu_seconds=time.process_time()-cpu,
                science_peak_rss_bytes=rss(),daily_status=outcome['overall_status'],persisted=outcome['persisted'],
                scheduler=app.scheduler.status())
            assert outcome['persisted'] and outcome['overall_status'] in ('partial','complete'),outcome
        finally:
            faulthandler.cancel_dump_traceback_later();trace.close()
            finish=time.monotonic();stop.set();worker.join(timeout=20);app.close()
        assert not worker.is_alive() and not errors,errors
        starts=[row['started_monotonic'] for row in observations]
        result.update(live_max_interstart_seconds=max((b-a for a,b in zip(starts,starts[1:])),default=0),
            live_late_by_full_period=sum(row['lateness_seconds']>=10 for row in observations),
            live_terminal_gap_seconds=finish-starts[-1] if starts else None,stack_log=str(trace_path))
        result.update(synthetic_live_cycles=observations,live_errors=errors,
            live_scope='one-module actual collector, synthetic console; no physical RS485/Hycube/HA')
    print('CAPACITY_RESULT '+json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':main()
