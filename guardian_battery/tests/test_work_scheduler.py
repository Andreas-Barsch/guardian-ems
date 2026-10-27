import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from work_scheduler import (WorkScheduler, WorkCancelled, AdmissionError,
                            checkpoint, atomic)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, 'test synchronization timeout'
        time.sleep(.001)


def test_one_admission_retains_stack_and_progress():
    s=WorkScheduler(quantum_checks=2)
    entered=threading.Event(); release=threading.Event(); values=[]
    def first():
        entered.set(); assert release.wait(5)
        local=[]
        for n in range(100):
            checkpoint(); local.append(n); values.append(('first',n))
        return local
    def second():
        for n in range(100):
            checkpoint(); values.append(('second',n))
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(s.execute,'analysis',first)
        assert entered.wait(5)
        b=pool.submit(s.execute,'daily',second)
        wait_for(lambda:len(s.status()['active'])==2);release.set()
        assert a.result(5)==list(range(100));b.result(5)
    assert values.index(('second',0)) < values.index(('first',99))
    assert all(j['yields']>0 for j in s.status()['recent'])


def test_raw_pauses_derivation_but_admits_verified_reads():
    s=WorkScheduler(); ran=threading.Event(); derived=threading.Event()
    with ThreadPoolExecutor(2) as pool:
        with s.raw_section():
            pending=pool.submit(s.execute,'daily',derived.set)
            wait_for(lambda:len(s.status()['active'])==1)
            read=pool.submit(s.execute,'manual',ran.set,lane='read',deadline=time.monotonic()+1)
            read.result(2)
            assert ran.is_set() and not derived.is_set()
            assert s.status()['raw_active']==1
        pending.result(5)
    assert derived.is_set()


def test_separate_read_lane_and_capacity():
    s=WorkScheduler(capacity=2);entered=threading.Event();release=threading.Event()
    def held():entered.set();assert release.wait(5)
    with ThreadPoolExecutor(2) as pool:
        f=pool.submit(s.execute,'derive',held);assert entered.wait(5)
        assert s.execute('read',lambda:42,lane='read')==42
        queued=pool.submit(s.execute,'daily',lambda:None)
        wait_for(lambda:len(s.status()['active'])==2)
        with pytest.raises(AdmissionError):s.execute('overflow',lambda:None)
        release.set();f.result(5);queued.result(5)
    assert s.status()['rejected']==1


@pytest.mark.parametrize('mode',['cancel','deadline','close'])
def test_waiting_request_cancels_without_running_callback(mode):
    s=WorkScheduler();stop=threading.Event();called=[]
    entered=threading.Event();release=threading.Event()
    def held():entered.set();assert release.wait(5)
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(s.execute,'held_read',held,lane='read')
        assert entered.wait(5)
        try:
            f=pool.submit(s.execute,'read',lambda:called.append(1),lane='read',
                          cancelled=stop.is_set,
                          deadline=time.monotonic()+.05 if mode=='deadline' else None)
            wait_for(lambda:len(s.status()['active'])==2)
            if mode=='cancel':stop.set()
            if mode=='close':s.close()
            with pytest.raises(WorkCancelled):f.result(5)
        finally:
            release.set()
        if mode=='close':
            with pytest.raises(WorkCancelled):first.result(5)
        else:first.result(5)
    assert not called and not s.status()['active']
    assert next(j for j in s.status()['recent'] if j['kind']=='read')['state']=='cancelled'


def test_atomic_defers_cancel_until_shared_transaction_released():
    s=WorkScheduler();stop=threading.Event();steps=[]
    def work():
        with atomic():
            stop.set();checkpoint();steps.append('committed')
        steps.append('released');checkpoint();steps.append('wrong')
    with pytest.raises(WorkCancelled):s.execute('write',work,cancelled=stop.is_set)
    assert steps==['committed','released']


def test_exception_cleanup_and_bounded_history():
    s=WorkScheduler(history_limit=2)
    with pytest.raises(ValueError):s.execute('bad',lambda:(_ for _ in ()).throw(ValueError()))
    for i in range(4):assert s.execute('good',lambda:i)==i
    assert not s.status()['active'] and len(s.status()['recent'])==2


def test_legacy_live_gate_does_not_reject_scheduled_request():
    import history_live_priority as live
    from research_budget import check_budget
    s=WorkScheduler()
    live.cycle_started()
    try:
        def read():
            with live.historical_slot():check_budget();return 17
        assert s.execute('manual',read,lane='read')==17
    finally:live.collector_stopped()


def test_cancel_is_not_partial_scientific_success():
    s=WorkScheduler();stop=threading.Event()
    def scientific():
        try:
            stop.set();checkpoint()
        except Exception:return 'wrong partial result'
    with pytest.raises(WorkCancelled):s.execute('daily',scientific,cancelled=stop.is_set)


def test_raw_priority_uses_selected_composition():
    from history_live_priority import raw_priority
    s=WorkScheduler()
    @raw_priority
    def write():return s.status()['raw_active']
    assert write()==0
    with s.select():assert write()==1
    assert s.status()['raw_active']==0


def test_nested_work_does_not_take_another_lease():
    s=WorkScheduler(capacity=1)
    assert s.execute('outer',lambda:s.execute('inner',lambda:3))==3
    with pytest.raises(AdmissionError):
        s.execute('outer',lambda:WorkScheduler().execute('inner',lambda:3))


def test_paused_reader_lock_holder_can_finish_before_waiter():
    from research_budget import budget_lock
    s=WorkScheduler(quantum_checks=1);lock=threading.Lock()
    held=threading.Event();release=threading.Event();order=[]
    def first():
        with budget_lock(lock):
            held.set();assert release.wait(5)
            for _ in range(10):checkpoint()
            order.append(1)
    def second():
        with budget_lock(lock):order.append(2)
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(s.execute,'first',first,lane='read');assert held.wait(5)
        b=pool.submit(s.execute,'second',second,lane='read')
        wait_for(lambda:len(s.status()['active'])==2);release.set()
        a.result(5);b.result(5)
    assert order==[1,2]


def test_nested_cancellation_applies_inside_callback():
    s=WorkScheduler();stop=threading.Event()
    def child():stop.set();checkpoint()
    with pytest.raises(WorkCancelled):
        s.execute('parent',lambda:s.execute('child',child,cancelled=stop.is_set))
    assert s.status()['recent'][-1]['state']=='cancelled'
