import csv
import hashlib
import io
import json
import uuid
from pathlib import Path

from .config import Config
from .db import Database, dumps, now
from .engine import validate_rules
from .errors import AppError


def digest(value):
    return hashlib.sha256(value).hexdigest()


def require_id(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise AppError("无效的资源 ID")
    return value


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.database)

    def upload(self, raw: bytes, filename: str):
        if len(raw) > self.config.max_bytes:
            raise AppError("文件超过4 MiB限制", 413)
        if not raw or b"\x00" in raw:
            raise AppError("请上传非空 UTF-8 CSV 文本，不支持二进制文件")
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 120:
            raise AppError("文件名必须为1至120个字符")
        name = filename.replace("\\", "/").split("/")[-1].strip()
        if not name:
            raise AppError("文件名无效")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise AppError("CSV 必须为 UTF-8 编码，请重新导出 UTF-8 CSV") from None
        try:
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            header = next(reader)
            columns = [c.strip() for c in header]
            if not 1 <= len(columns) <= self.config.max_columns or any(not c or len(c) > 120 for c in columns) or len(columns) != len(set(columns)):
                raise AppError("表头不能为空或重复，最多80列，每列名不超过120字符")
            rows = []
            for values in reader:
                if len(values) != len(columns):
                    raise AppError(f"第 {len(rows)+2} 条逻辑记录的列数与表头不一致")
                if any(len(v) > 10000 for v in values):
                    raise AppError("单个单元格不能超过10000字符")
                rows.append(dict(zip(columns, values)))
                if len(rows) > self.config.max_rows:
                    raise AppError("超过20000行限制", 413)
            if not rows:
                raise AppError("CSV 至少需要1行数据")
        except (csv.Error, StopIteration) as exc:
            raise AppError(f"CSV 解析失败：{exc}") from None
        sha = digest(raw)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT id FROM datasets WHERE sha256=?", (sha,)).fetchone()
            if existing:
                return {**self.dataset(existing["id"], con=con), "deduplicated": True}
            did = uuid.uuid4().hex
            con.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?)", (did, name, sha, dumps(columns), dumps(rows), len(rows), len(raw), now()))
            return {**self.dataset(did, con=con), "deduplicated": False}

    def dataset(self, did, con=None, full=False):
        require_id(did)
        if con is None:
            with self.db.connect() as opened:
                return self.dataset(did, con=opened, full=full)
        row = con.execute("SELECT * FROM datasets WHERE id=?", (did,)).fetchone()
        if not row:
            raise AppError("数据集不存在", 404)
        item = dict(row)
        item["columns"] = json.loads(item.pop("columns_json"))
        data = json.loads(item.pop("rows_json"))
        item["sample"] = data[:8]
        if full:
            item["rows"] = data
        return item

    def datasets(self):
        with self.db.connect() as con:
            rows = con.execute("SELECT id,name,sha256,columns_json,row_count,byte_count,created_at FROM datasets ORDER BY created_at DESC").fetchall()
            items=[]
            for row in rows:
                item=dict(row)
                item["columns"]=json.loads(item.pop("columns_json"))
                items.append(item)
            return items

    def save_ruleset(self, body, rid=None):
        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise AppError("规则集名称必须为1至80个字符")
        rules = validate_rules(body.get("rules"))
        if rid:
            require_id(rid)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if rid:
                old = con.execute("SELECT version FROM rulesets WHERE id=?", (rid,)).fetchone()
                if not old:
                    raise AppError("规则集不存在", 404)
                if body.get("version") != old["version"]:
                    raise AppError("规则集已更新，请刷新后重试，避免覆盖他人修改", 409)
                con.execute("UPDATE rulesets SET name=?,rules_json=?,version=version+1,updated_at=? WHERE id=?", (name.strip(), dumps(rules), now(), rid))
            else:
                rid = uuid.uuid4().hex
                con.execute("INSERT INTO rulesets VALUES(?,?,?,?,?)", (rid, name.strip(), 1, dumps(rules), now()))
        return self.ruleset(rid)

    def ruleset(self, rid, con=None):
        require_id(rid)
        if con is None:
            with self.db.connect() as opened:
                return self.ruleset(rid, con=opened)
        row = con.execute("SELECT * FROM rulesets WHERE id=?", (rid,)).fetchone()
        if not row:
            raise AppError("规则集不存在", 404)
        item = dict(row)
        item["rules"] = json.loads(item.pop("rules_json"))
        return item

    def rulesets(self):
        with self.db.connect() as con:
            return [self.ruleset(r["id"], con=con) for r in con.execute("SELECT id FROM rulesets ORDER BY updated_at DESC").fetchall()]

    def enqueue(self, body, key=None, parent=None):
        did, rid = require_id(body.get("dataset_id")), require_id(body.get("ruleset_id"))
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 128):
            raise AppError("幂等键长度必须为1至128字符")
        request_hash = digest(dumps({"dataset_id": did, "ruleset_id": rid, "parent": parent}).encode())
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if key:
                existing = con.execute("SELECT id,request_hash FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise AppError("相同幂等键不能用于不同请求", 409)
                    return self.job(existing["id"], con=con)
            if con.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 20:
                raise AppError("待处理队列已满，请稍后重试", 429)
            dataset = self.dataset(did, con=con)
            ruleset = self.ruleset(rid, con=con)
            references = {}
            rules = ruleset["rules"]
            version, rname = ruleset["version"], ruleset["name"]
            if parent:
                original = con.execute("SELECT * FROM jobs WHERE id=?", (parent,)).fetchone()
                if not original or original["state"] not in ("failed", "cancelled", "interrupted"):
                    raise AppError("仅失败、取消或中断任务可以重试", 409)
                rules, references = json.loads(original["rules_json"]), json.loads(original["references_json"])
                version, rname = original["ruleset_version"], original["ruleset_name"]
            for rule in rules:
                needed = [rule["column"]]
                if rule["type"] == "compare_fields":
                    needed.append(rule["params"]["other"])
                if any(c not in dataset["columns"] for c in needed):
                    raise AppError(f"规则 {rule['id']} 引用了当前数据集不存在的列")
                if rule["type"] == "reference" and not parent:
                    ref = self.dataset(rule["params"]["dataset_id"], con=con, full=True)
                    col = rule["params"]["column"]
                    if col not in ref["columns"]:
                        raise AppError("参考数据集的列不存在")
                    references[rule["id"]] = {"dataset_id": ref["id"], "sha256": ref["sha256"], "column": col,
                                                    "values": sorted({row[col].strip() for row in ref["rows"] if row[col].strip()})}
            rhash = digest(dumps({"rules": rules, "references": references}).encode())
            jid = uuid.uuid4().hex
            con.execute("""INSERT INTO jobs(id,dataset_id,ruleset_id,ruleset_name,ruleset_version,rules_json,
                references_json,rules_hash,state,created_at,parent_id,idempotency_key,request_hash)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (jid,did,rid,rname,version,dumps(rules),dumps(references),rhash,"queued",now(),parent,key,request_hash))
            self.db.event(con, jid, "任务已入队；已冻结数据哈希、规则版本与参考数据快照" + ("；重试沿用原快照" if parent else ""))
            return self.job(jid, con=con)

    def job(self, jid, con=None):
        require_id(jid)
        if con is None:
            with self.db.connect() as opened:
                return self.job(jid, con=opened)
        row = con.execute("SELECT j.*,d.name AS dataset_name,d.sha256 AS dataset_sha256,d.row_count FROM jobs j JOIN datasets d ON d.id=j.dataset_id WHERE j.id=?", (jid,)).fetchone()
        if not row:
            raise AppError("任务不存在", 404)
        item = dict(row)
        for column, key in (("summary_json", "summary"),("rule_results_json", "rule_results"),("rules_json", "rules"),("references_json", "references")):
            raw=item.pop(column)
            item[key] = json.loads(raw) if raw else None
        item.pop("request_hash", None)
        item.pop("idempotency_key", None)
        item["events"] = [dict(r) for r in con.execute("SELECT created_at,message FROM events WHERE job_id=? ORDER BY id", (jid,)).fetchall()]
        return item

    def jobs(self, state=None, offset=0, limit=50):
        if state and state not in ("queued","running","succeeded","failed","cancelled","interrupted"):
            raise AppError("未知的任务状态")
        clause, args = ("WHERE j.state=?", [state]) if state else ("", [])
        with self.db.connect() as con:
            total = con.execute(f"SELECT COUNT(*) FROM jobs j {clause}", args).fetchone()[0]
            rows = con.execute(f"""SELECT j.id,j.dataset_id,j.ruleset_name,j.ruleset_version,j.rules_hash,j.state,j.progress,
                j.cancel_requested,j.created_at,j.duration_ms,j.summary_json,d.name AS dataset_name,d.row_count
                FROM jobs j JOIN datasets d ON d.id=j.dataset_id {clause} ORDER BY j.created_at DESC,j.id LIMIT ? OFFSET ?""", [*args,limit,offset]).fetchall()
            items=[]
            for row in rows:
                item=dict(row); item["summary"]=json.loads(item.pop("summary_json")) if row["summary_json"] else None
                items.append(item)
            return {"items": items,"total":total,"offset":offset,"limit":limit}

    def issues(self, jid, severity=None, rule_id=None, search="", offset=0, limit=50):
        self.job(jid)
        clauses, args = ["job_id=?"], [jid]
        if severity:
            if severity not in ("error", "warning"):
                raise AppError("未知的严重级别")
            clauses.append("severity=?"); args.append(severity)
        if rule_id:
            clauses.append("rule_id=?"); args.append(rule_id)
        if search:
            escaped = search.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
            clauses.append("(value LIKE ? ESCAPE '\\' OR message LIKE ? ESCAPE '\\' OR column_name LIKE ? ESCAPE '\\')")
            args.extend([f"%{escaped}%"]*3)
        where = " AND ".join(clauses)
        with self.db.connect() as con:
            total=con.execute(f"SELECT COUNT(*) FROM issues WHERE {where}",args).fetchone()[0]
            items=[dict(r) for r in con.execute(f"SELECT row_number,rule_id,column_name AS column,severity,value,message FROM issues WHERE {where} ORDER BY row_number,id LIMIT ? OFFSET ?",[*args,limit,offset]).fetchall()]
            return {"items":items,"total":total,"offset":offset,"limit":limit}

    def cancel(self,jid):
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job=self.job(jid,con=con)
            if job["state"] not in ("queued","running"):
                raise AppError("任务已结束，不能取消",409)
            if job["state"] == "queued":
                con.execute("UPDATE jobs SET state='cancelled',cancel_requested=1,finished_at=? WHERE id=?",(now(),jid))
                self.db.event(con,jid,"排队任务已取消，未执行规则")
            else:
                con.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?",(jid,))
                self.db.event(con,jid,"已请求取消，等待工作线程检查点")
        return self.job(jid)

    def retry(self,jid):
        job=self.job(jid)
        return self.enqueue({"dataset_id":job["dataset_id"],"ruleset_id":job["ruleset_id"]},parent=jid)

    def recover(self):
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            ids=[r[0] for r in con.execute("SELECT id FROM jobs WHERE state='running'").fetchall()]
            for jid in ids:
                con.execute("UPDATE jobs SET state='interrupted',finished_at=?,error=? WHERE id=?",(now(),"上次进程退出，任务未完成；可按原快照重试",jid))
                self.db.event(con,jid,"启动恢复：运行中任务标记为中断，避免将未完成任务当作成功")
        return len(ids)

    def export_csv(self,jid):
        job=self.job(jid)
        if job["state"] != "succeeded":
            raise AppError("仅已完成任务可以导出",409)
        rows=self.issues(jid,limit=self.config.max_issues)["items"]
        stream=io.StringIO(newline="")
        writer=csv.writer(stream,quoting=csv.QUOTE_ALL)
        writer.writerow(["row_number","rule_id","column","severity","value","message"])
        for row in rows:
            values=[]
            for key in ("row_number","rule_id","column","severity","value","message"):
                value=str(row[key])
                # Human-facing export only. Preserve raw values in JSON, never mutate source data.
                if value.lstrip().startswith(("=","+","-","@","＝","＋","－","＠")) or value.startswith(("\t","\r","\n")):
                    value="'"+value
                values.append(value)
            writer.writerow(values)
        return ("\ufeff"+stream.getvalue()).encode("utf-8")
