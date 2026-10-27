"""Selected persisted Daily outputs only; never compute or discover old days."""
import os
from datetime import date,timedelta
from guardian_diagnostics import (GuardianDiagnosticsRepository,DiagnosticsNotFound,
    InvalidDiagnosticDate,DiagnosticsCorrupt,validate_diagnostic_date)
from research_budget import check_budget,ResearchQueryError


class SelectedDailyRepository(GuardianDiagnosticsRepository):
    # Structural selected-input cap, not an HA latency/memory approval.
    MAX_BYTES=2*1024*1024
    bounded_reads=True

    def __init__(self,root,first,last):
        self.first=date.fromisoformat(validate_diagnostic_date(first))
        self.last=date.fromisoformat(validate_diagnostic_date(last))
        if not 0 <= (self.last-self.first).days <= 32:
            raise ResearchQueryError('history_window_budget','Daily window must be at most 32 days',503)
        self.bytes_read=0;self.files_read=0
        super().__init__(root,text_reader=self._text)

    def _text(self,path):
        check_budget()
        remaining=self.MAX_BYTES-self.bytes_read
        with path.open('rb') as handle:
            before=os.fstat(handle.fileno())
            if before.st_size>remaining:
                raise ResearchQueryError('history_window_budget','Selected Daily input byte budget exceeded',503)
            raw=handle.read(remaining+1)
            after=os.fstat(handle.fileno())
        self.bytes_read+=len(raw);self.files_read+=1
        if len(raw)>remaining:
            raise ResearchQueryError('history_window_budget','Selected Daily input byte budget exceeded',503)
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ResearchQueryError('history_preparation_required','Selected Daily output changed during read',503)
        check_budget()
        return raw.decode('utf-8')

    def available(self,limit=None):
        valid,corrupt=[],[]
        selected=self.last
        while selected>=self.first:
            check_budget()
            if limit is not None and len(valid)>=limit:break
            text=selected.isoformat()
            try:valid.append(self.result(text))
            except DiagnosticsNotFound:pass
            except (InvalidDiagnosticDate,DiagnosticsCorrupt):
                corrupt.append(text);self._warn_once(text,'derived result unavailable date='+text)
            selected-=timedelta(days=1)
        return valid,corrupt
