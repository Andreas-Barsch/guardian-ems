"""Bounded manual time pages shared by chart and research request paths.

No cache, storage writes, hardware access or automatic retry. Existing inner
point cursors remain within a single page. Tokens are query-bound and signed.
"""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from maintenance import normalize_utc_timestamp
from research_budget import ResearchQueryError, check_budget

PAGE_SECONDS = 3600
MAX_DAYS = 90

class ManualHistoryPages:
    def __init__(self, secret=None):
        self.secret = secret or secrets.token_bytes(32)

    def run(self, endpoint, values, callback):
        check_budget()
        query = {k: v for k, v in values.items() if k not in {'cursor', 'paged'}}
        try:
            start = normalize_utc_timestamp(query['from'], 'from')
            end = normalize_utc_timestamp(query['to'], 'to')
            first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if not 0 <= (last-first).total_seconds() <= MAX_DAYS*86400:
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchQueryError('invalid_argument', 'A timezone-aware range of 0..90 days is required') from exc
        query.update({'from':start, 'to':end})
        if endpoint != 'chart' and query.get('resolution', 'auto') == 'auto':
            query['resolution'] = 'full' if (last-first).total_seconds() <= 86400 else 'display'
        key = hashlib.sha256(json.dumps([endpoint,query],sort_keys=True).encode()).hexdigest()
        lower, inner = start, None
        token = values.get('cursor')
        if token:
            try:
                if len(token)>16384 or not token.startswith('hp1.'):
                    raise ValueError()
                blob=base64.urlsafe_b64decode(token[4:]+'='*(-len(token[4:])%4))
                raw, sig=blob[:-32],blob[-32:]
                if not hmac.compare_digest(sig,hmac.new(self.secret,raw,hashlib.sha256).digest()):
                    raise ValueError()
                state=json.loads(raw)
                if state['query']!=key: raise ValueError()
                lower,inner=state['from'],state['inner']
                if not first<=datetime.fromisoformat(lower)<=last: raise ValueError()
            except (ValueError,KeyError,TypeError) as exc:
                raise ResearchQueryError('cursor_invalid','Time-page cursor is invalid; start the same query again') from exc
        upper=min(last,datetime.fromisoformat(lower)+timedelta(seconds=PAGE_SECONDS))
        page={**query,'from':lower,'to':upper.isoformat()}
        if inner: page['cursor']=inner
        body=callback(page)
        check_budget()
        continuation=body.get('next_cursor')
        if continuation:
            next_from=lower
        elif upper<last:
            next_from=(upper+timedelta(microseconds=1)).isoformat()
        else:
            next_from=None
        next_token=None
        if next_from:
            raw=json.dumps({'query':key,'from':next_from,'inner':continuation},separators=(',',':')).encode()
            next_token='hp1.'+base64.urlsafe_b64encode(raw+hmac.new(self.secret,raw,hashlib.sha256).digest()).decode().rstrip('=')
        body=dict(body)
        body.update(truncated=bool(next_token),next_cursor=next_token,
                    pagination={'requested_range':{'from':start,'to':end},
                      'page_range':{'from':lower,'to':upper.isoformat()},
                      'coverage_scope':'this_page_only','max_page_seconds':PAGE_SECONDS,
                      'next_cursor':next_token,'continuation':'manual_only',
                      'snapshot':'each_page_observed_separately',
                      'boundary_policy':'inclusive_microsecond_disjoint'})
        if isinstance(body.get('data'), dict) and 'next_cursor' in body['data']:
            body['data'] = {**body['data'], 'next_cursor': next_token,
                            'truncated': bool(next_token)}
        # Inner coverage stays explicitly scoped to this page, never to unread time.
        return body
