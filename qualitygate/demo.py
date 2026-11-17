"""Deterministic synthetic examples: no real orders, customers or business metrics."""
import csv
import io


def csv_bytes(rows):
    stream=io.StringIO(newline="")
    writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def seed(service):
    reference=service.upload(b"code,name\nCN,China\nUS,United States\nDE,Germany\n", "synthetic_countries.csv")
    clean=[]
    for i in range(240):
        day=f"2026-10-{i%28+1:02}"
        clean.append({"order_id":f"ORD-{i+1:05}","customer_email":f"customer{i+1}@example.com","amount":f"{12.5+i*3.7:.2f}","status":"paid" if i%3 else "pending","created_at":day,"paid_at":day if i%3 else "","country_code":("CN","US","DE")[i%3]})
    dirty=[dict(row) for row in clean]
    for i in range(0,240,31): dirty[i]["order_id"]=""
    for i in range(5,240,47): dirty[i]["order_id"]=dirty[1]["order_id"]
    for i in range(7,240,23): dirty[i]["customer_email"]="invalid-email"
    for i in range(10,240,41): dirty[i]["customer_email"]=""
    for i in range(13,240,29): dirty[i]["amount"]="-9.99"
    dirty[55]["amount"]="NaN"
    for i in range(17,240,37): dirty[i]["status"]="unknown"
    for i in range(19,240,43): dirty[i]["created_at"]="2026-02-30"
    for i in range(22,240,53): dirty[i]["paid_at"]="2025-01-01"
    for i in range(25,240,59): dirty[i]["country_code"]="XX"
    did=service.upload(csv_bytes(dirty),"synthetic_orders_dirty.csv")["id"]
    cid=service.upload(csv_bytes(clean),"synthetic_orders_clean.csv")["id"]
    rules=[
        {"id":"R01","name":"订单号不能为空","type":"required","column":"order_id"},
        {"id":"R02","name":"订单号不能重复","type":"unique","column":"order_id"},
        {"id":"R03","name":"客户邮箱不能为空","type":"required","column":"customer_email"},
        {"id":"R04","name":"邮箱基础格式","type":"email","column":"customer_email","severity":"warning"},
        {"id":"R05","name":"订单金额合理范围","type":"numeric_range","column":"amount","params":{"min":0,"max":100000}},
        {"id":"R06","name":"订单状态允许值","type":"enum","column":"status","params":{"values":["pending","paid","refunded"]}},
        {"id":"R07","name":"创建日期格式","type":"date","column":"created_at"},
        {"id":"R08","name":"付款日期不早于创建日期","type":"compare_fields","column":"paid_at","params":{"other":"created_at","mode":"date","op":"gte"}},
        {"id":"R09","name":"国家编码存在于参考表","type":"reference","column":"country_code","params":{"dataset_id":reference["id"],"column":"code"}},
    ]
    sets=service.rulesets()
    existing=next((x for x in sets if x["name"]=="合成订单 · 基础质量规则"),None)
    ruleset=existing or service.save_ruleset({"name":"合成订单 · 基础质量规则","rules":rules})
    return {"dirty_dataset_id":did,"clean_dataset_id":cid,"reference_dataset_id":reference["id"],"ruleset_id":ruleset["id"]}
