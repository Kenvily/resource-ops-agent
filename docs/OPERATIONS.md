<img src="../frontend/public/app-icon.svg" width="40" height="40" alt="Resource Ops Agent 图标" />

# Resource Ops Agent · 操作与部署

命令均从仓库根目录执行。首次安装见 [README](../README.md)，数据及设计见 [实现说明](IMPLEMENTATION.md)。完整配置以 [`.env.example`](../.env.example) 和 [`Settings`](../backend/app/core/config.py) 为准；保留已有 `.env`，修改后重启。

## 1. 启停与开发

```bash
./scripts/start-backend.sh              # 配置的本机数据库 + 后端，默认17979
./scripts/start-backend.sh --reload     # Python热重载
./scripts/start-backend.sh --port 8002
./scripts/dev.sh                       # 后端 + Vite，前端5173
```

选择一种启动方式，避免占用同一端口。统一入口启动脚本管理的本机 PostgreSQL、执行迁移及契约校验，再启动应用；远端 / Docker 数据库需先自行启动。Ctrl+C停止应用，数据库继续运行。`.env` 不随代码热重载生效。

后端提供 `frontend/dist`；更新前端后执行 `npm run build --prefix frontend` 并刷新浏览器。端口排查用 `ss -ltnp 'sport = :17979'`。默认绑定本机；`0.0.0.0` 是监听地址，访问使用服务器真实地址，且须加入 `ALLOWED_HOSTS`。

## 2. 配置

### 数据与功能开关

| 配置 | 默认 / 用途 |
| --- | --- |
| `DATA_MODE` | `mock`；`live`需配置两个获准的`PROM_*_URL` |
| `HISTORY_DATABASE_URL` | 空；完整历史、资源助手和使用统计需PostgreSQL；Mock/Live分库 |
| `MEMORY_CAPACITY_UNIT` / `MEMORY_ALLOCATION_UNIT` | `unverified`；确认源端单位后设置 |
| `ALLOCATION_ABSENCE_IS_ZERO` | false；仅确认成功查询无分配记录代表零后开启 |
| `HISTORY_SYNC_ENABLED` | false；额度历史后台同步 |
| `OPERATIONS_METRICS_ENABLED` | false；GPU/K8s当前指标、设备及实际使用历史 |
| `HOST_METRICS_ENABLED` / `WORKLOAD_METRICS_ENABLED` | false；GPU节点主机历史 / 普通容器当前资源 |
| `EFFICIENCY_ENABLED` | false；独立使用与生命周期采集 |
| `EFFICIENCY_VERIFIED_DEVICE_MODELS` | 空；填管理员核验过容器采集能力的精确型号 |
| `RESOURCE_MODEL_CATALOG_FILE` | 空；业务模型JSON目录，修改后重启 |
| `OPERATIONS_GPU_TEMPERATURE_C` | 85；需结合硬件规格确认 |
| `OPERATIONS_WORKSPACE_ENABLED` / `INVESTIGATION_FEEDBACK_ENABLED` | false；本机交付留存 / 反馈，生产须关闭 |
| `ANALYTICS_ENABLED` | true；匿名访问、功能及Agent统计，需数据库 |

Live配置示意，不含实际地址；单位及零记录语义必须另行核验：

```dotenv
DATA_MODE=live
PROM_YZ_URL=<集群A获准的Prometheus origin>
PROM_ZW_URL=<集群B获准的Prometheus origin>
HISTORY_DATABASE_URL=<独立Live数据库连接串>
HISTORY_SYNC_ENABLED=true
```

新库回填需要时间，开关关闭不删除已存数据。使用统计从启用后积累，不回填历史访问；关闭后重启并刷新浏览器。数据库故障不回退为页面直接查询源端。

### 采集频率与预算

| 配置 | 默认 |
| --- | --- |
| `SNAPSHOT_SYNC_INTERVAL_SECONDS` | 30秒；当前资源后台更新 |
| `SNAPSHOT_REFRESH_MIN_INTERVAL_SECONDS` | 5秒；跨进程刷新冷却 |
| `HISTORY_SYNC_INTERVAL_SECONDS` | 60秒；历史调度循环 |
| `HISTORY_CHUNK_POINTS` | 30点 / 块 |
| `HISTORY_QUERY_FILL_CHUNKS` / `HISTORY_FILL_COOLDOWN_SECONDS` | 开发查询补洞2块 / 30秒冷却；生产补洞须0 |
| `RESOURCE_BACKFILL_HOURS_PER_TICK` | 每集群每轮6小时，可设1–12 |
| `API_REQUESTS_PER_MINUTE` / `API_REQUEST_BURST` | 60 / 30；进程总预算为四倍 |

