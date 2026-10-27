"""Policy predecessor plus selected window from confirmed indexed Raw prefixes."""
from indexed_history_window import IndexedHistoryWindow
from cell_window_adapter import utc_seconds
from projection_repository import Unavailable
from research_budget import check_budget


class PreparedPolicyReader:
    def __init__(self,catalog,*,prefix_reader,consumer):
        self.catalog,self.consumer=catalog,consumer
        self.window=IndexedHistoryWindow(catalog,prefix_reader=prefix_reader)
        self.predecessor_decodes=0

    def _previous(self,stamp):
        ref=self.window.resolver.open_predecessor(stamp,'hycube_policy_valid')
        if ref is None:return None
        try:
            result=self.window.resolver.read_refs([ref])
            self.predecessor_decodes+=result['decodes']
            return result['records'][0]['record']
        finally:self.window.resolver.release_window(ref.snapshot_id)

    def latest_valid(self,*,require_current=False):
        if require_current:
            cursor=self.consumer.cursor()
            page=self.consumer.registry.read_page(next_seq=cursor[0],previous_hash=cursor[1],max_records=1)
            if page['records'] or self.catalog.status()['pending']:
                raise Unavailable('confirmed policy indexing pending; cannot infer policy change')
        return self._previous(1e20)

    def query(self,*,timestamp_from,timestamp_to,timing=None):
        from hycube_evidence import HycubePolicyHistory
        start,end=utc_seconds(timestamp_from),utc_seconds(timestamp_to)
        self.predecessor_decodes=0
        previous=None;previous_key=None
        changes=[]
        with self.window.read(start,end,predecessor_identity='hycube_policy_valid') as selected:
            for record,reference in selected:
                check_budget()
                if (reference.timestamp<=start and record.get('parse_quality')=='complete'
                        and (previous_key is None or reference.key>previous_key)):
                    previous,previous_key=record,reference.key
                if reference.timestamp>start and record.get('parse_quality')=='complete' and record.get('policy_changed',True):
                    changes.append(record)
        self.predecessor_decodes=self.window.metrics['predecessor_decodes']
        if timing:
            timing.counts(policy_files=self.window.metrics['files_opened'],
                policy_records=self.window.metrics['decodes'])
        return HycubePolicyHistory.segments(([previous] if previous else [])+changes,
            timestamp_from,timestamp_to,previous is not None)
