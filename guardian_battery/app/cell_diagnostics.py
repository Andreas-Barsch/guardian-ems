from __future__ import annotations
import json, statistics
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path

@dataclass
class CellSample:
    timestamp: float; module: int; voltages_mv: list[int]; current_a: float; soc_percent: float
    temperatures_c: list[float]; balancing: list[bool]


DIAGNOSTIC_PARAMETER_META = {
    "status": {
        "label": "Bewertung", "unit": "dimensionslos", "source": "Guardian-Berechnung",
        "definition": "Phasenbezogener diagnostischer Status der Zelle.",
        "interpretation": "NORMAL / BEOBACHTEN / AUFFÄLLIG / KRITISCH / LERNPHASE. Kein SOH-Prozentwert."
    },
    "confidence": {
        "label": "Confidence", "unit": "dimensionslos", "source": "Guardian-Berechnung",
        "definition": "Vertrauensstufe aus Zahl geeigneter Messpunkte und Datenabdeckung.",
        "interpretation": "LOW / MEDIUM / HIGH. Ein auffälliger Status mit LOW Confidence ist ein vorläufiger Hinweis."
    },
    "voltage": {
        "label": "Zellspannung", "unit": "mV", "source": "Pylontech BMS / bat",
        "definition": "Aktuell gemessene Spannung der Zellgruppe.",
        "interpretation": "Ein Einzelwert allein erlaubt keine Aussage über Zellgesundheit."
    },
    "deviation": {
        "label": "Abweichung zum Modulmedian", "unit": "mV", "source": "Guardian-Berechnung",
        "definition": "ΔVᵢ = Vᵢ − Median(V₁…V₁₅).",
        "interpretation": "Negativ = unter Modulmedian; positiv = über Modulmedian. Bedeutung ist phasenabhängig."
    },
    "evidence": {
        "label": "Evidenzabweichung", "unit": "mV", "source": "Guardian-Berechnung",
        "definition": "Größte absolute phasenbezogene Medianabweichung aus ausreichend belegten relevanten Phasen.",
        "interpretation": "Nur zusammen mit Phase, Messpunktzahl, Persistenz und Confidence interpretieren."
    },
    "lowest": {
        "label": "Lowest-Anteil", "unit": "%", "source": "Guardian-Berechnung",
        "definition": "Anteil gültiger Messpunkte einer Phase, in denen die Zelle die niedrigste Spannung im Modul hatte.",
        "interpretation": "Ein hoher Anteil allein beweist keinen Defekt; Betrag und Persistenz sind mit zu bewerten."
    },
    "highest": {
        "label": "Highest-Anteil", "unit": "%", "source": "Guardian-Berechnung",
        "definition": "Anteil gültiger Messpunkte einer Phase, in denen die Zelle die höchste Spannung im Modul hatte.",
        "interpretation": "Ein hoher Anteil allein beweist keinen Defekt; Betrag und Persistenz sind mit zu bewerten."
    },
    "rank": {
        "label": "Mittlerer Rang", "unit": "Rang von 15", "source": "Guardian-Berechnung",
        "definition": "Mittlere Spannungsrangposition innerhalb der 15 Zellgruppen; Rang 1 = höchste, Rang 15 = niedrigste Spannung.",
        "interpretation": "Dimensionsloser relativer Kennwert. Nur zusammen mit Spannungsabweichung und Stichprobengröße bewerten."
    },
    "samples": {
        "label": "Gültige Messpunkte", "unit": "Messpunkte", "source": "Guardian-Berechnung",
        "definition": "Anzahl der Messpunkte, die der jeweiligen Betriebsphase zugeordnet wurden.",
        "interpretation": "Kleine Stichproben reduzieren die Aussagekraft und damit die Confidence."
    }
}

