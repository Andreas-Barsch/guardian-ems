import threading
import time

from derived_mqtt_worker import DerivedMqttWorker


def wait_for(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(): return True
        time.sleep(.005)
    return False


def job(generation):
    return {"key": (generation, "cfg", ((1, "SN"),), 0),
            "generation": generation, "positions": (1,), "results": {}}


def test_first_generation_runs_without_blocking_submit_and_deduplicates_success():
    release = threading.Event(); seen = []
    def publish(value):
        seen.append(value["generation"]); release.wait(); return {"success": True}
    worker = DerivedMqttWorker(publish); worker.start()
    started = time.monotonic(); assert worker.submit(job(1))
    assert time.monotonic() - started < .01
    assert wait_for(lambda: worker.status()["active"])
    assert worker.submit(job(1)) is False
    release.set(); assert wait_for(lambda: worker.status()["generation_last_completed"] == 1)
    assert worker.submit(job(1)) is False
    assert worker.stop()


def test_one_active_one_latest_pending_coalesces_intermediate_generations():
    release = threading.Event(); seen = []
    def publish(value):
        seen.append(value["generation"])
        if value["generation"] == 1: release.wait()
        return {"success": True}
    worker = DerivedMqttWorker(publish); worker.start(); worker.submit(job(1))
    assert wait_for(lambda: worker.status()["generation_active"] == 1)
    worker.submit(job(2)); worker.submit(job(3)); worker.submit(job(4))
    status = worker.status()
    assert status["generation_pending"] == 4
    assert status["coalesced_count"] == 2
    release.set(); assert wait_for(lambda: worker.status()["generation_last_completed"] == 4)
    assert seen == [1, 4]; assert worker.stop()


def test_failure_isolated_and_newest_pending_continues():
    seen = []
    def publish(value):
        seen.append(value["generation"])
        if value["generation"] == 1: raise OSError("broken")
        return {"success": True, "publish_count": 5}
    worker = DerivedMqttWorker(publish); worker.start(); worker.submit(job(1))
    assert wait_for(lambda: worker.status()["failure_count"] == 1)
    assert worker.status()["last_successfully_published_generation"] is None
    worker.submit(job(2)); assert wait_for(lambda: worker.status()["generation_last_completed"] == 2)
    assert worker.status()["last_completed"]["publish_count"] == 5
    assert seen == [1, 2]; assert worker.stop()


def test_return_code_style_failure_and_reconnect_make_generation_submitable():
    worker = DerivedMqttWorker(lambda _job: {"success": False, "error": "rc"})
    worker.start(); worker.submit(job(1))
    assert wait_for(lambda: worker.status()["failure_count"] == 1)
    assert worker.submit(job(1))
    assert wait_for(lambda: worker.status()["failure_count"] == 2)
    worker.stop()
    successful = DerivedMqttWorker(lambda _job: {"success": True}); successful.start()
    successful.submit(job(1)); assert wait_for(lambda: successful.status()["last_successfully_published_generation"] == 1)
    successful.invalidate_reconnect()
    assert successful.status()["reconnect_invalidated"] is True
    assert successful.submit(job(1)); successful.stop()


def test_config_identity_and_restart_provenance_are_independent_jobs():
    seen = []
    worker = DerivedMqttWorker(lambda value: seen.append(value["key"]) or
                               {"success": True})
    worker.start()
    base = job(1)
    assert worker.submit(base)
    assert wait_for(lambda: len(seen) == 1)
    changed_config = dict(base, key=(1, "cfg-2", ((1, "SN"),), 0))
    assert worker.submit(changed_config)
    assert wait_for(lambda: len(seen) == 2)
    changed_identity = dict(base, key=(1, "cfg-2", ((1, "SN-NEW"),), 0))
    assert worker.submit(changed_identity)
    assert wait_for(lambda: len(seen) == 3)
    assert worker.stop()
    restarted = DerivedMqttWorker(lambda _value: {"success": True})
    restarted.start()
    assert restarted.submit(changed_identity)
    assert restarted.stop()


def test_shutdown_discards_pending_and_is_bounded():
    release = threading.Event()
    worker = DerivedMqttWorker(lambda _job: release.wait(.1) or {"success": True})
    worker.start(); worker.submit(job(1)); assert wait_for(lambda: worker.status()["active"])
    worker.submit(job(2)); started = time.monotonic(); result = worker.stop(timeout=.01)
    assert not result and time.monotonic() - started < .05
    assert worker.status()["pending"] is False
    release.set(); assert wait_for(lambda: worker.stop(.2))
