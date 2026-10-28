"""Read Guardian's existing shared evidence; cache only inside simulator output.

All Guardian files opened rb. No writers, MQTT publishing, device HTTP or control.
Incremental byte/time budgets protect the active collector from large backfills.
"""
import bisect
import json
import math
import sqlite3
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .model import timestamp,iso,InputError,number


def tail_record(directory):
    for path in sorted(Path(directory).glob('*.jsonl'),reverse=True)[:2]:
        with path.open('rb') as f:
            offset=max(0,path.stat().st_size-1048576);f.seek(offset)
            if offset:f.readline()
            lines=f.read(1048576).splitlines()
        for line in reversed(lines):
            try:
                row=json.loads(line)
                if isinstance(row,dict):return row
            except (ValueError,UnicodeError):pass
    raise InputError('Keine vollständige Guardian-Beobachtung verfügbar')


def current(spec,now):
    root=Path(spec['root']);r=tail_record(root/'hycube_history')
    at=timestamp(r['received_at'])
    if not -60<=(now-at).total_seconds()<=600:raise InputError('Guardian-Hycube-Empfang ist veraltet oder zukünftig')
    soc=number(r['BatteryCapacity'],'BatteryCapacity',0,100)
    observation={'value':soc,'unit':'%','observed_at':iso(at),'source':'guardian.hycube','timestamp_basis':'received_at'}
    flows={}
    for name,field in [('house_power','HomePower'),('hycube_pv_power','solarPower'),('external_power','ExternalPower'),('grid_power','GridPower'),('battery_power','BatteryPower')]:
        value=r.get(field)
        if isinstance(value,(float,int)) and not isinstance(value,bool) and math.isfinite(value):
            flows[name]={'value':value,'unit':'W','observed_at':iso(at),'source':'guardian.hycube','source_field':field,'sign_semantics':'Originalvorzeichen; keine unbestätigte Richtungsumkehr'}
    return observation,flows


def read_history(spec,now):
    root=Path(spec['root']).resolve();cache=Path(spec['cache']).resolve()
    if cache.is_relative_to(root):raise InputError('Simulatorcache darf nicht im Guardian-Verzeichnis liegen')
    cache.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(cache)
    try:return _read(db,root,spec,now)
    except sqlite3.Error as exc:raise InputError('Eigener Guardian-Lesecache nicht verfügbar') from exc
    finally:db.close()


