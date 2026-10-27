"""Container entrypoint: explicit prepared activation, otherwise legacy startup.

Offline migration never writes this operator-owned marker. A present invalid
marker is a hard failure, not permission to fall back to another writer owner.
"""
import hashlib
import json
from pathlib import Path

ACTIVATION_PATH=Path('/data/guardian-prepared-activation.json')
SHARED_ACTIVATION_PATH=Path('/share/guardian_battery/guardian-prepared-activation.json')
MIGRATION_REQUEST_PATH=Path('/share/guardian_battery/guardian-prepared-migration-request.json')
MAX_METADATA=65536
MAX_MIGRATION_STEPS=1_000_000


def _read(path):
    with path.open('rb') as handle:raw=handle.read(MAX_METADATA+1)
    if len(raw)>MAX_METADATA:raise ValueError('activation metadata exceeds budget')
    return raw


def _present(path):
    if path is None:return False
    try:Path(path).lstat()
    except FileNotFoundError:return False
    return True


def _migrate(request_path):
    """One explicit bounded offline turn, never acquisition or activation."""
    from version import GUARDIAN_VERSION,require_source_commit
    source=require_source_commit()
    request=json.loads(_read(Path(request_path)))
    fields={'contract','guardian_version','source_commit','root','plan','plan_sha256','steps'}
    if not isinstance(request,dict) or set(request)!=fields:
        raise ValueError('explicit migration request fields required')
    if (request['contract']!='guardian-prepared-migration-request-1' or
            request['guardian_version']!=GUARDIAN_VERSION or request['source_commit']!=source):
        raise ValueError('migration version/source contract mismatch')
    if type(request['steps']) is not int or not 1<=request['steps']<=MAX_MIGRATION_STEPS:
        raise ValueError('migration steps must be 1..1000000')
    if not isinstance(request['root'],str) or not isinstance(request['plan'],str):
        raise ValueError('explicit migration paths required')
    root=Path(request['root'])
    if not root.is_absolute() or not root.is_dir():raise ValueError('existing absolute migration root required')
    root=root.resolve();plan=(root/request['plan']).resolve()
    if not plan.is_relative_to(root):raise ValueError('migration plan outside root')
    with plan.open('rb') as handle:raw=handle.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('migration plan exceeds budget')
    if hashlib.sha256(raw).hexdigest()!=request['plan_sha256']:
        raise ValueError('migration plan hash mismatch')
    # Reuse the same frozen plan and resumable bounded steps as the sealed CLI.
    # Decode the verified bytes once; never reopen an unverified replacement.
    from prepared_migration import PreparedMigration
    migration=PreparedMigration(root,json.loads(raw));migration.begin()
    for _ in range(request['steps']):
        state=migration.step()
        if state['phase']=='complete':break
    print(json.dumps({'phase':state['phase'],'ordinal':state['ordinal'],
        'plan_sha256':migration.digest,'runtime_activated':False},sort_keys=True),flush=True)
    return 0


def main(*,activation_path=ACTIVATION_PATH,shared_activation_path=None,migration_request_path=None):
    active=[Path(path) for path in (activation_path,shared_activation_path) if _present(path)]
    migrate=_present(migration_request_path)
    if len(active)>1 or (migrate and active):
        raise ValueError('ambiguous startup controls; preserve files and select one operation')
    if migrate:return _migrate(migration_request_path)
    if not active:
        from main import main as legacy_main
        return legacy_main()
    activation_path=active[0]
    # Validate before candidate construction, directory creation or transport.
    from version import GUARDIAN_VERSION,require_source_commit
    source=require_source_commit()
    marker=json.loads(_read(activation_path))
    fields={'contract','guardian_version','source_commit','root','layout','layout_sha256'}
    if not isinstance(marker,dict) or set(marker)!=fields:
        raise ValueError('explicit activation fields required')
    if (marker['contract']!='guardian-prepared-activation-1' or
            marker['guardian_version']!=GUARDIAN_VERSION or marker['source_commit']!=source):
        raise ValueError('activation version/source contract mismatch')
    if not isinstance(marker['root'],str) or not isinstance(marker['layout'],str):
        raise ValueError('explicit activation paths required')
    root=Path(marker['root'])
    if not root.is_absolute() or not root.is_dir():raise ValueError('existing absolute activation root required')
    root=root.resolve();layout=(root/marker['layout']).resolve()
    if not layout.is_relative_to(root):raise ValueError('activation layout outside root')
    raw=_read(layout)
    if hashlib.sha256(raw).hexdigest()!=marker['layout_sha256']:
        raise ValueError('activation layout hash mismatch')
    layout_value=json.loads(raw)
    if not isinstance(layout_value,dict) or layout_value.get('contract')!='guardian-prepared-candidate-1':
        raise ValueError('prepared candidate layout required')
    from prepared_candidate import main as prepared_main
    return prepared_main(['--root',str(root),'--layout',str(layout),'--run'])


if __name__=='__main__':
    main(shared_activation_path=SHARED_ACTIVATION_PATH,migration_request_path=MIGRATION_REQUEST_PATH)