间隔是本轮执行后的等待，不保证严格整点到达。历史按粒度出现新网格后同步；指标 / 主机 / 容器复用一分钟循环，主机近期每集群每轮最多30分钟，失败或空结果冷却10分钟。页面轮询、聚合及保留策略见实现说明。

### 模型与业务归属

```dotenv
LLM_ENABLED=true
LLM_DATA_PROCESSING_AUTHORIZED=true
DEEPSEEK_API_KEY=<本机密钥>
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

仅在具备数据处理授权时开启。密钥不进Git或聊天；密钥、额度、网络、超时或预算异常会降级。配置可用不等于实际调用成功，可查看 `/api/v1/insights/capabilities`。反向代理需允许长响应、关闭流式缓冲并传播取消。

仓库中的业务目录仅含脱敏演示规则，真实业务映射应保存在仓库外，通过 `RESOURCE_MODEL_CATALOG_FILE` 指向私有文件。业务目录结构参考 [business-models.json](../config/business-models.json) 与 [目录模型](../backend/app/resources/catalog.py)。目录可能含企业名称，Mock演示应设置 `RESOURCE_MODEL_CATALOG_FILE=` 或使用脱敏目录。

- `model_key`保持稳定；`category/name/description/model_names/attributes`描述归属、版本和可扩展字段。
- `selectors`限制集群及`pod_name/namespace_name/container_name`；`exact`精确、`kubernetes_pod`兼容标准后缀、`pod_prefix`显式允许业务名前缀与版本段。
- 规则不扫描任意字段或中间子串；精确优先、前缀最长优先，等长冲突保留未知。Pod家族匹配只用于`pod_name`。
- `RESOURCE_MODEL_MAPPINGS`精确容器映射仍优先；未配置保留库内映射，显式`[]`关闭当前绑定并保留旧版本。不重写历史，也不由备注中的相对日期推断已下线。

## 3. 数据库与历史维护

### 本机原生或Docker

本机脚本要求已有 PostgreSQL 程序，不负责安装。新环境首次执行`init`，既有环境不要重建：

```bash
.venv/bin/python scripts/postgres-local.py init
.venv/bin/python scripts/postgres-local.py status
.venv/bin/python scripts/postgres-local.py backup
.venv/bin/python scripts/postgres-local.py start
.venv/bin/python scripts/postgres-local.py psql
.venv/bin/python scripts/postgres-local.py stop
```

脚本管理本机5432实例；程序、数据、socket、日志和备份在`.local/postgres/`，连接串保存在`.env`，目录 / 备份权限为0700 / 0600。停止数据库前先停止应用。`data/base`、WAL等由PostgreSQL管理，不按文件手动清理。

Docker可选；在`.env`配置强`HISTORY_POSTGRES_PASSWORD`及匹配、密码已URL编码的连接串。模板库 / 用户为`gpu_history`，与原生实例不同时占用5432：

```bash
docker compose --env-file .env -f deploy/compose.postgres.yml up -d
```

### 迁移与维护命令

当前schema15。升级先停旧后端、备份，再运行统一启动脚本；迁移在事务中执行，可重复运行，契约不匹配会拒绝复用。独立部署使用以下CLI初始化，再启动应用：

```bash
PYTHONPATH=backend .venv/bin/python -m app.history.cli init
PYTHONPATH=backend .venv/bin/python -m app.history.cli status
PYTHONPATH=backend .venv/bin/python -m app.history.cli sync
PYTHONPATH=backend .venv/bin/python -m app.history.cli backfill --batches 2
```

| CLI命令 | 用途 |
| --- | --- |
| `discover` | 有界探测源端起点；不删除历史，不证明全区间完整 |
| `rollup --batches 4` | 从已有本地样本补建统计，不查询源端 |
| `repair` | 保留事实、重置进度；随后backfill会重新请求源端 |
| `validate` | 核验存量约束，不代替覆盖或业务验收 |

后台通常自动同步。手动批次用于加速，宜在维护窗口停止应用以避免争锁；失败块不推进，可重跑。恢复备份到**新建隔离库**后检查契约与关键记录，不直接覆盖运行中的Live库；回退需匹配代码及备份，不只改版本号。

### 主机长历史回填

```bash
.venv/bin/python scripts/backfill-host-history.py --window 3mo --strategy sample --batch-hours 24
.venv/bin/python scripts/backfill-host-history.py --window 3mo --status
```

默认直接按小时网格采样；支持`--cluster yz/zw`和`--batches`。共用集群锁，中断保留提交；超时缩小批次、连续成功后恢复，缺测不补零。“完成尝试”不表示覆盖完整。日志在`.local/logs/`，PID在`.local/run/`。

需要完整小时均值、极值及分钟有效数时用`--strategy aggregate --batch-hours 6`，该策略需要分钟原料。近期分钟 / 5分钟统计继续保留；两种历史不能混称为区间统计。

## 4. 排障与生产部署

| 现象 | 检查 |
| --- | --- |
| 启动失败 | 17979/5432端口、数据库程序 / 权限、连接串、schema和契约 |
| `Invalid host header` | 实际访问域名 / IP加入`ALLOWED_HOSTS`；Host校验不是认证 |
| 总览为空或无完整合计 | 集群失败 / 空 / 缺测 / 过期、单位和零记录语义；不硬编码数量 |
| CPU、内存、DMI或ECC未知 | 对应开关、源端新鲜度、身份、标签和单位；不由别的指标猜测 |
| 长历史不完整 | 检查点、质量版本、源端起点发现及回填错误；本地缺口不能证明源端丢失 |
| `crypto.randomUUID`异常或前端仍旧 | 重新构建并强制刷新；已用安全随机数兼容内网HTTP |
| 模型降级 | 数据授权、密钥、额度、服务器出网、超时及证据覆盖 |
| 统计为空 | 数据库 / 开关；访问从启用后积累，UV不是实名人数 |
| 429 / 409 / 503 | 限流冷却 / 幂等或版本冲突 / 存储与采集状态 |

`/api/live`检查进程；`/api/ready`只读检查当前快照，不能证明长历史完整；兼容的`/api/health`在开发模式可能触发原同步流程。接口文档在本机`/docs`。

生产配置独立维护，勿覆盖调试环境：

```dotenv
PRODUCTION_MODE=true
PRODUCTION_PUBLIC_ORIGIN=https://roc.internal.example:8443
ALLOWED_HOSTS=["roc.internal.example"]
GATEWAY_SHARED_SECRET=<独立的32至128位URL安全随机字符串>
HISTORY_QUERY_FILL_CHUNKS=0
OPERATIONS_WORKSPACE_ENABLED=false
INVESTIGATION_FEEDBACK_ENABLED=false
```

使用 [Nginx模板](../deploy/nginx.conf.example)，在私有include中覆盖客户端同名密钥头。验证证书、认证、失效、访问组及防绕过后，以单worker运行：

```bash
PYTHONPATH=backend .venv/bin/python -m app.history.cli init
.venv/bin/python -m uvicorn app.main:app --app-dir backend \
  --host 127.0.0.1 --port 17979 --no-access-log --no-proxy-headers
