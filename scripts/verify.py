"""Reproduce tests and measure deterministic synthetic workloads. No network needed."""
from pathlib import Path
import json
import platform
import subprocess
import sys
import tempfile
import time
import unittest
import statistics
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from qualitygate.config import Config
from qualitygate.demo import seed,csv_bytes
from qualitygate.engine import evaluate
from qualitygate.service import Service
from qualitygate.worker import Worker

def main():
    docs=ROOT/"docs";docs.mkdir(exist_ok=True)
    test_count=unittest.defaultTestLoader.discover(str(ROOT/"tests")).countTestCases()
    tests=subprocess.run([sys.executable,"-m","unittest","discover","-s","tests","-v"],cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace")
    (docs/"test-results.txt").write_text(tests.stdout+tests.stderr,encoding="utf-8")
    if tests.returncode:raise SystemExit("Tests failed; see docs/test-results.txt")
    scratch=ROOT/".test-tmp";scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as td:
        s=Service(Config(Path(td)));d=seed(s);worker=Worker(s);summaries={}
        for name,key in (("dirty","dirty_dataset_id"),("clean","clean_dataset_id")):
            j=s.enqueue({"dataset_id":d[key],"ruleset_id":d["ruleset_id"]});worker.run_one()
            result=s.job(j["id"]);summaries[name]={"summary":result["summary"],"duration_ms":result["duration_ms"]}
            dataset=s.dataset(d[key],full=True)
            (ROOT/"examples").mkdir(exist_ok=True)
            (ROOT/"examples"/f"synthetic_orders_{name}.csv").write_bytes(csv_bytes(dataset["rows"]))
            (docs/f"example-{name}-report.json").write_text(json.dumps({"job":result,"issues":s.issues(j["id"],limit=10000)["items"]},ensure_ascii=False,indent=2),encoding="utf-8")
        base=s.dataset(d["clean_dataset_id"],full=True)["rows"]
        rows=[{**base[i%len(base)],"order_id":f"BENCH-{i:06}"} for i in range(20000)]
        raw=csv_bytes(rows);start=time.perf_counter();ds=s.upload(raw,"synthetic_benchmark_20000.csv");upload_ms=(time.perf_counter()-start)*1000
        j=s.enqueue({"dataset_id":ds["id"],"ruleset_id":d["ruleset_id"]})
        times=[]
        for _ in range(3):
            start=time.perf_counter();evaluate(rows,j["rules"],j["references"]);times.append(round((time.perf_counter()-start)*1000,2))
        worker.run_one();result=s.job(j["id"])
        ab={"unthrottled_ms":[],"throttled_ms":[]}
        for _ in range(3):
            for label,interval in (("unthrottled_ms",0),("throttled_ms",.05)):
                run=s.enqueue({"dataset_id":ds["id"],"ruleset_id":d["ruleset_id"]})
                Worker(s,checkpoint_interval=interval).run_one()
                measured=s.job(run["id"])
                assert measured["summary"]==result["summary"]
                ab[label].append(measured["duration_ms"])
        ab["unthrottled_median_ms"]=statistics.median(ab["unthrottled_ms"])
        ab["throttled_median_ms"]=statistics.median(ab["throttled_ms"])
        ab["median_duration_reduction_pct"]=round((1-ab["throttled_median_ms"]/ab["unthrottled_median_ms"])*100,2)
        report={"generated_at":datetime.now(timezone.utc).isoformat(),"python":platform.python_version(),"platform":platform.platform(),"synthetic":True,"rows":len(rows),"rules":len(j["rules"]),"checks_evaluated":result["summary"]["evaluated"],"checks_skipped":result["summary"]["skipped"],"upload_bytes":len(raw),"upload_ms":round(upload_ms,2),"engine_only_ms_3_runs":times,"worker_end_to_end_ms":result["duration_ms"],"worker_summary":result["summary"],"demo":summaries,"limitations":"Local single-process synthetic workload. Not a production, concurrent-user, or cross-machine benchmark."}
        report["checkpoint_ab"]=ab
        report["test_count"]=test_count
        (docs/"benchmark.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        md=f"""# 实际验证记录

生成时间（UTC）：{report['generated_at']}；Python {report['python']}。

- {test_count}项自动化测试通过，完整日志见 `test-results.txt`。
- 引擎边界、并发幂等入队、规则版本隔离、取消、失败原子性、中断恢复、CSV输入/导出、真实HTTP解析与路由均有测试。
- HTTP测试使用内存传输，不是网络E2E。本机网络连接被宿主沙箱拒绝；内置浏览器访问127.0.0.1超时，因此浏览器交互与视觉验收未完成。前端已通过Node语法检查；不将语法检查等同于页面功能验收。
- 服务CLI已实际启动并生成首个演示报告；未执行Docker与云端CI。

## 合成示例实测

| 数据 | 行数 | 命中 | 问题行 | 检查通过率 |
|---|---:|---:|---:|---:|
| 脏订单 | {summaries['dirty']['summary']['row_count']} | {summaries['dirty']['summary']['failed']} | {summaries['dirty']['summary']['bad_rows']} | {summaries['dirty']['summary']['score']}% |
| 清洁对照 | {summaries['clean']['summary']['row_count']} | {summaries['clean']['summary']['failed']} | {summaries['clean']['summary']['bad_rows']} | {summaries['clean']['summary']['score']}% |

清洁对照为独立构造的合成数据，不能表述为真实业务质量提升或自动修复效果。

## 容量内单次工作负载

20,000行合成订单，9条规则，CSV {len(raw):,} 字节；评估{result['summary']['evaluated']:,}次，跳过{result['summary']['skipped']:,}次。

纯引擎3次运行：{times} ms。单次工作线程（含数据库检查点和结果提交）：{result['duration_ms']} ms。上传解析与落库：{upload_ms:.2f} ms。

同一进程、相同数据和规则，交替运行3次：未节流检查点 {ab['unthrottled_ms']} ms，50ms节流 {ab['throttled_ms']} ms；中位耗时减少 {ab['median_duration_reduction_pct']}%。两组汇总结果逐次断言一致，取消竞争另有回归测试。

前端通过Node语法检查及8项VM渲染/转义断言；VM测试不是浏览器布局/E2E。测试命令为 `node --check qualitygate/static/app.js`、`node scripts/test_frontend.mjs`。

这些是当前机器、当前进程的测量，不是稳定延迟承诺、并发压测或生产吞吐。可执行 `python scripts/verify.py` 重新生成全部记录。
"""
        (docs/"VALIDATION.md").write_text(md,encoding="utf-8")
        print(json.dumps({"tests":f"{test_count} passed","demo":summaries,"benchmark_worker_ms":result["duration_ms"],"checkpoint_ab":ab},ensure_ascii=True))

if __name__=="__main__":main()
