from zoneinfo import ZoneInfo

from hub.services.db import hub_database
from hub.services.scheduler import AppScheduler, Scheduler


def test_run_now_records_history_and_errors(tmp_path):
    s = Scheduler(hub_database(tmp_path), ZoneInfo("Asia/Kolkata"))
    a = AppScheduler(s, "demo")
    calls = []
    a.cron("ok", lambda: calls.append(1), "0 7 * * *")
    a.interval("bad", lambda: 1 / 0, minutes=5)

    assert a.last_run("ok") is None
    assert a.run_now("ok", wait=True)
    assert calls == [1]
    run = a.last_run("ok")
    assert run["status"] == "ok" and run["trigger"] == "manual" and run["finished_at"]

    assert a.run_now("bad", wait=True)  # exception is contained
    bad = a.last_run("bad")
    assert bad["status"] == "error" and "ZeroDivisionError" in bad["error"]

    ids = [j["id"] for j in s.jobs()]
    assert ids == ["demo:bad", "demo:ok"]
    s.remove_app("demo")
    assert s.jobs() == []


def test_run_now_refuses_overlap(tmp_path):
    import threading

    s = Scheduler(hub_database(tmp_path), ZoneInfo("UTC"))
    a = AppScheduler(s, "demo")
    gate = threading.Event()
    a.interval("slow", gate.wait, minutes=5)
    assert a.run_now("slow")
    assert a.is_running("slow")  # true as soon as run_now returns, so the UI can start polling
    for _ in range(100):
        if (a.last_run("slow") or {}).get("status") == "running":
            break
        threading.Event().wait(0.01)
    assert a.is_running("slow") and a.last_run("slow")["status"] == "running"
    assert a.run_now("slow") is False
    gate.set()
    for _ in range(100):
        if not a.is_running("slow"):
            break
        threading.Event().wait(0.01)
    assert a.last_run("slow")["status"] == "ok"


def test_cron_uses_hub_timezone(tmp_path):
    s = Scheduler(hub_database(tmp_path), ZoneInfo("Asia/Kolkata"))
    AppScheduler(s, "demo").cron("daily", lambda: None, "15 7 * * *")
    s.start()
    try:
        nxt = s.jobs()[0]["next_run"]
        assert (nxt.hour, nxt.minute) == (7, 15) and nxt.utcoffset().total_seconds() == 5.5 * 3600
    finally:
        s.shutdown()