```

现有网关仅提供传输防护，不等于已实现企业SSO、用户级RBAC或可信审计。正式发布仍需认证权限、TLS / 网络隔离、备份恢复和业务验收；不得公开数据库端口、私有标签、Live截图或凭证日志。

## 5. 测试、评测与脚本

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/ruff check backend/app backend/tests
.venv/bin/mypy --config-file backend/pyproject.toml backend/app
npm run typecheck --prefix frontend
npm run build --prefix frontend
```

数据库用例会清空表，**只能指向独立、可丢弃的Mock库**；未配置则相关用例跳过：

```bash
TEST_HISTORY_DATABASE_URL='postgresql://user:password@127.0.0.1:5432/gpu_history_test' \
  .venv/bin/python -m pytest backend/tests -q
```

浏览器测试先在另一终端启动隔离Mock，显式覆盖当前`.env`中的采集及模型开关：

```bash
DATA_MODE=mock PROM_YZ_URL= PROM_ZW_URL= RESOURCE_MODEL_CATALOG_FILE= \
  MEMORY_CAPACITY_UNIT=MB MEMORY_ALLOCATION_UNIT=MB ALLOCATION_ABSENCE_IS_ZERO=true \
  HISTORY_DATABASE_URL= HISTORY_SYNC_ENABLED=false HOST_METRICS_ENABLED=false \
  WORKLOAD_METRICS_ENABLED=false OPERATIONS_METRICS_ENABLED=false EFFICIENCY_ENABLED=false \
  LLM_ENABLED=false OPERATIONS_WORKSPACE_ENABLED=false INVESTIGATION_FEEDBACK_ENABLED=false \
  PRODUCTION_MODE=false SNAPSHOT_REFRESH_MIN_INTERVAL_SECONDS=0 \
  API_REQUESTS_PER_MINUTE=600 API_REQUEST_BURST=100 \
  .venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001 --no-access-log
```

