"""Read-only, bounded append proof for an already indexed frozen prefix.

The registry is explicitly injected, never discovered from an untrusted path.
No Raw bytes beyond the frozen indexed prefix are read and no consumer cursor
is advanced. An older read is labelled as-of; a new revision or unconfirmed
physical change remains unavailable. Proof limits are structural read budgets,
not HA latency/release budgets.
"""
import json
from projection_repository import Conflict, Unavailable
from raw_registry import RawRegistry, RegistryError, encoded, hashed, file_signature, MAX_BLOCK
from research_budget import check_budget


class ConfirmedPrefixReader:
    MAX_PROOF_PAGES = 4

    def __init__(self, registry):
        # A fresh read-only handle keeps metrics separate from the live writer.
        self.registry = RawRegistry(registry.root)

    def read_index_block(self, db, source, *, max_bytes):
        """Index the exact immutable receipt block, independent of backlog age.

        Unlike an interactive read this does not claim a current source view.
        The full original block hash and file identity prove these input bytes;
        later receipts/revisions are consumed separately in journal order. GET
        retains its stricter current-prefix/lineage gate without any relaxation.
        """
        registry=RawRegistry(self.registry.root)
        context=json.loads(source['context'])
        if context.get('registry_id')!=registry.config['registry_id']:
            raise Conflict('index source belongs to another registry')
        proof=db.execute('SELECT * FROM registry_proof WHERE registry_id=? AND source=? ORDER BY seq DESC LIMIT 1',
            (registry.config['registry_id'],source['source'])).fetchone()
        if proof is None or proof['receipt']!=source['receipt']:
            raise Conflict('index receipt proof changed')
        receipt=registry._receipt(proof['seq'])
        if (hashed(encoded(receipt))!=source['receipt'] or receipt['seq']>registry._head()['seq']
                or receipt['source']!=source['source'] or receipt['revision']!=source['revision']
                or receipt['path']!=source['path'] or receipt['writer_epoch']!=source['writer_epoch']
                or receipt['namespace']!=source['namespace'] or receipt['end']!=source['frontier']
                or receipt['file_id']!=[source['dev'],source['ino']]):
            raise Conflict('index receipt binding changed')
        if not receipt['start']<=source['cursor']<=receipt['end']:
            raise Conflict('index cursor outside its confirmed receipt block')
        size=receipt['end']-receipt['start']
        if size>max_bytes:
            raise Unavailable('confirmed index block exceeds selected byte budget')
        for phase in ('before','after'):
            sig=file_signature(source['path'])
            if sig[:2]!=receipt['file_id'] or sig[2]<receipt['end']:
                raise Conflict('confirmed index source replaced or truncated')
            if phase=='before':
                raw=registry._raw_range(source['path'],receipt['start'],receipt['end'])
                if hashed(raw)!=receipt['block_hash']:
                    raise Conflict('confirmed index block bytes changed')
        return raw[source['cursor']-receipt['start']:],len(raw)

    def verify(self, db, source, ref):
        try:
            return self._verify(db, source, ref)
        except RegistryError as exc:
            raise Conflict('confirmed prefix registry proof unavailable: '+str(exc)) from exc

    def _verify(self, db, source, ref):
        registry = RawRegistry(self.registry.root)
        context = json.loads(source['context'])
        if context.get('registry_id') != registry.config['registry_id']:
            raise Conflict('frozen prefix is not owned by the explicitly bound registry')
        proof = db.execute('SELECT * FROM registry_proof WHERE registry_id=? AND source=? ORDER BY seq DESC LIMIT 1',
                           (registry.config['registry_id'], ref.source_id)).fetchone()
        if proof is None or proof['receipt'] != source['receipt'] or proof['revision'] != ref.revision:
            raise Conflict('catalog receipt proof changed')
        current = registry._receipt(proof['seq'])
        if (hashed(encoded(current)) != source['receipt'] or current['source'] != ref.source_id
                or current['revision'] != ref.revision or current['end'] != source['frontier']
                or current['path'] != ref.path or current['writer_epoch'] != source['writer_epoch']
                or current['namespace'] != source['namespace']
                or tuple(current['file_id']) != (source['dev'], source['ino'])):
            raise Conflict('catalog receipt does not prove the frozen source')
        # An unchanged source's confirmed tip is an indexed, bounded lookup.
        # Activity in other sources must not age an immutable day out of the
        # append-proof budget. Only changed selected sources need the journal.
        selected_tip = registry.state(ref.source_id)
        if selected_tip == current:
            if file_signature(ref.path) != current['signature'] or ref.prefix_end > current['end']:
                raise Conflict('physical source differs from its confirmed append proof')
            return {'source':ref.source_id, 'revision':ref.revision, 'receipt':ref.receipt,
                    'indexed_prefix_end':ref.prefix_end, 'confirmed_prefix_end':current['end'],
                    'pending_confirmed_bytes':current['end']-ref.prefix_end,
                    'registry_sequence':current['seq'], 'proof_records':0,
                    'proof_metadata_bytes':registry.metrics['metadata_bytes']}
        next_seq, previous = current['seq']+1, source['receipt']
        frozen_head = registry._head()
        records = 0
        for _ in range(self.MAX_PROOF_PAGES):
            check_budget()
            if next_seq > frozen_head['seq']:
                break
            page = registry.read_page(next_seq=next_seq, previous_hash=previous,
                max_records=min(64, frozen_head['seq']-next_seq+1), max_bytes=MAX_BLOCK)
            if not page['records']:
                raise Unavailable('confirmed append proof did not reach its frozen registry frontier')
            records += len(page['records'])
            for receipt in page['records']:
                if receipt['source'] != ref.source_id:
                    continue
                if (receipt['mode'] not in ('append','adopt') or receipt['revision'] != ref.revision
                        or receipt['source_previous'] != hashed(encoded(current))
                        or receipt['start'] != current['end'] or receipt['block_previous'] != current['block_chain']
                        or any(receipt[key] != current[key] for key in ('path','writer_epoch','namespace','file_id'))):
                    raise Conflict('frozen prefix revision or append lineage changed')
                current = receipt
            next_seq, previous = page['next_seq'], page['previous_hash']
        if next_seq != frozen_head['seq']+1:
            raise Unavailable('confirmed append proof budget exceeded; older indexed prefix not proven within four registry pages')
        if previous != frozen_head['hash']:
            raise Conflict('frozen registry head proof differs')
        # Exact latest confirmed signature: no acceptance based on growth alone.
        # The caller separately hashes every selected original record, before
        # returning any result, and rechecks this proof after its bounded read.
        if file_signature(ref.path) != current['signature'] or ref.prefix_end > current['end']:
            raise Conflict('physical source differs from its confirmed append proof')
        return {'source':ref.source_id, 'revision':ref.revision, 'receipt':ref.receipt,
                'indexed_prefix_end':ref.prefix_end, 'confirmed_prefix_end':current['end'],
                'pending_confirmed_bytes':current['end']-ref.prefix_end,
                'registry_sequence':frozen_head['seq'], 'proof_records':records,
                'proof_metadata_bytes':registry.metrics['metadata_bytes']}
