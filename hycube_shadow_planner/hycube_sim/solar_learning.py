"""Per-site causal forecast calibration; original Guardian files stay read-only.

One rolling seven-day energy ratio per plant, recomputed once per completed UTC
hour. Raw forecasts are frozen before their target hour. No corrected forecast is
used as a training target. Learning state is private to this observer.
"""
import bisect
import hashlib
import json
import math
import sqlite3
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .model import InputError,iso,timestamp,resample,number

UTC=timezone.utc
PLANTS=('hycube','secondary')


def hour_floor(t):return t.astimezone(UTC).replace(minute=0,second=0,microsecond=0)


def integrate_hour(points,start,end,max_gap_seconds=60):
    """Trapezoids, clipped at exact UTC boundaries; no gap bridging/extrapolation."""
    energy=covered=0.0
    for (a,va),(b,vb) in zip(points,points[1:]):
        seconds=(b-a).total_seconds()
        if not 0<seconds<=max_gap_seconds or va is None or vb is None:continue
        lo=max(a,start);hi=min(b,end)
        if hi<=lo:continue
        left=(lo-a).total_seconds()/seconds;right=(hi-a).total_seconds()/seconds
        p0=va+(vb-va)*left;p1=va+(vb-va)*right
        duration=(hi-lo).total_seconds()
        energy+=(p0+p1)/2*duration/3600;covered+=duration
    return energy,covered


