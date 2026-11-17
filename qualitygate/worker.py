import logging
import threading
import time

from .db import dumps, now
from .engine import Cancelled, evaluate

log=logging.getLogger(__name__)


class Worker:
    """Single worker, durable SQLite queue, atomic claims and result publication."""
    def __init__(self,service,checkpoint_interval=.05):
        self.service=service
        self.checkpoint_interval=checkpoint_interval
        self.stop_event=threading.Event()
        self.thread=None

    def start(self):
        self.service.recover()
        self.thread=threading.Thread(target=self.loop,name="qualitygate-worker",daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def claim(self):
        with self.service.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row=con.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                return None
            jid=row["id"]
            con.execute("UPDATE jobs SET state='running',started_at=? WHERE id=?",(now(),jid))
            self.service.db.event(con,jid,"工作线程已领取任务，开始规则评估")
            return jid

    def run_one(self):
        jid=self.claim()
        if not jid:
            return False
        started=time.perf_counter()
        try:
            job=self.service.job(jid)
            dataset=self.service.dataset(job["dataset_id"],full=True)
            last_progress=[-1]
            next_poll=[0.0]
            def checkpoint(progress):
                if self.stop_event.is_set():
                    raise InterruptedError("服务正在关闭，任务可在重启后重试")
                clock=time.monotonic()
                # Keep inexpensive engine checkpoints, throttle database I/O.
                # Always poll at the end; the commit transaction checks again.
                if progress != 100 and clock < next_poll[0]:
                    return False
                next_poll[0]=clock+self.checkpoint_interval
                with self.service.db.connect() as con:
                    row=con.execute("SELECT cancel_requested FROM jobs WHERE id=?",(jid,)).fetchone()
                    if progress != last_progress[0]:
                        con.execute("UPDATE jobs SET progress=? WHERE id=?",(min(progress,99),jid))
                        last_progress[0]=progress
                    return bool(row[0])
            result=evaluate(dataset["rows"],job["rules"],job["references"],self.service.config.max_issues,checkpoint)
            with self.service.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                if con.execute("SELECT cancel_requested FROM jobs WHERE id=?",(jid,)).fetchone()[0]:
                    raise Cancelled()
                con.executemany("INSERT INTO issues(job_id,row_number,rule_id,column_name,severity,value,message) VALUES(?,?,?,?,?,?,?)",[(jid,x["row_number"],x["rule_id"],x["column"],x["severity"],x["value"],x["message"]) for x in result["issues"]])
                elapsed=round((time.perf_counter()-started)*1000,2)
                con.execute("UPDATE jobs SET state='succeeded',progress=100,finished_at=?,duration_ms=?,summary_json=?,rule_results_json=? WHERE id=?",(now(),elapsed,dumps(result["summary"]),dumps(result["rules"]),jid))
                self.service.db.event(con,jid,f"评估完成：{result['summary']['failed']} 条规则命中，{result['summary']['bad_rows']} 个问题行；结果原子提交")
        except (Cancelled,InterruptedError) as exc:
            state="cancelled" if isinstance(exc,Cancelled) else "interrupted"
            with self.service.db.connect() as con:
                con.execute("UPDATE jobs SET state=?,finished_at=?,duration_ms=?,error=? WHERE id=?",(state,now(),round((time.perf_counter()-started)*1000,2),str(exc) or "用户取消任务",jid))
                self.service.db.event(con,jid,"任务已取消，未发布部分结果" if state=="cancelled" else "服务关闭，任务已标记为中断")
        except Exception:
            log.exception("Job %s failed",jid)
            with self.service.db.connect() as con:
                con.execute("UPDATE jobs SET state='failed',finished_at=?,error=? WHERE id=?",(now(),"任务执行失败，请检查服务日志；可按原快照重试",jid))
                self.service.db.event(con,jid,"任务失败，未发布部分结果")
        return True

    def loop(self):
        while not self.stop_event.is_set():
            try:
                if not self.run_one():
                    self.stop_event.wait(.25)
            except Exception:
                log.exception("Worker loop failed")
                self.stop_event.wait(.5)
