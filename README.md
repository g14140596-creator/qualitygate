# QualityGate｜数据质量巡检与任务管理平台

一个完整的本地 Python 工程项目：上传 CSV → 定义规则 → 后台巡检 → 定位问题 → 对比与导出。面向 Python 开发、数据工程与测试开发方向的个人作品。

**实现方式：Python 标准库 HTTP 服务 + SQLite + 原生 JavaScript/CSS。运行不依赖 pip、Node、云服务或 API 密钥。** 本项目由 Codex 辅助构建，尚需作者自行运行、理解和持续维护；不包装为企业实习、生产系统或真实客户项目。

## 启动

需要 Python 3.11 或以上（开发环境实际验证为 Python 3.14）。在 **Windows文件资源管理器** 中双击 `启动项目.cmd`，保持终端打开。启动器会在8877—8896中检测空闲端口，使用独立的桌面数据目录，健康检查通过后自动打开默认浏览器。请不要在Codex内把CMD当作文档预览；预览文件不会执行它。

如果使用下面的手动命令，则默认地址为：

**http://127.0.0.1:8765**

也可以在项目目录运行：

```powershell
python -m qualitygate --demo
```

`--demo` 会生成3份合成数据与9条规则，首次自动运行脏订单检查。重复启动不会重复创建相同数据。端口被占用时：

```powershell
python -m qualitygate --demo --port 8766 --data-dir .qualitygate-other
```

没有安装任何运行依赖，不需要执行 `pip install`。按 Ctrl+C 关闭服务；`.qualitygate/` 保存数据与运行记录，重启保留。一个数据目录只允许一个应用进程持有，防止相互恢复正在运行的任务。

## 三分钟演示

1. 查看首次脏订单巡检：240行合成数据、9条规则、8类规则类型。
2. 打开任务，筛选错误/警告、按规则定位问题，查看行号和原始值。
3. 在“新建巡检”里选择 `synthetic_orders_clean.csv`，使用同一规则集，运行后回到概览查看前后结果。
4. 编辑规则集，会生成新版本；旧任务仍保留旧版规则及参考数据快照。
5. 导出 CSV 问题清单或 JSON 完整报告。JSON 包含数据哈希、规则快照、事件记录、汇总及保留明细。
6. 导入 `examples/` 里的 CSV，或上传自己的 UTF-8 CSV。自有数据只保存在本机数据库。

若暂时不启动服务，可用本机浏览器打开 `docs/DEMO_REPORT.html`，查看从实测JSON生成的离线报告。它是静态结果展示，不提供后台任务交互，不等同于工作台端到端验收。重新生成：`python scripts/build_demo_report.py`。

## 功能

| 领域 | 实现 |
|---|---|
| 数据导入 | UTF-8/BOM、严格CSV解析、表头/列数检查、SHA256内容去重、预览 |
| 规则引擎 | 必填、唯一性、数值范围、枚举、日期、邮箱、跨字段比较、参考表匹配 |
| 任务调度 | SQLite持久化队列、后台线程、原子领取、进度、取消、原快照重试 |
| 一致性 | 规则版本乐观锁、请求幂等键、不可变数据、规则及参考值快照 |
| 结果 | 规则命中/问题行分开计数、错误/警告、质量门禁、分页筛选、同指纹对比 |
| 恢复 | 进程中断标记、排队任务保留、禁止发布部分结果 |
| 交付 | 中文操作界面、移动端布局、JSON/CSV导出、自动化测试、CI配置 |

## 项目结构

```text
qualitygate/
  engine.py       纯规则引擎及配置校验
  db.py           数据结构、连接与事务
  service.py      导入、版本、任务、导出等应用服务
  worker.py       持久化队列消费与结果原子提交
  server.py       REST路由、请求校验、静态资源
  demo.py         可复现的合成数据与缺陷注入
  static/         中文操作页面
tests/            单元、服务、并发、故障注入与HTTP解析测试
scripts/          可复现验证与性能测量
examples/         生成的合成CSV
docs/             架构、接口、验证结果、简历与面试说明
```

## 测试

```powershell
python -m unittest discover -s tests -v
python scripts/verify.py
node --check qualitygate/static/app.js
node scripts/test_frontend.mjs
```

最后一项为可选 JavaScript 语法检查，需要Node；它不是运行依赖。HTTP测试使用真实 `BaseHTTPRequestHandler` 的内存传输，覆盖请求解析与路由，不冒充真实网络端到端测试。当前宿主环境的本机端口访问受限，浏览器端到端和视觉验收未能完成，见 `docs/VALIDATION.md`。

## 明确边界

- 单机、单工作线程、无认证，仅监听 `127.0.0.1`。**当前不用于公网部署。** 多用户生产化需换成熟ASGI框架、认证、数据库权限、队列与监控。
- CSV最大4 MiB、20,000行、80列；规则集最多32条。数据在内存中评估，不宣称流式大数据处理。
- 问题明细最多保存10,000条，完整命中汇总继续计算；页面和报告会标识截断。CSV/JSON导出只含保留明细。
- 数据集不可编辑。清洁示例是独立构造的对照数据，并非系统自动修复脏数据。
- 不承诺分布式“恰好一次”；当前是单进程锁、事务领取、结果原子提交和显式重试。
- 数值使用Decimal；邮箱只检查基础格式；日期只接受YYYY-MM-DD。空字符串/纯空白为空，字面量`NULL`不自动视为空。
- CSV导出对可触发公式的内容添加文本前缀，降低首次打开风险；无法保证所有表格软件及反复另存后的行为。程序化复查使用保留原始值的JSON。
- 没有声称已运行Docker或GitHub Actions；仓库提供可运行配置，当前本机执行证据单独保存。

## 技术取舍与参考

采用标准库是为了让项目在离线、无密钥的个人环境里立即运行，同时把事务、任务状态和快照设计清晰展示出来。后续可将HTTP层替换为FastAPI，保持服务层和引擎不变；当前简历不要写成FastAPI项目。

- [Python sqlite3文档](https://docs.python.org/3/library/sqlite3.html)：参数化SQL、连接与事务。
- [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/)：阅读后台执行的边界；当前代码没有使用FastAPI。
- [OWASP CSV Injection](https://community.owasp.org/attacks/CSV_Injection)：了解导出表格公式风险及防护局限。

代码采用MIT许可证。数据均为合成示例，邮箱统一使用example.com。

## 本轮优化与证据

工作线程将SQLite检查点按50ms节流，保留末次及提交事务的取消检查；数值边界和比较函数按规则预解析。本机20,000行、9条规则同进程交替对照3轮，后台任务中位耗时从4623.26ms降至144.57ms，汇总逐次一致。当前41项Python测试和8项前端VM断言通过。详细条件与局限见[优化记录](docs/OPTIMIZATION.md)和[验证记录](docs/VALIDATION.md)，这些数字不是生产性能承诺。
