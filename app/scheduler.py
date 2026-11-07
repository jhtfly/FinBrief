"""自动检索调度：每 60 秒 tick 一次，按设置支持"每日定时"与"间隔分钟"两种模式。"""
import threading
import time

from app import db

_started = False
_lock = threading.Lock()


def start_scheduler():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(daemon=True)
        sched.add_job(_tick, "interval", seconds=60, id="auto_fetch_tick")
        sched.start()
        db.log("system", "自动检索调度器已启动（每分钟检查一次设置）")
    except Exception as e:
        db.log("system", f"调度器启动失败: {e}", "warn")


def _tick():
    try:
        s = db.all_settings()
        mode = s.get("auto_fetch_mode", "off")
        if mode == "off":
            return
        if mode == "daily":
            t = s.get("auto_fetch_time", "08:00")
            try:
                hh, mm = (t.split(":") + ["0", "0"])[:2]
                hh, mm = int(hh), int(mm)
            except ValueError:
                return
            now = time.localtime()
            if (now.tm_hour, now.tm_min) == (hh, mm):
                today = time.strftime("%Y%m%d")
                if db.get_setting("last_auto_day") != today:
                    db.set_setting("last_auto_day", today)
                    _run_auto("每日定时检索")
        elif mode == "interval":
            try:
                minutes = max(5, int(s.get("auto_fetch_interval_min", "30")))
            except ValueError:
                return
            last = db.last_fetch_run()
            due = True
            if last and last.get("started_at"):
                try:
                    started = time.mktime(time.strptime(
                        last["started_at"], "%Y-%m-%dT%H:%M:%S"))
                    due = time.time() - started >= minutes * 60
                except ValueError:
                    due = True
            if due:
                _run_auto(f"间隔检索（每{minutes}分钟）")
    except Exception as e:
        db.log("fetch", f"自动检索调度异常: {e}", "warn")


def _run_auto(reason: str):
    from app.fetcher import runner
    from app.tasks import run_in_background

    def _job(state):
        return runner.run_fetch(progress_cb=lambda *a: None)

    db.log("fetch", f"自动检索已提交（{reason}）")
    run_in_background("fetch", _job, label=reason)
