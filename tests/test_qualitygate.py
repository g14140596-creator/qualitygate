import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from qualitygate.config import Config
from qualitygate.db import now
from qualitygate.demo import seed
from qualitygate.engine import Cancelled, evaluate, validate_rules
from qualitygate.errors import AppError
from qualitygate.server import make_server
from qualitygate.service import Service
from qualitygate.worker import Worker

SCRATCH=Path(__file__).resolve().parents[1]/".test-tmp"
SCRATCH.mkdir(exist_ok=True)

def rule(kind,column="x",severity="error",params=None,rid="R01"):
    return validate_rules([{"id":rid,"type":kind,"column":column,"severity":severity,"params":params or {}}])[0]


class EngineTests(unittest.TestCase):
    def check(self,kind,values,params=None):
        return evaluate([{"x":x} for x in values],[rule(kind,params=params)],{})

    def test_required_whitespace(self):
        s=self.check("required",["", "  ","0"])["summary"]
        self.assertEqual((s["failed"],s["passed"],s["skipped"]),(2,1,0))

    def test_unique_flags_all_duplicates_and_skips_empty(self):
        r=self.check("unique",["a"," a ","","b"])
        self.assertEqual([x["row_number"] for x in r["issues"]],[2,3])
        self.assertEqual(r["summary"]["skipped"],1)

    def test_numeric_decimal_boundaries_and_non_finite(self):
        s=self.check("numeric_range",["0.1","0.3","0.30000000000000000001","NaN","Infinity","oops",""],{"min":"0.1","max":"0.3"})["summary"]
        self.assertEqual((s["passed"],s["failed"],s["skipped"]),(2,4,1))

    def test_enum_is_case_sensitive_after_trimming(self):
        self.assertEqual(self.check("enum",[" paid ","PAID"],{"values":["paid"]})["summary"]["failed"],1)

    def test_strict_calendar_date(self):
        self.assertEqual(self.check("date",["2024-02-29","2026-02-29","2026-1-01","20260101"])["summary"]["failed"],3)

    def test_email_syntax_only(self):
        self.assertEqual(self.check("email",["a@example.com","a@host","a@@a.com","a b@c.com",""])["summary"]["failed"],3)

    def test_compare_dates_and_invalid_counterpart(self):
        rows=[{"x":"2026-01-02","y":"2026-01-01"},{"x":"2025-01-01","y":"2026-01-01"},{"x":"","y":"x"},{"x":"2026-01-01","y":"broken"}]
        s=evaluate(rows,[rule("compare_fields",params={"other":"y","mode":"date","op":"gte"})],{})["summary"]
        self.assertEqual((s["passed"],s["failed"],s["skipped"]),(1,2,1))

    def test_numeric_comparison_all_operators(self):
        for op,passed in [("gt",False),("gte",True),("eq",True),("lte",True),("lt",False)]:
            s=evaluate([{"x":"2.00","y":"2"}],[rule("compare_fields",params={"other":"y","op":op,"mode":"number"})],{})["summary"]
            self.assertEqual(s["passed"],int(passed),op)

    def test_reference_uses_frozen_values(self):
        r=rule("reference",params={"dataset_id":"a"*32,"column":"code"})
        s=evaluate([{"x":" CN "},{"x":"XX"}],[r],{"R01":{"values":["CN"]}})["summary"]
        self.assertEqual(s["failed"],1)

    def test_counts_not_row_count_and_details_bounded(self):
        rules=[rule("required",rid="a"),rule("enum",params={"values":["yes"]},rid="b")]
        s=evaluate([{"x":"no"},{"x":"no"}],rules,{},max_issues=1)["summary"]
        self.assertEqual((s["failed"],s["bad_rows"],s["stored_issues"]),(2,2,1))
        self.assertTrue(s["truncated"])
        self.assertEqual(s["score"],50)

    def test_skipped_not_in_denominator_and_null_when_all_skip(self):
        s=self.check("unique",["",""])["summary"]
        self.assertEqual(s["evaluated"],0); self.assertIsNone(s["score"])

    def test_warning_gate(self):
        self.assertEqual(evaluate([{"x":"bad"}],[rule("email",severity="warning")],{})["summary"]["gate"],"review")

    def test_cancellation_checkpoint(self):
        with self.assertRaises(Cancelled):
            evaluate([{"x":"a"}],[rule("required")],{},checkpoint=lambda _:True)

    def test_rule_validation_rejects_invalid_inputs(self):
        for rules in ([],[{}],[{"type":"numeric_range","column":"x","params":{"min":3,"max":2}}],[{"type":"numeric_range","column":"x","params":{"min":"NaN"}}],[{"type":"enum","column":"x","params":{"values":[1]}}],[{"id":"same","type":"date","column":"x"}]*2):
            with self.assertRaises(AppError): validate_rules(rules)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=SCRATCH)
        self.s=Service(Config(Path(self.tmp.name)))
        self.ds=self.s.upload(b"x,y\na,1\na,2\n", "demo.csv")
        self.rs=self.s.save_ruleset({"name":"unique","rules":[rule("unique")]})

    def tearDown(self): self.tmp.cleanup()
    def enqueue(self,key=None): return self.s.enqueue({"dataset_id":self.ds["id"],"ruleset_id":self.rs["id"]},key)

    def test_upload_dedup_and_immutable_source(self):
        again=self.s.upload(b"x,y\na,1\na,2\n","other.csv")
        self.assertEqual(again["id"],self.ds["id"]);self.assertTrue(again["deduplicated"])

    def test_upload_bom_unicode_quotes_and_multiline(self):
        ds=self.s.upload('\ufeff名字,内容\r\n小黄,"line1\nline2"\r\n'.encode(),"中文.csv")
        self.assertEqual(ds["row_count"],1);self.assertEqual(ds["sample"][0]["内容"],"line1\nline2")

    def test_upload_invalid_shape_encoding_binary_empty(self):
        for raw in (b"",b"a,a\n1,2\n",b"a,b\n1\n",b"a\n",b"a\n\xff",b"a\n\x00",b'a\n"unterminated'):
            with self.assertRaises(AppError):self.s.upload(raw,"bad.csv")

    def test_upload_limits(self):
        limited=Service(Config(Path(self.tmp.name)/"limits",max_rows=1,max_bytes=100))
        with self.assertRaises(AppError):limited.upload(b"x\n1\n2\n","large.csv")
        with self.assertRaises(AppError):limited.upload(b"x"*101,"large.csv")

    def test_rule_optimistic_version_conflict(self):
        changed=self.s.save_ruleset({"name":"new","version":1,"rules":[rule("required")]},self.rs["id"])
        self.assertEqual(changed["version"],2)
        with self.assertRaises(AppError) as ctx:self.s.save_ruleset({"name":"stale","version":1,"rules":[rule("required")]},self.rs["id"])
        self.assertEqual(ctx.exception.status,409)

    def test_snapshot_survives_edit(self):
        j=self.enqueue()
        self.s.save_ruleset({"name":"new","version":1,"rules":[rule("required")]},self.rs["id"])
        Worker(self.s).run_one()
        result=self.s.job(j["id"])
        self.assertEqual(result["ruleset_version"],1);self.assertEqual(result["summary"]["failed"],2)
        self.assertEqual(result["rules_hash"],j["rules_hash"])

    def test_idempotency_same_request_and_conflict(self):
        j=self.enqueue("key")
        self.assertEqual(self.enqueue("key")["id"],j["id"])
        other=self.s.upload(b"x\nb\n","other.csv")
        with self.assertRaises(AppError) as ctx:self.s.enqueue({"dataset_id":other["id"],"ruleset_id":self.rs["id"]},"key")
        self.assertEqual(ctx.exception.status,409)

    def test_concurrent_idempotent_submission(self):
        ids=[];errors=[]
        def submit():
            try:ids.append(self.enqueue("concurrent")["id"])
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=submit) for _ in range(5)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertFalse(errors);self.assertEqual(len(set(ids)),1)

    def test_missing_column_rejected_before_enqueue(self):
        rs=self.s.save_ruleset({"name":"missing","rules":[rule("required",column="none")]})
        with self.assertRaises(AppError):self.s.enqueue({"dataset_id":self.ds["id"],"ruleset_id":rs["id"]})
        self.assertEqual(self.s.jobs()["total"],0)

    def test_worker_results_pagination_filter_and_restart_persistence(self):
        j=self.enqueue();Worker(self.s).run_one()
        self.assertEqual(self.s.job(j["id"])["state"],"succeeded")
        self.assertEqual(self.s.issues(j["id"],limit=1,offset=1)["items"][0]["row_number"],3)
        self.assertEqual(self.s.issues(j["id"],search="a")["total"],2)
        self.assertEqual(self.s.issues(j["id"],severity="warning")["total"],0)
        reopened=Service(self.s.config)
        self.assertEqual(reopened.job(j["id"])["summary"]["bad_rows"],2)

    def test_queued_cancel_and_retry_uses_original_rules(self):
        j=self.enqueue();self.s.cancel(j["id"])
        self.s.save_ruleset({"name":"new","version":1,"rules":[rule("required")]},self.rs["id"])
        retry=self.s.retry(j["id"])
        self.assertEqual(retry["rules_hash"],j["rules_hash"])
        Worker(self.s).run_one();self.assertEqual(self.s.job(retry["id"])["summary"]["failed"],2)

    def test_running_cancel_does_not_publish_partial_results(self):
        j=self.enqueue()
        original=evaluate
        def cancel_then_eval(*args,**kwargs):
            self.s.cancel(j["id"])
            return original(*args,**kwargs)
        with patch("qualitygate.worker.evaluate",side_effect=cancel_then_eval):Worker(self.s).run_one()
        self.assertEqual(self.s.job(j["id"])["state"],"cancelled")
        self.assertEqual(self.s.issues(j["id"])["total"],0)

    def test_process_recovery_preserves_queued_and_marks_running(self):
        first=self.enqueue();second=self.enqueue()
        Worker(self.s).claim()
        self.assertEqual(self.s.recover(),1)
        self.assertEqual(self.s.job(first["id"])["state"],"interrupted")
        self.assertEqual(self.s.job(second["id"])["state"],"queued")

    def test_unexpected_failure_is_atomic(self):
        j=self.enqueue()
        with patch("qualitygate.worker.evaluate",side_effect=RuntimeError("injected failure")):
            with self.assertLogs("qualitygate.worker",level="ERROR"):Worker(self.s).run_one()
        self.assertEqual(self.s.job(j["id"])["state"],"failed")
        self.assertIsNone(self.s.job(j["id"])["summary"])
        self.assertEqual(self.s.issues(j["id"])["total"],0)

    def test_csv_export_formula_prefix_and_raw_json_value(self):
        ds=self.s.upload(b'x\n"=SUM(1,2)"\n',"formula.csv")
        rs=self.s.save_ruleset({"name":"enum","rules":[rule("enum",params={"values":["ok"]})]})
        j=self.s.enqueue({"dataset_id":ds["id"],"ruleset_id":rs["id"]});Worker(self.s).run_one()
        exported=list(csv.DictReader(io.StringIO(self.s.export_csv(j["id"]).decode("utf-8-sig"))))
        self.assertTrue(exported[0]["value"].startswith("'="))
        self.assertEqual(self.s.issues(j["id"])["items"][0]["value"],"=SUM(1,2)")

    def test_demo_actual_dirty_and_clean_outcomes(self):
        demo=seed(self.s)
        def run(did):
            j=self.s.enqueue({"dataset_id":did,"ruleset_id":demo["ruleset_id"]});Worker(self.s).run_one();return self.s.job(j["id"])
        dirty,clean=run(demo["dirty_dataset_id"]),run(demo["clean_dataset_id"])
        self.assertGreater(dirty["summary"]["failed"],0)
        self.assertEqual(clean["summary"]["failed"],0)
        self.assertEqual(dirty["rules_hash"],clean["rules_hash"])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(dir=SCRATCH);cls.s=Service(Config(Path(cls.tmp.name),port=0))
        cls.server=make_server(cls.s);cls.port=cls.server.server_address[1]
    @classmethod
    def tearDownClass(cls):cls.server.server_close();cls.tmp.cleanup()
    def request(self,path,body=None,headers=None):
        # Exercise the real HTTP parser + handler using an in-memory transport.
        # No loopback permission is needed; browser E2E is a separate check.
        class MemorySocket:
            def __init__(self,raw):self.input=io.BytesIO(raw);self.output=bytearray()
            def makefile(self,*args,**kwargs):return self.input
            def sendall(self,data):self.output.extend(data)
            def settimeout(self,value):pass
        fields={"Host":f"127.0.0.1:{self.port}","Connection":"close",**(headers or {})}
        if body is not None:fields["Content-Length"]=str(len(body))
        head=f"{'POST' if body is not None else 'GET'} {path} HTTP/1.1\r\n"+"".join(f"{key}: {value}\r\n" for key,value in fields.items())+"\r\n"
        sock=MemorySocket(head.encode()+ (body or b""))
        self.server.RequestHandlerClass(sock,("127.0.0.1",12345),self.server)
        head,raw=bytes(sock.output).split(b"\r\n\r\n",1)
        lines=head.decode().split("\r\n")
        return int(lines[0].split()[1]),raw,dict(line.split(": ",1) for line in lines[1:])

    def test_health_and_static_csp(self):
        status,raw,headers=self.request("/")
        self.assertEqual(status,200);self.assertIn(b"QualityGate",raw);self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])
        self.assertEqual(self.request("/api/health")[0],200)
    def test_foreign_origin_and_dns_rebinding_host_rejected(self):
        self.assertEqual(self.request("/api/datasets",headers={"Origin":"https://evil.example"})[0],403)
        self.assertEqual(self.request("/api/datasets",headers={"Host":"evil.example"})[0],403)
    def test_invalid_json_content_type_and_unknown_path(self):
        self.assertEqual(self.request("/api/rulesets",b"{",{"Content-Type":"application/json"})[0],400)
        self.assertEqual(self.request("/api/rulesets",b"{}",{"Content-Type":"text/plain"})[0],415)
        self.assertEqual(self.request("/not-found")[0],404)
    def test_negative_pagination_rejected(self):
        self.assertEqual(self.request("/api/jobs?offset=-1")[0],400)
    def test_http_full_upload_rules_submit_result_export(self):
        status,raw,_=self.request("/api/datasets?filename=http.csv",b"x\na\na\n",{"Content-Type":"text/csv"})
        self.assertEqual(status,201);ds=json.loads(raw)
        status,raw,_=self.request("/api/rulesets",json.dumps({"name":"HTTP","rules":[rule("unique")]}).encode(),{"Content-Type":"application/json"})
        self.assertEqual(status,201);rs=json.loads(raw)
        status,raw,_=self.request("/api/jobs",json.dumps({"dataset_id":ds["id"],"ruleset_id":rs["id"]}).encode(),{"Content-Type":"application/json","Idempotency-Key":"http-test"})
        self.assertEqual(status,202);jid=json.loads(raw)["id"]
        Worker(self.s).run_one()
        status,raw,_=self.request(f"/api/jobs/{jid}/export?format=json")
        self.assertEqual(status,200);self.assertEqual(json.loads(raw)["job"]["summary"]["failed"],2)


if __name__=="__main__":unittest.main(verbosity=2)
