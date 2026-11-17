"""Render an offline, inspectable HTML report from measured JSON artifacts."""
from pathlib import Path
import html
import json

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT/"docs"
def h(value):return html.escape(str(value))

def main():
    report=json.loads((DOCS/"example-dirty-report.json").read_text(encoding="utf-8"))
    benchmark=json.loads((DOCS/"benchmark.json").read_text(encoding="utf-8"))
    job=report["job"];s=job["summary"]
    rows="".join(f'<tr><td>{x["row_number"]}</td><td>{h(x["rule_id"])}</td><td>{h(x["column"])}</td><td><span class="{x["severity"]}">{"错误" if x["severity"]=="error" else "警告"}</span></td><td class="value">{h(x["value"]) if x["value"] else "（空值）"}</td><td>{h(x["message"])}</td></tr>' for x in report["issues"])
    rules="".join(f'<li><strong>{h(x["id"])} {h(x["name"])}</strong><span>{x["failed"]} 命中 / {x["passed"]} 通过 / {x["skipped"]} 跳过</span></li>' for x in job["rule_results"])
    metrics="".join(f'<div class="metric"><small>{label}</small><strong>{value}</strong><span>{caption}</span></div>' for label,value,caption in [
        ("合成订单",s["row_count"],"行数据，不含表头"),("规则命中",s["failed"],f'{s["error_count"]}错误 / {s["warning_count"]}警告'),("问题行",s["bad_rows"],"跨规则去重"),("检查通过率",str(s["score"])+"%","跳过项不进入分母")])
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>QualityGate 合成数据实测报告</title><style>
*{box-sizing:border-box}body{background:#f4f6f0;color:#243c30;font:14px/1.7 "Segoe UI","Microsoft YaHei",sans-serif;margin:0}main{max-width:1120px;margin:auto;padding:38px 24px 70px}.brand{letter-spacing:3px;color:#46745b;font-size:12px;font-weight:700}h1{font-size:32px;line-height:1.4;margin:10px 0}h2{font-size:18px;margin:0 0 15px}.notice{background:#e7eee0;padding:14px 20px;border-radius:10px;color:#5e7453;font-size:12px;margin:22px 0}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin-bottom:25px}.metric{background:white;padding:20px;border:1px solid #e0e7db;border-radius:12px}.metric small,.metric span{display:block;color:#7f8f75;font-size:11px}.metric strong{display:block;font-size:31px;line-height:1.6}.panel{border:1px solid #e0e7db;border-radius:12px;background:white;padding:24px;margin:20px 0}.split{display:grid;grid-template-columns:1.2fr 1fr;gap:20px}.split .panel{margin:0}ul{margin:0;padding:0;list-style:none}li{display:flex;justify-content:space-between;gap:15px;border-bottom:1px solid #edf0e8;padding:9px 0;font-size:12px}li span{color:#8b987f;font-size:11px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:11px 12px;border-bottom:1px solid #eaf0e4;text-align:left}th{color:#84937a;font-weight:400}.scroll{overflow:auto}.value{font-family:Consolas,monospace;overflow-wrap:anywhere;max-width:250px}.error{color:#b5654f;background:#fcf0e9;padding:3px 7px;border-radius:5px}.warning{color:#a48135;background:#fcf6e4;padding:3px 7px;border-radius:5px}.muted{color:#8b987f;font-size:12px}.code{font:11px/1.8 Consolas,monospace;overflow-wrap:anywhere}a{color:#2f7554}@media(max-width:700px){.metrics{grid-template-columns:1fr 1fr}.split{grid-template-columns:1fr}h1{font-size:24px}main{padding:25px 14px}li{display:block}li span{display:block}.panel{padding:18px}}
</style><main><div class="brand">QUALITYGATE / MEASURED DEMO</div><h1>把数据问题，变成可定位的检查结果。</h1><p class="muted">合成订单质量巡检 · 数据、规则和每条命中都可复查</p><div class="notice">这是从实际运行JSON生成的离线静态报告，不连接后台，也不代表浏览器工作台已完成端到端验收。所有订单和邮箱均为合成数据。</div><div class="metrics">METRICS</div><div class="split"><section class="panel"><h2>9条规则，8种检查类型</h2><ul>RULES</ul></section><section class="panel"><h2>可复现的工程验证</h2><p>TESTS项Python自动化测试通过，另有8项前端VM断言。</p><p>2万行、9规则同进程交替对照3轮，检查点节流使后台巡检中位耗时由 <strong>BASELINEms</strong> 降至 <strong>OPTIMIZEDms</strong>；每轮汇总断言一致。</p><p class="muted">本机合成负载，不是生产性能或并发用户压测。</p><p><a href="benchmark.json">查看基准原始记录</a> · <a href="example-dirty-report.json">查看任务完整JSON</a></p><p class="code">数据 SHA256<br>SHA<br>规则指纹<br>RHASH</p></section></div><section class="panel"><h2>全部ISSUECOUNT条保留问题明细</h2><p class="muted">行号按CSV逻辑记录计数，从2开始；同一行可以命中多条规则。此样本未触及明细上限。</p><div class="scroll"><table><thead><tr><th>行号</th><th>规则</th><th>列</th><th>级别</th><th>原始值</th><th>原因</th></tr></thead><tbody>ROWS</tbody></table></div></section></main></html>'''
    page=page.replace('BASELINE',str(benchmark['checkpoint_ab']['unthrottled_median_ms'])).replace('OPTIMIZED',str(benchmark['checkpoint_ab']['throttled_median_ms'])).replace('ISSUECOUNT',str(len(report['issues']))).replace('METRICS',metrics).replace('RULES',rules).replace('TESTS',str(benchmark['test_count'])).replace('<br>SHA<br>', '<br>'+job['dataset_sha256']+'<br>').replace('<br>RHASH</p>','<br>'+job['rules_hash']+'</p>').replace('ROWS',rows)
    (DOCS/'DEMO_REPORT.html').write_text(page,encoding='utf-8')
    print('Generated offline report from actual JSON artifacts')

if __name__=='__main__':main()