```bash
cd frontend
npx playwright install chromium --only-shell
PLAYWRIGHT_BASE_URL=http://127.0.0.1:8001 npm run test:e2e
```

有数据库的浏览器用例需另备隔离Mock服务，通过`EFFICIENCY_TEST_BASE_URL`指定；归档专项通过`ARCHIVE_TEST_BASE_URL`指定，缺环境则跳过。测试截图仅使用Mock，产物放在被Git忽略的`frontend/test-results/`。

| 脚本 | 独立用途 |
| --- | --- |
| `start-backend.sh` / `start-backend.py` | 启动入口与Python实现 |
| `dev.sh` | 后端与Vite联调 |
| `postgres-local.py` | 本机实例、账号、连接配置及备份 |
| `backfill-host-history.py` | 有界长历史回填 |
| `benchmark_history.py` / `benchmark_efficiency.py` | 独立Mock库的额度下钻 / 使用调查专项基准，会写入测试数据 |
| `benchmark_operations.py` | 运营报告查询专项基准，不采集源端 |
| `benchmark_agent_tasks.py` | 独立Mock测试库的同ID并发、结果回放、请求冲突与取消终态验证；不调用模型，仅清理本次创建的任务 |
| `evaluate-agent.py` | 规则 / Single-shot / Agent 合成对照，不访问业务库或Prometheus |
| `export-showcase.mjs` / `showcase-data.py` | 经明确授权后强制只读提取并脱敏真实历史快照，导出页面PDF与项目介绍 |
| `validate-production.py` | 强制数据库只读的覆盖、当前资源、单卡历史及有界并发验收 |

```bash
.venv/bin/python scripts/evaluate-agent.py --provider scripted
PYTHONPATH=backend .venv/bin/python scripts/validate-production.py --runs 8 --concurrency 4
```

Agent结果默认写入`.local/validation/`；`scripted`只验证协议。使用`--provider deepseek`须先配置授权与密钥，会产生费用；按`task_success`等字段判断结果，退出0只表示脚本执行完成。真实合成结果及限制见实现说明。

生产验收可加`--http-base http://127.0.0.1:17979`，仅访问固定本机只读接口，最多8次 / 端点，不调用模型、采集或清理。`--strict`固定返回2，表示企业验收门槛仍需人工确认；Mock延迟和本机短测不代表持续生产性能。

### 展示素材导出

安装Playwright Chromium并构建前端后，仅在具备真实数据展示授权时执行：

```bash
node scripts/export-showcase.mjs --authorized-real-data
```

仅更新项目介绍、保留页面PDF时加`--deck-only`。量化结果必须通过`--value-evidence <JSON路径>`传入报告，未提供时只展示功能说明；不使用固定默认成绩。报告含`service`（`validate-production.py --runs 20 --concurrency 4`输出）及`task_consistency`（下述命令输出）两个字段，合并后的本次报告在`.local/validation/showcase-value-20261116.json`。

```bash
PYTHONPATH=backend TEST_HISTORY_DATABASE_URL='postgresql://user:password@127.0.0.1:55432/roc_agent_test' \
  .venv/bin/python scripts/benchmark_agent_tasks.py
node scripts/export-showcase.mjs --authorized-real-data --deck-only \
  --value-evidence .local/validation/showcase-value-20261116.json
```

任务验证库名须以`_test`结尾，初始化还会校验Mock契约；不得使用Live业务库。查询短测和合成工作器测试分别标注，不能作为模型正确率或生产SLA。

输出仅为`assets/screenshots/*.pdf`和`assets/project-overview.pdf`，临时截图不加入仓库。脚本读取当前Live数据库，连接强制只读，禁用任务回收维护，不请求Prometheus或模型；在本进程以最后保存的健康快照时点查询资源，并标注历史时点，保留真实指标及缺测。各服务并非同一原子快照，以其采样时间为准。

节点、IP、UUID、Pod/Namespace、业务名、原始问题及源地址均脱敏；重新生成后仍应人工复核PDF，确认没有未识别的自由文本。导出不是公开发布动作。当前素材可从README进入。

## 6. Git与本地文件

开发在`develop`，合入`main`使用`git merge --no-ff develop`。不提交`.env`、`.local*`、备份、部署包、私有助手配置或Live截图。

可重建的`.pytest_cache`、`.ruff_cache`、`.mypy_cache`、`__pycache__`及浏览器测试产物可清理。`.venv`、`node_modules`、运行中的`frontend/dist`、Python安装元信息和数据库目录承担实际运行职责，不当作冗余删除。