class Learner:
    def __init__(self,path,config):
        self.config=config
        self.db=sqlite3.connect(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS learning_state(plant TEXT,epoch TEXT,state TEXT,PRIMARY KEY(plant,epoch));
        CREATE TABLE IF NOT EXISTS frozen_forecasts(plant TEXT,epoch TEXT,hour TEXT,issued TEXT,captured TEXT,kwh REAL,PRIMARY KEY(plant,epoch,hour));
        CREATE TABLE IF NOT EXISTS measured_power(plant TEXT,epoch TEXT,t REAL,kw REAL,PRIMARY KEY(plant,epoch,t));
        CREATE TABLE IF NOT EXISTS read_offsets(plant TEXT,epoch TEXT,path TEXT,inode INTEGER,offset INTEGER,PRIMARY KEY(plant,epoch,path));
        CREATE TABLE IF NOT EXISTS hourly_errors(plant TEXT,epoch TEXT,hour TEXT,forecast REAL,actual REAL,coverage REAL,valid INTEGER,PRIMARY KEY(plant,epoch,hour));
        ''')
    def close(self):self.db.close()
    def epoch(self,plant,forecast,spec):
        raw={'plant':plant,'site_id':forecast.get('site_id'),'measurement':spec,'revision':self.config.get('calibration_revision','1')}
        return hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()[:20]
    def state(self,plant,epoch,fallback):
        row=self.db.execute('SELECT state FROM learning_state WHERE plant=? AND epoch=?',(plant,epoch)).fetchone()
        return json.loads(row[0]) if row else {'factor':fallback,'started_at':None,'last_update_hour':None,'status':'INITIAL_80_PERCENT'}
    def save(self,plant,epoch,state):
        self.db.execute('INSERT OR REPLACE INTO learning_state VALUES(?,?,?)',(plant,epoch,json.dumps(state,allow_nan=False)))
    def freeze(self,plant,epoch,forecast,now):
        issued=timestamp(forecast['generated_at'])
        if issued>now or now-issued>timedelta(hours=self.config.get('max_forecast_age_hours',8)):return
        first=hour_floor(now)
        if first<now:first+=timedelta(hours=1)
        last=first+timedelta(hours=self.config.get('forecast_hours',72))
        edges=[];t=first
        while t<=last:edges.append(t);t+=timedelta(hours=1)
        values=resample(forecast.get('intervals',[]),edges,plant)
        for t,value in zip(edges,values):
            if value is None or t<now:continue
            # Only future hours can be revised. On the following runs an already
            # started hour is never reintroduced with a more recent forecast.
            old=self.db.execute('SELECT issued FROM frozen_forecasts WHERE plant=? AND epoch=? AND hour=?',(plant,epoch,iso(t))).fetchone()
            if old and timestamp(old[0])>=issued:continue
            self.db.execute('INSERT OR REPLACE INTO frozen_forecasts VALUES(?,?,?,?,?,?)',(plant,epoch,iso(t),iso(issued),iso(now),value))
    def ingest(self,plant,epoch,spec,now):
        if spec.get('verified') is not True:return {'status':'WAITING_FOR_VERIFIED_MEASUREMENT','points':0}
        if spec.get('type')!='guardian_field':return {'status':'UNSUPPORTED_MEASUREMENT','points':0}
        root=Path(spec['directory']).resolve();field=spec['field']
        scale=float(spec.get('scale_to_kw',.001));limit=float(spec['max_power_kw'])
        if not math.isfinite(scale) or scale==0 or not math.isfinite(limit) or limit<=0:raise InputError('Ungültige PV-Messwertskalierung')
        budget=int(self.config.get('measurement_byte_budget',4194304));used=count=0
        # Existing old measurements cannot create seven days of paired forecasts.
        # Read from a small recent tail on first contact, incrementally thereafter.
        for path in sorted(root.glob('*.jsonl'))[-2:]:
            st=path.stat();old=self.db.execute('SELECT inode,offset FROM read_offsets WHERE plant=? AND epoch=? AND path=?',(plant,epoch,str(path))).fetchone()
            offset=old[1] if old and old[0]==st.st_ino and old[1]<=st.st_size else max(0,st.st_size-65536)
            with path.open('rb') as f:
                f.seek(offset)
                if not old and offset:f.readline()
                while f.tell()<st.st_size and used<budget:
                    before=f.tell();line=f.readline(65537)
                    if len(line)>65536:raise InputError('Guardian-PV-Zeile zu groß')
                    if not line.endswith(b'\n'):f.seek(before);break
                    used+=len(line)
                    try:
                        row=json.loads(line);t=timestamp(row['received_at'])
                        if t>now:continue
                        raw=row.get(field);kw=None
                        if not isinstance(raw,bool) and isinstance(raw,(float,int)) and math.isfinite(raw):
                            v=raw*scale
                            if 0<=v<=limit:kw=v
                        self.db.execute('INSERT OR REPLACE INTO measured_power VALUES(?,?,?,?)',(plant,epoch,t.timestamp(),kw));count+=1
                    except (KeyError,ValueError,TypeError):continue
                offset=f.tell()
            self.db.execute('INSERT OR REPLACE INTO read_offsets VALUES(?,?,?,?,?)',(plant,epoch,str(path),st.st_ino,offset))
        return {'status':'CONNECTED','points':count,'bytes_read':used,'source_field':field}
    def calculate(self,plant,epoch,state,now,fallback):
        boundary=hour_floor(now-timedelta(seconds=float(self.config.get('max_measurement_gap_seconds',60))))
        if state.get('last_update_hour')==iso(boundary):return state
        state['last_update_hour']=iso(boundary)
        if not state.get('started_at'):state['status']='WAITING_FOR_MEASUREMENTS';return state
        start=boundary-timedelta(days=7)
        forecasts=list(self.db.execute('SELECT hour,kwh,issued,captured FROM frozen_forecasts WHERE plant=? AND epoch=? AND hour>=? AND hour<? ORDER BY hour',(plant,epoch,iso(start),iso(boundary))))
        valid=[];eligible=0;covered_forecasts=0;days=set();max_gap=float(self.config.get('max_measurement_gap_seconds',60));minimum=float(self.config.get('minimum_forecast_hour_kwh',.02))
        for hour,fc,issued,captured in forecasts:
            a=timestamp(hour);b=a+timedelta(hours=1)
            if timestamp(issued)>a or timestamp(captured)>a:continue
            covered_forecasts+=1
            if fc<minimum:continue
            eligible+=1
            rows=self.db.execute('SELECT t,kw FROM measured_power WHERE plant=? AND epoch=? AND t>=? AND t<=? ORDER BY t',(plant,epoch,a.timestamp()-max_gap,b.timestamp()+max_gap))
            points=[(datetime.fromtimestamp(t,UTC),v) for t,v in rows]
            actual,coverage=integrate_hour(points,a,b,max_gap)
            complete=abs(coverage-3600)<.001
            self.db.execute('INSERT OR REPLACE INTO hourly_errors VALUES(?,?,?,?,?,?,?)',(plant,epoch,hour,fc,actual,coverage,int(complete)))
            if complete:
                valid.append((fc,actual));days.add(a.date().isoformat())
        predicted=sum(x for x,y in valid);actual=sum(y for x,y in valid)
        state.update(valid_hours=len(valid),eligible_hours=eligible,valid_days=len(days),forecast_hours=covered_forecasts,
            raw_forecast_kwh=predicted,actual_kwh=actual,
            raw_bias_kwh=(actual-predicted)/len(valid) if valid else None,
            raw_mae_kwh=sum(abs(y-x) for x,y in valid)/len(valid) if valid else None,
            training_until=iso(boundary),window_days=7)
        ready=(now-timestamp(state['started_at'])>=timedelta(days=7) and len(valid)>=int(self.config.get('minimum_valid_hours',24))
            and len(days)>=7 and eligible>0 and len(valid)/eligible>=float(self.config.get('minimum_daylight_coverage',.9))
            and covered_forecasts>=168*float(self.config.get('minimum_forecast_coverage',.95)) and predicted>=float(self.config.get('minimum_forecast_sum_kwh',1)))
        if ready:
            ratio=actual/predicted
            if 0<=ratio<=float(self.config.get('maximum_factor',2)):
                state.update(factor=ratio,status='LEARNED',last_learned_at=iso(now),learned_raw_ratio=ratio)
            else:state['status']='IMPLAUSIBLE_RATIO_KEEP_PREVIOUS'
        elif not state.get('last_learned_at'):
            state.update(factor=fallback,status='INITIAL_LEARNING' if now-timestamp(state['started_at'])<timedelta(days=7) else 'INSUFFICIENT_DATA_KEEP_INITIAL')
        else:state['status']='INSUFFICIENT_DATA_KEEP_LEARNED'
        return state
    def run(self,snapshot,now,fallback):
        result={}
        for plant in PLANTS:
            forecast=snapshot.get('forecasts',{}).get(plant,{})
            spec=self.config.get('measurements',{}).get(plant,{})
            epoch=self.epoch(plant,forecast,spec);state=self.state(plant,epoch,fallback)
            if forecast:self.freeze(plant,epoch,forecast,now)
            try:acquisition=self.ingest(plant,epoch,spec,now)
            except (InputError,OSError,KeyError,ValueError):
                state['status']='MEASUREMENT_READ_ERROR_KEEP_FACTOR'
                state['measurement']={'status':'READ_ERROR','points':0}
                self.save(plant,epoch,state);result[plant]={**state,'epoch':epoch};continue
            latest=self.db.execute('SELECT MAX(t) FROM measured_power WHERE plant=? AND epoch=? AND kw IS NOT NULL',(plant,epoch)).fetchone()[0]
            if latest is not None and now.timestamp()-latest<=float(self.config.get('max_measurement_gap_seconds',60)) and state.get('started_at') is None:
                state['started_at']=iso(now)
            state=self.calculate(plant,epoch,state,now,fallback)
            state['measurement']=acquisition
            if spec.get('verified') is not True:state['status']='WAITING_FOR_VERIFIED_MEASUREMENT'
            self.save(plant,epoch,state);result[plant]={**state,'epoch':epoch}
        self.db.commit();return result


def update_learning(snapshot,config,now,fallback):
    path=Path(config['state_file']).resolve()
    for spec in config.get('measurements',{}).values():
        if spec.get('directory') and path.is_relative_to(Path(spec['directory']).resolve()):raise InputError('Lernspeicher darf nicht in Guardian-Quelldateien liegen')
    path.parent.mkdir(parents=True,exist_ok=True)
    backup=Path(str(path)+'.last.json')
    learner=None
    try:
        learner=Learner(path,config)
        result=learner.run(snapshot,now,fallback)
        temp=Path(str(backup)+'.tmp');temp.write_text(json.dumps(result,allow_nan=False));temp.replace(backup)
        return result
    except (sqlite3.Error,OSError):
        try:previous=json.loads(backup.read_text())
        except (OSError,ValueError):previous={}
        result={}
        for plant in PLANTS:
            raw={'plant':plant,'site_id':snapshot.get('forecasts',{}).get(plant,{}).get('site_id'),'measurement':config.get('measurements',{}).get(plant,{}),'revision':config.get('calibration_revision','1')}
            epoch=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()[:20]
            old=previous.get(plant,{})
            result[plant]={**old,'factor':old.get('factor',fallback) if old.get('epoch')==epoch else fallback,'status':'LEARNING_STORE_ERROR_KEEP_FACTOR','epoch':epoch}
        return result
    finally:
        if learner is not None:learner.close()
