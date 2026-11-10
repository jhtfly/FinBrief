"""后台任务管理 v2.6：并发组并行 + 组内并发上限排队 + 协作式打断 + 持久化记录。

并发组规则（不同组并行；组内同时运行的个数由设置控制，超出排队）：
- fetch 组：fetch（整轮采集，网络重；fetch_concurrency 为组内站点级并发，任务槽恒 1）
- net 组：probe / ai_check（站点扫描类，同时运行 1 个）
- ai 组：screen / compile / translate / backfill / hotspots / ai_add / summarize / search
  （ai_concurrency 由设置控制，默认 2；按大模型 API 限流调低）

打断：运行中的任务调用 state.check() 协作检查取消标志——各业务模块在每个
处理单元（站点/批次/条目）间检查，收到打断即抛 TaskCancelled 并回滚本任务已写的
数据（采集删除本轮入库线索、筛选删判定、翻译/补日期复位字段、编译删草稿），
呈现为"恢复到未执行该任务前的状态"。
"""
import json
import threading
import traceback
import uuid

from app import db

GROUPS = {
    "fetch": "fetch",
    "probe": "net", "ai_check": "net",
    "screen": "ai", "compile": "ai", "translate": "ai",
    "backfill": "ai", "hotspots": "ai", "ai_add": "ai", "summarize": "ai",
    "search": "ai", "multi_compile": "ai", "custom_compile": "ai",
    "supplement": "ai",   # v2.13.3 弃编译线索补材料重编
    "ai_classify": "ai",  # v2.13.4 数据库一键 AI 智能分类
    "ai_search_local": "ai",   # v2.15.0 线索面板 AI 检索改后台任务（原同步等待 10-40s 无感知）
    "rsshub_upgrade": "net",   # v2.21 RSSHub 整包升级（Releases 下载运行包替换，v2.13.4 的 npm dist 链路已失效）
    "rsshub_heal": "net",  # v2.21 路由规则体检：失效 RSSHub 路由自动迁移可用实例
}
GUARANTEE_ONE = {"fetch": 1, "net": 1}   # 组内固定 1 并发（站点级并发在 run_fetch 内部）

_lock = threading.Lock()
_records = {}
_running_ids = []
_queued_ids = []
_fns = {}
_cancel_flags = {}   # id -> threading.Event 打断标志（运行中可打断）


class TaskCancelled(Exception):
    """任务被打断；各模块收到后回滚本轮写入。"""


class TaskHandle:
    """传给任务函数的进度句柄（兼容旧 state.update 用法）。"""

    def __init__(self, rec):
        self._rec = rec

    def update(self, progress: str):
        self._rec["progress"] = str(progress)[:300]
        _db_upsert(self._rec)

    @property
    def cancelled(self) -> bool:
        ev = _cancel_flags.get(self._rec["id"])
        return bool(ev and ev.is_set())

    def check(self):
        """协作检查点：被打断时抛 TaskCancelled（在处理单元间调用）。"""
        if self.cancelled:
            raise TaskCancelled("任务已被用户打断")


def _db_upsert(rec):
    try:
        with db.get_conn() as conn:
            conn.execute(
                """INSERT INTO task_runs(id, stage, label, status, progress, result,
                                         error, created_at, started_at, finished_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     status=excluded.status, progress=excluded.progress,
                     result=excluded.result, error=excluded.error,
                     started_at=excluded.started_at, finished_at=excluded.finished_at""",
                (rec["id"], rec["stage"], rec.get("label", ""), rec["status"],
                 (rec.get("progress") or "")[:300], _jsonable(rec.get("result")),
                 rec.get("error"), rec["created_at"],
                 rec.get("started_at"), rec.get("finished_at")))
    except Exception:
        pass


def _jsonable(v):
    if v is None:
        return ""
    try:
        return json.dumps(v, ensure_ascii=False)[:500]
    except (TypeError, ValueError):
        return str(v)[:500]


def _view(rec):
    return {k: rec.get(k) for k in
            ("id", "stage", "group", "label", "status", "progress",
             "created_at", "started_at")}


def _group_limit(grp) -> int:
    if grp in GUARANTEE_ONE:
        return 1
    try:
        n = int(db.get_setting("ai_concurrency", "2"))
    except (TypeError, ValueError):
        n = 2
    return max(1, min(8, n))


def _group_running(grp) -> int:
    return sum(1 for t in _running_ids if _records[t]["group"] == grp)


def _group_queued(grp) -> int:
    return sum(1 for t in _queued_ids if _records[t]["group"] == grp)