class CellDiagnosticStore:
    def __init__(self, path: Path, max_samples_per_module: int = 8640):
        self.path=path; self.max_samples=max_samples_per_module
        self.samples=defaultdict(lambda: deque(maxlen=max_samples_per_module)); self._load()
    def _load(self):
        try:
            if self.path.exists():
                for k,v in json.loads(self.path.read_text()).get('samples',{}).items(): self.samples[int(k)].extend(v[-self.max_samples:])
        except Exception: self.samples.clear()
    def save(self):
        tmp=self.path.with_suffix('.tmp'); tmp.write_text(json.dumps({'samples':{str(k):list(v) for k,v in self.samples.items()}},separators=(',',':'))); tmp.replace(self.path)
    def add(self,s): self.samples[s.module].append(asdict(s))
    @staticmethod
    def phases(s,o):
        a=[]; i=float(s['current_a']); soc=float(s['soc_percent']); mean=statistics.fmean(s['voltages_mv'])/1000
        a.append('charge' if i>=o['cell_diag_charge_current_a'] else 'discharge' if i<=-o['cell_diag_discharge_current_a'] else 'rest')
        if soc<=o['cell_diag_low_soc_percent'] or mean<=3.22: a.append('low')
        if soc>=o['cell_diag_high_soc_percent'] or mean>=3.38: a.append('high')
        return a
    def analyse(self,module,o):
        vals=list(self.samples.get(module,()))
        if not vals: return {'module':module,'status':'LERNPHASE','confidence':'LOW','sample_count':0,'current_median_mv':None,'cells':[],'method':'Phase-Resolved Cell Voltage Consistency'}
        n=15; names=('discharge','low','charge','high','rest'); st={p:[{'n':0,'dev':[],'low':0,'high':0,'ranks':[]} for _ in range(n)] for p in names}
        for s in vals:
            v=s['voltages_mv'];
            if len(v)!=15: continue
            med=statistics.median(v); lo=min(v); hi=max(v); order=sorted(set(v),reverse=True); ranks={}; pos=1
            for x in order:
                cnt=v.count(x); ranks[x]=(pos+pos+cnt-1)/2; pos+=cnt
            for p in self.phases(s,o):
                for j,x in enumerate(v):
                    q=st[p][j]; q['n']+=1; q['dev'].append(x-med); q['ranks'].append(ranks[x]); q['low']+=x==lo; q['high']+=x==hi
        cells=[]
        for j in range(n):
            pp={}; relevant=[]
            for p in names:
                q=st[p][j]
                if q['n']:
                    pp[p]={'samples':q['n'],'median_deviation_mv':round(statistics.median(q['dev']),1),'mean_rank':round(statistics.fmean(q['ranks']),2),'lowest_percent':round(100*q['low']/q['n'],1),'highest_percent':round(100*q['high']/q['n'],1)}
                    if p!='rest' and q['n']>=o['cell_diag_min_phase_samples']: relevant.append(abs(pp[p]['median_deviation_mv']))
                else: pp[p]={'samples':0}
            ev=max(relevant,default=0); valid=max((pp[p]['samples'] for p in ('discharge','low','charge','high')),default=0)
            status='LERNPHASE' if valid<o['cell_diag_min_phase_samples'] else 'KRITISCH' if ev>=o['cell_diag_critical_deviation_mv'] else 'AUFFÄLLIG' if ev>=o['cell_diag_warning_deviation_mv'] else 'BEOBACHTEN' if ev>=o['cell_diag_observe_deviation_mv'] else 'NORMAL'
            conf='HIGH' if valid>=o['cell_diag_confidence_high_samples'] else 'MEDIUM' if valid>=o['cell_diag_confidence_medium_samples'] else 'LOW'
            lv=vals[-1]['voltages_mv']; lm=statistics.median(lv)
            cells.append({'cell':j+1,'status':status,'confidence':conf,'current_voltage_mv':lv[j],'current_deviation_mv':round(lv[j]-lm,1),'evidence_deviation_mv':ev,'phases':pp})
        order={'KRITISCH':4,'AUFFÄLLIG':3,'BEOBACHTEN':2,'NORMAL':1,'LERNPHASE':0}; worst=max(cells,key=lambda c:(order[c['status']],c['evidence_deviation_mv']))
        return {'module':module,'status':worst['status'],'confidence':worst['confidence'],'sample_count':len(vals),'current_median_mv':round(statistics.median(vals[-1]['voltages_mv']),1),'evidence_worst_cell':worst['cell'],'evidence_deviation_mv':worst['evidence_deviation_mv'],'method':'Phase-Resolved Cell Voltage Consistency','cells':cells,'dynamic_resistance':'DATENSAMMLUNG','capacity_consistency':'NICHT BEWERTBAR','rest_drift':'DATENSAMMLUNG','ica_dva':'NICHT BEWERTBAR'}
