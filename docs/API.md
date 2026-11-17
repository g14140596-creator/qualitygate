# REST API

本机地址`http://127.0.0.1:8765`。除CSV上传外，POST请求使用`Content-Type: application/json`。成功返回JSON，失败返回`{"error":"说明"}`。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 版本、服务模式及工作线程状态 |
| GET / POST | `/api/datasets` | 列表 / 原始CSV上传，文件名用`?filename=...` |
| GET | `/api/datasets/{id}` | 元数据和前8行预览 |
| GET / POST | `/api/rulesets` | 规则集列表 / 创建 |
| GET / POST | `/api/rulesets/{id}` | 读取 / 带version乐观锁更新 |
| GET / POST | `/api/jobs` | 列表 / 创建任务（202） |
| GET | `/api/jobs/{id}` | 规则快照、汇总、事件、状态 |
| GET | `/api/jobs/{id}/issues` | 保留明细分页、筛选 |
| POST | `/api/jobs/{id}/cancel` | 取消排队或执行中任务，body为`{}` |
| POST | `/api/jobs/{id}/retry` | 创建沿用原快照的子任务，body为`{}` |
| GET | `/api/jobs/{id}/export?format=csv或json` | 导出完成任务 |
| POST | `/api/demo` | 幂等导入合成数据，body为`{}` |

创建规则集：

```json
{"name":"金额检查","rules":[
  {"id":"R01","name":"金额必填","type":"required","column":"amount","severity":"error","params":{}},
  {"id":"R02","name":"金额范围","type":"numeric_range","column":"amount","severity":"error","params":{"min":"0","max":"100000"}}
]}
```

规则参数：

| type | params |
|---|---|
| required / unique / date / email | `{}` |
| numeric_range | min / max至少设置一个，边界包含 |
| enum | `{"values":["paid","pending"]}` |
| compare_fields | `{"other":"created_at","mode":"date","op":"gte"}`，模式number/date，操作gt/gte/eq/lt/lte |
| reference | `{"dataset_id":"参考数据集ID","column":"code"}` |

创建任务请求：`{"dataset_id":"...","ruleset_id":"..."}`。可提供请求头`Idempotency-Key`，防止同键重复入队。重试接口创建新子任务，未提供幂等键保障。

任务列表支持`state`、`offset`、`limit`（1—100）。明细支持`severity`、`rule_id`、`search`、`offset`、`limit`（1—100）。`search`为字面量子串搜索，SQL通配符经过转义。

常见状态码：400校验失败；403非本机Host或跨来源；404资源不存在；409版本/状态/幂等冲突；413大小超限；415请求类型错误；429排队任务达到20个；500内部错误。