def _read(db,root,spec,now):
    db.executescript('''CREATE TABLE IF NOT EXISTS offsets(path TEXT PRIMARY KEY,inode INTEGER,offset INTEGER);
    CREATE TABLE IF NOT EXISTS pack(t REAL PRIMARY KEY,soc REAL,source TEXT);
    CREATE TABLE IF NOT EXISTS cells(t REAL,serial TEXT,row TEXT,source TEXT,PRIMARY KEY(t,serial));''')
    since=now-timedelta(days=16);budget=int(spec.get('byte_budget',33554432));deadline=time.monotonic()+float(spec.get('time_budget_seconds',8));used=0;pending=0;pack_pending=0;invalid=0
    files=[]
    for directory in ('hycube_history_projection','cell_history'):
        files += [(directory,p) for p in sorted((root/directory).glob('*.jsonl'),reverse=True) if since.date().isoformat()<=p.stem<=now.date().isoformat()]
    for directory,path in files:
        st=path.stat();old=db.execute('SELECT inode,offset FROM offsets WHERE path=?',(str(path),)).fetchone();offset=old[1] if old else 0
        if old and (old[0]!=st.st_ino or offset>st.st_size):
            db.execute('DELETE FROM '+('pack' if directory=='hycube_history_projection' else 'cells')+' WHERE source=?',(str(path),));offset=0
        if offset<st.st_size and (used>=budget or time.monotonic()>deadline):
            pending+=1;pack_pending+=int(directory=="hycube_history_projection");continue
        with path.open('rb') as f:
            f.seek(offset)
            while f.tell()<st.st_size and used<budget and time.monotonic()<=deadline:
                begin=f.tell();line=f.readline(65537)
                if len(line)>65536:raise InputError('Guardian-Zeile überschreitet Leselimit')
                if not line.endswith(b'\n'):f.seek(begin);break
                used+=len(line)
                try:
                    r=json.loads(line)
                    if directory=='hycube_history_projection':
                        t=timestamp(r['received_at']).timestamp();value=number(r['battery_capacity'],'pack SOC',0,100)
                        db.execute('INSERT OR REPLACE INTO pack VALUES(?,?,?)',(t,value,str(path)))
                    else:
                        t=number(r['timestamp'],'timestamp',1);serial=r.get('module_serial')
                        if not isinstance(serial,str) or not serial:raise InputError('Physische Seriennummer fehlt')
                        cells=r['voltages_mv']
                        if len(cells)!=15:raise InputError('Unvollständiger Zellframe')
                        cells=[number(v,'cell mV',1500,4500) for v in cells]
                        normalized={'serial':serial,'cells_mv':cells,'temperature_ok':None,'protection_clear':r.get('protection_clear') if isinstance(r.get('protection_clear'),bool) else None,'temperatures_c':r.get('temperatures_c'),'balancing':r.get('balancing'),'module_soc_percent':r.get('soc_percent')}
                        db.execute('INSERT OR REPLACE INTO cells VALUES(?,?,?,?)',(t,serial,json.dumps(normalized),str(path)))
                except (KeyError,ValueError,TypeError):invalid+=1
            offset=f.tell()
        if offset<st.st_size:
            pending+=1;pack_pending+=int(directory=="hycube_history_projection")
        db.execute('INSERT OR REPLACE INTO offsets VALUES(?,?,?)',(str(path),st.st_ino,offset))
    db.commit()
    pack=list(db.execute('SELECT t,soc FROM pack WHERE t>=? AND t<=? ORDER BY t',(since.timestamp(),now.timestamp())))
    full=[t for t,s in pack if s>=100];last_full=max(full,default=None)
    gaps=[b[0]-a[0] for a,b in zip(pack,pack[1:])]
    covered=bool(pack) and pack_pending==0 and invalid==0 and (now.timestamp()-pack[-1][0])<=60 and (now.timestamp()-pack[0][0])>14*86400 and max(gaps,default=1e9)<=60
    since_full=last_full if last_full is not None else (pack[0][0] if pack else now.timestamp())
    overdue=covered and (now.timestamp()-since_full)>14*86400
    # Select most recent high-SOC period, then evaluate raw cells, not display averages.
    windows=[];begin=None
    previous=None
    for t,soc in pack:
        if previous is not None and t-previous>60:begin=None
        previous=t
        if soc<95:
            if begin is not None:windows.append((begin,t))
            begin=None
        elif begin is None and soc>=98:begin=t
    if begin is not None:windows.append((begin,pack[-1][0]))
    selected=next(((a,b) for a,b in reversed(windows) if b-a>=7200),None)
    lo,hi=selected if selected else (now.timestamp()-3*3600,now.timestamp())
    # Bound the full-window evaluator; latest 4 hours suffice for a recent hold check.
    lo=max(lo,hi-4*3600)
    serials=spec['module_serials'];by={s:[] for s in serials}
    for t,serial,row in db.execute('SELECT t,serial,row FROM cells WHERE t>=? AND t<=? ORDER BY t',(lo,hi)):
        if serial in by:by[serial].append((t,json.loads(row)))
    indices={s:[x[0] for x in vals] for s,vals in by.items()};pt=[x[0] for x in pack];frames=[]
    for anchor,_ in by[serials[0]]:
        modules=[];ts=[]
        for serial in serials:
            seq=indices[serial];i=bisect.bisect_left(seq,anchor);candidates=[j for j in (i-1,i) if 0<=j<len(seq)]
            if not candidates:break
            j=min(candidates,key=lambda j:abs(seq[j]-anchor))
            if abs(seq[j]-anchor)>5:break
            ts.append(seq[j]);modules.append(by[serial][j][1])
        if len(modules)!=len(serials) or max(ts)-min(ts)>5:continue
        t=max(ts);i=bisect.bisect_right(pt,t)-1
        if i<0 or t-pt[i]>30:continue
        frames.append({'at':iso(datetime.fromtimestamp(t,timezone.utc)),'pack_soc_percent':pack[i][1],'modules':modules})
    last_cells=[]
    for serial in serials:
        r=db.execute('SELECT t,row FROM cells WHERE serial=? ORDER BY t DESC LIMIT 1',(serial,)).fetchone()
        if r:last_cells.append({'at':iso(datetime.fromtimestamp(r[0],timezone.utc)),**json.loads(r[1])})
    return {'sample_interval_seconds':60,'module_serials':serials,'frames':frames,
        'maintenance_evidence':{'last_observed_full_charge':iso(datetime.fromtimestamp(last_full,timezone.utc)) if last_full else None,'more_than_14_days_without_full_charge_proven':overdue,'continuous_14_day_coverage':covered,'coverage_first':iso(datetime.fromtimestamp(pack[0][0],timezone.utc)) if pack else None,'coverage_last':iso(datetime.fromtimestamp(pack[-1][0],timezone.utc)) if pack else None,'largest_gap_seconds':max(gaps,default=None),
          'recommendation':'Vollladung zur Wartung prüfen: mehr als 14 belegte Tage ohne 100 Prozent.' if overdue else 'Kein zeitlicher Vollladebedarf belegt; Datenlücken sind kein Nachweis ausgebliebener Vollladungen.'},
        'latest_modules':last_cells,'acquisition':{'bytes_read':used,'pending_files':pending,'invalid_records':invalid,'pack_points':len(pack),'common_frames':len(frames),'source':'Guardian-Dateien rb; eigener inkrementeller Cache','missing_protection_evidence':True}}