def _start_locked(rec):
    fn, args = _fns.pop(rec["id"], (None, None))
    if fn is None:
        return
    rec["status"] = "running"
    rec["progress"] = "启动中…"
    rec["started_at"] = db.now_iso()
    _running_ids.append(rec["id"])
    _db_upsert(rec)
    ev = threading.Event()
    _cancel_flags[rec["id"]] = ev

    def wrapper():
        try:
            result = fn(TaskHandle(rec), *args)
            rec["result"] = result
            # v2.9.2/3：业务结果语义化——
            # - ok= False 或 0（全失败，如补全日期 0/16）→ error，原因进概况；
            # - 有 total 且 0<ok<total 且任务自报口径 → 概况"成功 X/Y"；
            # - 其余照旧 done
            if isinstance(result, dict):
                okv = result.get("ok")
                tot = result.get("total")
                if okv is False or okv == 0:
                    reason = (result.get("reason") or result.get("error")
                              or f"成功 0" + (f"（共 {tot}）" if tot else ""))
                    rec["status"] = "error"
                    rec["error"] = reason          # v2.15.0 失败 reason 同步 error 列（此前仅 progress，toast 只显示"失败："空文案）
                    rec["progress"] = f"失败：{reason}"[:280]
                    return
                if (isinstance(okv, int) and "fail" not in result
                        and isinstance(tot, int) and tot > 0 and okv < tot):
                    rec["status"] = "done"
                    rec["progress"] = f"完成（成功 {okv}/{tot}）"
                    return
            rec["status"] = "done"
            rec["progress"] = "完成"
        except TaskCancelled:
            rec["status"] = "cancelled"
            rec["progress"] = "已打断（本轮写入已回滚）"
        except Exception as e:
            traceback.print_exc()
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["status"] = "error"
            rec["progress"] = f"失败: {rec['error']}"[:300]
            db.log("system", f"{rec['stage']} 任务异常: {rec['error']}", "error")
        finally:
            with _lock:
                rec["finished_at"] = db.now_iso()
                _db_upsert(rec)
                if rec["id"] in _running_ids:
                    _running_ids.remove(rec["id"])
                _cancel_flags.pop(rec["id"], None)
                _cleanup_locked()
                _pump_locked(rec["group"])

    threading.Thread(target=wrapper, daemon=True).start()


def _pump_locked(grp):
    """组内出队排队任务，直至达到该组并发上限。"""
    while _group_running(grp) < _group_limit(grp):
        nxt = None
        for tid in list(_queued_ids):
            rec = _records.get(tid)
            if rec and rec["group"] == grp:
                nxt = tid
                break
        if not nxt:
            break
        _queued_ids.remove(nxt)
        _start_locked(_records[nxt])


def _cleanup_locked():
    finished = [tid for tid, r in _records.items()
                if r["status"] in ("done", "error", "cancelled")]
    for tid in finished[50:]:
        _records.pop(tid, None)


def _submit(stage, fn, args, label=""):
    grp = GROUPS.get(stage, "ai")
    tid = uuid.uuid4().hex[:12]
    rec = {"id": tid, "stage": stage, "group": grp, "label": label or "",
           "status": "queued", "progress": "排队中…", "result": None,
           "error": None, "created_at": db.now_iso(),
           "started_at": None, "finished_at": None}
    with _lock:
        _records[tid] = rec
        _fns[tid] = (fn, args)
        _queued_ids.append(tid)
        _db_upsert(rec)
        _pump_locked(grp)
        if rec["status"] == "running":
            return {"status": "started", "id": tid, "position": 0}
        pos = _queued_ids.index(tid) + 1
        return {"status": "queued", "id": tid, "position": pos}


def run_in_background(stage, fn, *args, label=""):
    """提交后台任务：有空槽立即启动，同组满员排队。返回 (ok, msg)。"""
    info = _submit(stage, fn, args, label=label)
    if info["status"] == "started":
        return True, "已启动"
    return True, f"同类任务并发已满，已自动排队（第 {info['position']} 位）"


def group_busy(stage_or_grp: str) -> int:
    """某组（或某 stage）当前 running+queued 的任务数（调度防重复提交用）。"""
    with _lock:
        grp = GROUPS.get(stage_or_grp, stage_or_grp)
        n = sum(1 for t in _running_ids if _records[t]["group"] == grp)
        n += sum(1 for t in _queued_ids if _records[t]["group"] == grp)
        return n


def clear_queue(grp: str | None = None) -> int:
    """清空排队（可限定并发组）。返回清除数。"""
    n = 0
    with _lock:
        for tid in list(_queued_ids):
            rec = _records.get(tid)
            if not rec:
                continue
            if grp and rec["group"] != grp:
                continue
            _queued_ids.remove(tid)
            _fns.pop(tid, None)
            rec["status"] = "cancelled"
            rec["progress"] = "已取消（清空排队）"
            rec["finished_at"] = db.now_iso()
            _db_upsert(rec)
            n += 1
    return n


def task_snapshot():
    with _lock:
        running = [_view(_records[t]) for t in _running_ids]
        queued = [_view(_records[t]) for t in _queued_ids]
    return {"running": running, "queued": queued,
            "history": db.list_task_runs(100)}


def interrupt_task(tid) -> bool:
    """请求打断运行中的任务（协作式，由业务模块在检查点响应）。"""
    ev = _cancel_flags.get(tid)
    if ev:
        ev.set()
        return True
    return False


def cancel_task(tid) -> bool:
    """移除排队任务。"""
    with _lock:
        rec = _records.get(tid)
        if not rec or rec["status"] != "queued":
            return False
        if tid in _queued_ids:
            _queued_ids.remove(tid)
        _fns.pop(tid, None)
        rec["status"] = "cancelled"
        rec["progress"] = "已取消"
        rec["finished_at"] = db.now_iso()
        _db_upsert(rec)
        return True


def _register_cancel(tid):
    with _lock:
        _cancel_flags[tid] = threading.Event()
