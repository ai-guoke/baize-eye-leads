# 白泽云析 · 研发 PRD

> **归档备查。** 开源仓库当前维护重点见 [`TODO.md`](./TODO.md)（稳定性与可运行性）。  
> 本文保留中长期产品设想，**不代表正在实施的公开路线图**。

> 相关：[`平台架构方案.md`](./平台架构方案.md) · [`数据工程方案.md`](./数据工程方案.md)

工单编号：`D-*` 数据工程 · `A-*` 架构后端 · `F-*` 前端 · `Q-*` 质量与非功能。

---

## 1. 目标与非目标

### 1.1 一句话目标

把「一个跑在单机、靠查询参数认身份的内部工具」，升级为「**数据完整、查询可预期、展示不浪费、能卖给外部租户、能被 Agent 调用**」的 SaaS 底座。

### 1.2 四个可量化目标

| 目标 | 现状 | 目标值 |
|------|------|--------|
| 数据完整度 | 01 底册未跑（853 分片）、02 高新未接 | 两包入库完成，联系方式覆盖率 +X% 可量化 |
| 查询性能 | facets 实时 GROUP BY 全表；OFFSET 深翻页 | 点查 P95 < 50ms、筛选 P95 < 1s |
| 展示有效性 | 详情区块无条件渲染，99% 企业看到空的「企业简介」 | 空区块 0 个；L4 稀有字段有值才现 |
| 可售卖 | 无认证、无配额、无审计 | JWT 租户隔离 + 明文号码 100% 审计 |

### 1.3 本期非目标（明确不做）

- **一次性全量导入**——一律「样本建模 → 验证 → 放量」，见 D-00
- **配额与计费**——本期不做计量闸门；VIP 账号无限查阅，只保留审计流水
- 出海 / 研析产品线（07/08/09、04/05/06 九包中的六包）
- 目录大搬家到 `packages/` + `services/`（等出海立项）
- 向量相似企业、图谱关联、外置 ES
- 前端引入 React/Vue 构建体系（理由见 §3.1）

### 1.4 工作方式：样本先行

**不先建模就导数据，是本项目最大的返工风险。** 01 底册 853 分片跑 48 小时，跑完才发现表结构不对，代价是两天加一次全量回滚。

所有涉及新表、新字段、新筛选维度的改动，一律三段式：

```
① 样本建模   独立测试库 qcc_lab，抽样入库，验证结构与性能
② 验证放行   通过 D-00 检查清单
③ 全量放量   生产库 qcc 挂机跑
```

---

## 2. 现状基线

### 2.1 代码

| 模块 | 规模 | 状态判断 |
|------|------|----------|
| `app/main.py` | 1552 行 | 单文件承载全部 API，需按域拆分 |
| `app/templates/index.html` | 2031 行 | HTML + CSS + JS 混写，详情区块硬编码 |
| `app/templates/map.html` | 1560 行 | 与 index 存在筛选/请求逻辑重复 |
| `etl/load_jzh.py` | 539 行 | **事实上的公共规则库**，被 4 个脚本 import |
| `scripts/quality_scan.py` | 507 行 | 空值哨兵副本缺 5 项，填充率报高 |

### 2.2 数据

`companies` 7470 万 · `contacts` 1.72 亿 · `tags` 136.6 万 · `admin_divisions` 4401 街道。  
03 产业链 94 文件已全量跑完；01 底册、02 高新未开始。

### 2.3 已知缺陷（本期必须消化）

| # | 缺陷 | 影响 | 归属 |
|---|------|------|------|
| 1 | 空值哨兵两份、信用代码正则两种 | 质量指标失真，未来 bzid 会算出两个 ID | D-01 |
| 2 | 详情区块无条件渲染 | 99% 企业看到整屏 `—` | F-02 |
| 3 | `user_id` 来自查询参数 | 改个参数就能读他人数据 | A-05 |
| 4 | `allow_origins=["*"]` + Doris root 空密码 | 上线即高危 | Q-03 |
| 5 | `replication_num=1` | 单副本，SaaS 不可接受 | Q-02 |
| 6 | `contact_status` / `sales_territory` 在公共库 | 退租与合规删除会牵动公共库 | A-06 |

---

## 3. 技术选型（本期定稿）

选型的两条标准：**后期好维护** · **开源用户 clone 下来能跑起来**。据此一律选「依赖少、无额外服务、社区通用」的方案，凡是能不引入的中间件都不引入。

| 领域 | 选型 | 为什么 | 被否方案 |
|------|------|--------|----------|
| **前端** | 原生 **ES Modules**，无构建 | 浏览器直接跑，`git clone` 即可调试；无 node 工具链、无构建产物、无 lockfile 漂移。当前痛点是代码重复与区块硬编码，模块化即可解决 | React/Vue + Vite：为两个页面引入整条前端工具链，开源用户多一道 `npm install` |
| **网关** | **FastAPI 自建**（`app/gateway/`），与之眼同进程 | 单进程可跑，`uvicorn app.main:app` 一条命令；契约按独立服务设计，将来拆进程不动客户端 | APISIX/Kong：逼开源用户部署额外中间件，本期流量根本用不上 |
| **认证** | **PyJWT + passlib[bcrypt]**，JWT（UI）/ API Key（Agent） | 两个小依赖，无外部 IdP；自托管友好 | OAuth2 全套 / Auth0：引入外部依赖与账号体系 |
| **配置** | **pydantic-settings + `.env`** | 已依赖 pydantic，零新增；`.env.example` 进仓，密钥不进仓 | 自研 config 解析、hydra |
| **依赖管理** | **`pyproject.toml`**（`baize_core` 打包）+ **`requirements.txt` 全 pin** | pip 通用，任何环境可复现；`baize_core` 抽包后必须有 pyproject | poetry/uv 强制：对开源用户是额外学习成本（可选支持，不强制） |
| **测试** | **pytest + pytest-cov**，黄金用例走 `parametrize` | 事实标准；黄金用例天然是参数化表格 | unittest：写起来啰嗦 |
| **CI** | **GitHub Actions**，按路径过滤触发 | 仓库已在 GitHub；路径过滤为将来单仓多服务预留 | 自建 runner |
| **DB 迁移** | 沿用 **`sql/NN_*.sql` 顺序脚本** + `schema_migrations` 记录表 | Doris 不是标准 OLTP，alembic 支持差；顺序 SQL 直观可审计 | alembic |
| **日志** | 标准库 `logging` + JSON formatter | 零依赖；结构化日志便于后续接采集 | structlog / loguru |
| **容器化** | **docker-compose**：Doris + app 一条命令 | 开源用户最短上手路径 | k8s manifest（本期过重） |
| **地图** | 高德 JS API（已有） | 已接入，区划与坐标同源 | — |

**依赖预算**：本期新增运行时依赖不超过 **4 个**（PyJWT、passlib、pydantic-settings、pytest-cov），其中 pytest-cov 仅开发期。

### 3.1 前端不引框架的边界条件

这不是永久决定。触发重估的信号有三个：详情区块超过 **15 个** · 出现跨页面共享的复杂状态 · 需要离线缓存或 PWA。在此之前，注册表 + 单文件单区块足够，且对开源贡献者友好——改一个区块只需看懂一个 50 行的文件。

---

## 4. 里程碑总览

| 里程碑 | 周期 | 主题 | 出口标准 |
|--------|------|------|----------|
| **M0** | W1–W2 | 规则收敛 + **样本建模** | 黄金用例 CI 全绿；`qcc_lab` 验证通过 |
| **M1** | W3–W4 | 数据放量（01 底册 + 02 高新） | 两包入库完成，抽检正确 |
| **M2** | W4–W5 | 查询性能 | facets 毫秒级；游标分页上线 |
| **M3** | W5–W7 | 前端重构与展示 | 空区块归零；来源可追溯 |
| **M4** | W7–W9 | 网关与租户隔离 | JWT 鉴权 + 审计闭环（无配额闸门） |
| **M5** | W9–W10 | 实体层与 Agent | `describe_schema` 可驱动一次完整检索 |

放量后的 ETL 是挂机任务，与 M2/M3 的开发**并行**，不互相阻塞。

---

# 第一部分 · 数据工程需求

## D-00 样本建模验证 ★M0，所有放量的前置闸门

**原则**：先在小样本上把表结构、筛选性能、展示效果全部验一遍，通过检查清单才允许碰生产库。

### 独立测试库

```sql
CREATE DATABASE qcc_lab;   -- schema 与 qcc 完全一致，单副本，可随时 DROP 重建
```

生产库 `qcc` 在 M0 阶段**只读**，不接受任何新 ETL 写入。测试脚本用 `--db qcc_lab` 切换，DB 名从配置读，不硬编码。

### 样本集设计

样本不是「随便抽几个文件」，要**覆盖已知的数据形态差异**：

| 样本 | 来源 | 量级 | 覆盖什么 |
|------|------|------|----------|
| S1 工商底册 | 01 包抽 **5 个分片** | ~50 万 | 沿海/内陆/直辖市各一，覆盖区划与地址差异 |
| S2 高新 CSV | 02 包 `03_CSV全量` 抽 **2 万行** | 2 万 | 主入口字段形态 |
| S3 高新证书 | 02 包 Excel 抽 **3 个年度** | ~5 万 | 含**撤销**记录，验时间线 |
| S4 稀疏样本 | 从生产库抽 1000 家**无简介无标签**企业 | 1000 | 验前端空区块 |
| S5 富样本 | 抽 200 家**同时有产业链+高新+多号码**企业 | 200 | 验徽章、时间线、来源标注 |

S4/S5 是前端验收专用——**光看富样本会误判**，全库 99% 的企业长得像 S4。

### 放量检查清单

七项全过才允许动生产库：

| # | 检查项 | 判据 |
|---|--------|------|
| 1 | 表结构够用 | 样本字段无一落空；无需再 ALTER |
| 2 | 补空正确 | 原非空字段一字未变；`partial_columns` 未误清空 |
| 3 | **高新筛选性能** | 「浙江 + 高新 + 有手机」JOIN 驱动 P95 < 1s（决定要不要物化，见 D-04） |
| 4 | 主键匹配率 | 02 税号命中率 ≥ 95%；待审队列量可人工消化 |
| 5 | 前端不塌 | S4 稀疏样本空区块为 0；S5 富样本展示完整 |
| 6 | 耗时可推算 | 样本耗时 × 分片数 = 全量预估，与磁盘余量对得上 |
| 7 | 可回滚 | `DROP DATABASE qcc_lab` 后生产库无任何残留 |

任一项不过 → 改设计重跑样本，**不放量**。

**验收**：检查清单七项截图或日志留档，写入 `docs/lab-report-YYYYMMDD.md`。

## D-01 `baize_core` 就地抽取 ★M0 必做

**背景**：`etl/load_jzh.py` 已成为全仓依赖根，规则已在两处漂移。上了实体层后，名称指纹规则不一致会让同一家公司算出两个 `bzid`——**不报错、不崩溃、日志干净，只是永远关联不上**。

**做法**：在现仓建 `baize_core/`（**现在就用未来的名字**，第二步只需 `git mv baize_core packages/`，所有 import 一字不改）。

```text
baize_core/
  cleaning.py    EMPTY 哨兵 · is_empty · clean · tb 字节截断 · split_multi
  capital.py     parse_capital · FX 汇率 · 脏数据纠偏
  region.py      PROVINCE_MAP · STATUS_MAP · parse_region
  dates.py       parse_date · Excel 序列号
  contact.py     手机/座机/邮箱 校验与归一
  identity.py    CREDIT_RE 严格版 · surrogate_credit
  xlsx.py        流式读（迁自 scripts/xlsx_stream.py）
  doris.py       Stream Load 客户端（迁自 app/doris_client.py）
```

**约束**：

1. 原位置保留 re-export（`from baize_core.cleaning import *`），现有脚本零改动继续跑
2. 修掉两处漂移：`quality_scan.py` 补齐 5 个哨兵；`app/main.py` 的宽松正则**留在查询解析层**，不进 core（那是「用户输了什么」，不是「能否入库」）
3. 不动目录结构、不动断点目录、不改开源仓链接

**验收**：`pytest` 黄金用例全绿；重跑一个已完成分片，产出与旧逻辑逐字节一致。

## D-02 黄金用例测试 ★M0 必做

唯一能自动拦住规则漂移的机制。CI 必跑。

| 输入 | 期望输出 |
|------|----------|
| `"1000万美元"` | `(7200.00, "美元")` |
| `"5000"`（无单位） | `(0.50, "人民币")` |
| `"nan"` / `"null值"` / `"???"` / `"**"` | `None` |
| `"010-1234 5678"` | landline `"010-12345678"` |
| `"13812345678"` | mobile |
| `"1381234567"`（10 位） | landline，不是 mobile |
| `"91330100MA2xxxxx"` 小写 | 转大写后合法 |
| `"44000"`（Excel 序列） | `2020-06-xx` |
| `"存续（在营、开业、在册）"` | `"存续"` |
| 超长字符串 | 按**字节**截断，不截断出半个汉字 |

覆盖率要求：`baize_core` 行覆盖 ≥ 80%。

## D-03 01 全国工商底册补全 ★M0 样本 → M1 放量

| 项 | 内容 |
|----|------|
| 源 | `raw_datasets/01_全国企业工商底册库_2023.11/全国数据`，**853** xlsx |
| 脚本 | `etl/enrich_national_2023.py`，改 `ROOT`，加 `--db` 参数 |
| 动作 | `credit_code` 对齐 → 补空字段 → 电话邮箱追加 `contacts`（`source=national2023`）→ 主库无此主体则新增 |
| 断点 | `output/etl_state/national2023/`，键 = 文件名 + 字节数 |

**两段执行**：

```powershell
# ① M0 样本：5 分片进 qcc_lab，过 D-00 检查清单
python etl\enrich_national_2023.py --db qcc_lab --limit 5

# ② M1 放量：全量进生产，挂机约 48h（单分片约 200s × 853）
python etl\enrich_national_2023.py
```

**注意**：旧目录 613 分片是 01 的子集，以 853 为准。同码只补空 + `contacts` UNIQUE 去重，重复跑安全。

**验收**：853 分片全部 SKIP/OK；抽 100 个码，原非空字段一字未变、原空字段有新增；`has_mobile` 与明细一致。

## D-04 02 高新技术企业资质 ★M0 样本 → M1 放量

**价值**：全库仅 0.18% 有荣誉标签，高新是**第一个高区分度的资质维度**，直接支撑「找高新企业」这个高频销售场景。

### 建模决策：不加 `companies.is_hightech`

高新约 59 万，占全库 **0.8%**。三条理由：

**① 高选择性应从小表驱动。** `WHERE is_hightech=1` 要在 7470 万行主表上扫；而从 `qualifications`（约 60 万行）驱动、colocate join 回主表，扫描量差两个数量级。这与 `has_mobile` 情况相反——那个约 50% 选择性，标志位才划算。

**② 避免布尔列坟场。** 加了 `is_hightech`，下一个就是 `is_specialized_new`、`is_listed`、`is_unicorn`。每加一个资质就要 ALTER 一次 7470 万行的表，主表迟早被标志位淹没。

**③ 布尔值表达不了时效。** 2019 认定、2022 到期、2023 重认定——`is_hightech=1` 说不清「现在还有效吗」，而 `valid_to` 能。销售最关心的恰恰是**当前有效**的高新。

**职责划分**：

| 用途 | 数据源 | 查询形态 |
|------|--------|----------|
| 筛选「有效高新」 | `qualifications` | 小表驱动 + 有效期判定，join 回主表 |
| 列表徽章 | `tags.honors` | 当前页 50 家批量 `IN` 查询 |
| 详情时间线 | `qualifications` | 单码点查 |

**兜底**：若 D-00 检查清单第 3 项（JOIN 筛选 P95 < 1s）不达标，解法**不是**往主表加列，而是物化一张 `entity_labels(credit_code, label_type, label_value, valid_to)` 窄表并建索引。决策依据是实测数据，不是猜测。

### 新建表

```sql
CREATE TABLE qualifications (
    credit_code  VARCHAR(32)  NOT NULL,
    qual_type    VARCHAR(32)  NOT NULL COMMENT 'hightech / specialized_new / ...',
    cert_no      VARCHAR(64)  NOT NULL COMMENT '证书编号；缺失用 年度+序号 兜底',
    company_name VARCHAR(300),
    cert_year    VARCHAR(8)            COMMENT '认定年度',
    issue_date   DATE,
    valid_to     DATE,
    revoked      TINYINT DEFAULT "0"   COMMENT '是否被撤销',
    batch_no     VARCHAR(64)           COMMENT '认定批次',
    source       VARCHAR(32),
    loaded_at    DATETIME
) UNIQUE KEY(credit_code, qual_type, cert_no)
DISTRIBUTED BY HASH(credit_code) BUCKETS 32
PROPERTIES ("replication_num" = "2");
```

### 新脚本 `etl/load_hightech.py`

| 输入 | 处理 |
|------|------|
| `03_CSV全量格式(2024-2025)`（约 59 万） | 主入口：补 `companies` 空字段 + 电话邮箱进 `contacts`（`source=hightech2026`） |
| `01_Excel表格格式(2013-2023)` | 证书明细 → `qualifications`；含撤销记录 |
| `02_DTA学术格式` | **跳过**，与 CSV 同源冗余 |

同时 `tags.honors` 追加「高新技术企业」（合并去重，不覆盖）。

**主键风险**：02 的「纳税人识别号」不保证是 18 位码。先过严格 `CREDIT_RE`；失败则「公司名 + 省」候选匹配，命中唯一才写，否则进 `output/pending_match/` 待审 CSV，**不猜**。D-00 样本要实测命中率，低于 95% 需先改匹配策略。

**两段执行**：样本 `--db qcc_lab --limit 20000` → 过检查清单 → 放量。

**验收**：任取一家 2015 认定、2019 撤销、2022 重认定的企业，`qualifications` 有 3 行且时序正确；「浙江 + 有效高新 + 有手机」筛选 P95 < 1s。

## D-05 数据质量看板 ★M1

`scripts/quality_scan.py` 改用 `baize_core` 哨兵后重跑，产出各字段真实填充率，落表 `data_quality_snapshot(field, non_null, total, pct, scanned_at)`。

**这不是可选项**——§5.3 的前端字段分层直接依赖这份数据。填充率变了，UI 层级要跟着调。

---

# 第二部分 · 架构与后端需求

## A-01 `app/main.py` 按域拆分 ★M2

1552 行单文件，新增鉴权/审计/游标后会失控。拆为：

```text
app/
  api/
    search.py      /api/search /api/stats /api/facets
    company.py     /api/company/* /api/company/*/contacts
    geo.py         /api/map/* /api/nearby /api/admin_center /api/cities...
    crm.py         /api/territory /api/contact_status /api/tasks/*
    export.py      /api/export
  query/
    where.py       build_where / build_q_condition / from_clause
    classify.py    classify_query / normalize_phone（宽松匹配留这里）
    cursor.py      游标编解码
  deps.py          租户上下文依赖注入（A-05 用）
```

纯物理拆分 + 路由挂载，**不改任何 API 行为**。回归测试保证契约不变。

## A-02 facets 物化 ★M2

**现状**：`/api/facets` 每次实时 `GROUP BY` 全表 7470 万，首屏冷启动秒级。

```sql
CREATE TABLE facet_cache (
    dim        VARCHAR(32) NOT NULL COMMENT 'province/city/district/industry_l1/chain/scale/status/honor',
    parent_key VARCHAR(64) NOT NULL DEFAULT "" COMMENT '级联父级，如 city 的省',
    key_name   VARCHAR(128) NOT NULL,
    cnt        BIGINT,
    updated_at DATETIME
) UNIQUE KEY(dim, parent_key, key_name)
DISTRIBUTED BY HASH(dim) BUCKETS 8;
```

- 刷新脚本 `etl/refresh_facets.py`，每日一次 + ETL 批次后触发
- `/api/facets` 只读缓存表；缓存为空时**降级**走实时查询并告警，不能白屏
- 产业链维度沿用 `LATERAL VIEW EXPLODE(chain_industries)` 去重排序

**验收**：`/api/facets` P95 < 100ms。

## A-03 游标分页 ★M2

`LIMIT n OFFSET m` 在千万级越翻越慢，且 Agent 会连续翻页。

```
GET /api/search?...&cursor=<opaque>&limit=50
→ { "rows": [...], "next_cursor": "...", "has_more": true }
```

- 排序键必须**全序**：`(sort_key, credit_code)`，同参数必得同结果
- `cursor` 为 base64 编码的 `(sort_value, credit_code)`，不暴露 offset
- 兼容期：保留 `offset` 参数但标记废弃，UI 先切游标

## A-04 结构化错误与 `TOO_BROAD` 保护 ★M2

统一错误体，UI 与 Agent 共用：

```json
{ "error": { "code": "TOO_BROAD", "message": "条件过宽，预计扫描 6800 万行",
             "hint": "建议补充省份或行业", "suggest_dims": ["province", "industry_l1"] } }
```

错误码：`QUOTA_EXCEEDED` · `INVALID_PARAM` · `NOT_FOUND` · `TOO_BROAD` · `UPSTREAM_UNAVAILABLE`。

`TOO_BROAD` 触发条件：无地域、无行业、无关键词的裸查询。**直接拒绝并建议收窄维度**，而不是硬跑几十秒。

## A-05 网关与租户身份 ★M4

**现状是安全洞**：`user_id` 是普通查询参数，改个值就能读他人数据。

| 项 | 方案 |
|----|------|
| 身份 | JWT（UI）/ API Key（Agent），网关解析出 `tenant_id` + `role` |
| 角色 | `owner` / `member` / `agent`（Agent **只读**；配额本期不设，但角色位先留） |
| 注入 | FastAPI `Depends(get_tenant_ctx)`，业务层**禁止**再读 `user_id` 参数 |
| CORS | `allow_origins` 收敛到白名单 |
| 形态 | S1 阶段网关可与之眼同进程（`app/gateway/`），但**接口路径与契约按独立服务设计** |

网关从 S1 就要存在，哪怕只代理之眼一个服务，否则后面迁移会牵动所有客户端。

## A-06 租户数据迁移 ★M4

```sql
-- 新库 tenant_core
contact_status   (tenant_id, credit_code, status, note, updated_by, updated_at)
sales_territory  (tenant_id, user_id, province, city, district)
favorites        (tenant_id, user_id, credit_code, created_at)
export_records   (tenant_id, user_id, rows, filters_json, created_at)
```

从 `qcc` 迁出，全部带 `tenant_id`，支持整租户删除。存量数据归入 `tenant_id='default'`。

## A-07 脱敏与审计 ★M4

**本期不做配额闸门与计费。** VIP 账号无限查阅，普通账号同样放行，只是**全程留痕**。计费口径后期再定，届时只需在网关加一层校验，不改数据结构。

| 场景 | 行为 |
|------|------|
| 列表 / 详情默认 | `138****5678`、`a***@abc.com` |
| 点「查看完整」 | 调 `reveal_contact` → 返明文 → **写审计** |
| 导出 | 记录行数与筛选条件，不拦截 |

```sql
-- platform 库
audit_log (tenant_id, user_id, action, target, qty, channel, ip, ua, created_at)
```

`action`：`reveal_contact` / `export_rows` / `search`。`channel` 区分 `ui` / `agent`。

**为什么不做计费也要埋这张表**：审计是合规底线（谁在什么时候看了谁的手机号，必须能查），同时它天然就是将来的计量数据源——后期开 VIP 时，`GROUP BY tenant_id, action` 就是账单，**不用回头补埋点**。

**账号分级**：`platform.accounts.plan` 字段（`vip` / `normal`），本期两者行为一致，仅作标记，为后期开关预留。

## A-08 实体层 `bzid` ★M5

按架构方案 §4 建 `entity_master` / `entity_alias`；`companies` 新增可空列 `bzid` 异步回填，**主键保持 `credit_code` 不变**，不破坏现有查询与 ETL。本期只做国内主体（`credit_code` → `bzid` 一一映射），为出海留接口。

## A-09 Agent 工具集 ★M5

MCP + OpenAPI 双暴露：`describe_schema` · `search_companies` · `get_company` · `list_contacts` · `reveal_contact` · `get_chain_tags` · `get_qualifications` · `resolve_entity`。

`describe_schema` 最关键：让 Agent 自己发现「产业链有哪些取值」，而不是靠猜。它直接读 `facet_cache`。

**不让 Agent 写 SQL。** 生成式 SQL 直连 Doris 同时带来注入风险和全表扫描，一次失误就能拖垮在线查询。

---

# 第三部分 · 前端展示与交互需求

## F-01 前端工程化：拆模块，不引框架 ★M3

**决策**：不引入 React/Vue/构建链，改用**原生 ES Modules** 拆分。

理由：这是 Python 主仓，前端是两个页面；引入构建链意味着 node 工具链、构建产物、部署步骤全部增加，而当前痛点是**代码重复与区块硬编码**，靠模块化就能解决。等到需要复杂状态管理时再重估。

```text
app/static/js/
  api.js         统一 fetch 封装：错误码处理、脱敏字段、cursor
  filters.js     筛选状态 ↔ URL 参数（index 与 map 共用，消除重复）
  format.js      金额/日期/脱敏/来源人话映射
  detail/
    registry.js  区块注册表（F-02 核心）
    blocks/*.js  每个区块一个渲染函数
  badges.js      列表徽章
```

`index.html` / `map.html` 只留结构与 `<script type="module">` 引入。目标：两个模板各降到 800 行以内。

## F-02 详情页区块注册表 ★M3 核心

**现状问题**：区块以静态 `<div class="sec-title">` 硬编码在 HTML 中。`intro` 填充率仅 **1.0%**，意味着 99% 的用户打开详情会看到一个标题下面孤零零一个 `—`。`registrar`、`taxpayer_qual` 同理。

**改为声明式**：

```js
// registry.js
export const BLOCKS = [
  { key:'basic',   title:'基本信息',   order:10, always:true,
    has:()=>true,                         render:renderBasic },
  { key:'chain',   title:'产业链与荣誉', order:20,
    has:d=>d.tags?.chain_industries?.length || d.tags?.honors?.length,
    render:renderChain },
  { key:'qual',    title:'资质证书',   order:25, lazy:'/api/company/{code}/qualifications',
    has:d=>d.qual_count>0,                render:renderQualTimeline },
  { key:'contact', title:'联系方式',   order:30, always:true,
    has:()=>true, emptyText:'暂无联系方式', emptyAction:'录入',
    render:renderContacts },
  { key:'ids',     title:'标识与资质', order:40, always:true, render:renderIds },
  { key:'addr',    title:'地址与网络', order:50, always:true, render:renderAddr },
  { key:'intro',   title:'企业简介',   order:60,
    has:d=>!!d.intro,                     render:renderIntro },
  { key:'scope',   title:'经营范围',   order:70, always:true, render:renderScope },
  { key:'trade',   title:'外贸记录',   order:80, lazy:'/api/trade/summary/{code}',
    failSilent:true,                      render:renderTrade },
];
```

**渲染规则**：

| 字段 | 语义 |
|------|------|
| `always` | 恒显示（覆盖率高的 L1/L2 区块） |
| `has(d)` | 返回 false 则**整块不渲染**，连标题一起消失 |
| `lazy` | 详情打开后异步拉取，加载中显骨架屏 |
| `failSilent` | 请求失败静默隐藏——出海服务没上线不影响之眼详情 |

**验收**：随机抽 20 家企业打开详情，**空区块数为 0**；有简介的企业照常展示。

## F-03 多值与时序展示 ★M3

不同数据形态给不同呈现，不能一律拍成键值对：

| 形态 | 展示 | 交互 |
|------|------|------|
| 多值标签 | chips | **点击即按该标签筛选**（产业链已实现，荣誉/高新补上） |
| 层级路径 | 面包屑，默认显示前 3 条 | 「展开全部」 |
| 长文本 | 限高滚动 + 一键复制 | 复制反馈 toast |
| **时间序列** | **时间线** | 高新证书按年份纵向排列，撤销的置灰划线并标注 |
| 明细事实 | 摘要 + 「查看全部」 | 跳详情子页 |

高新证书特别说明：同一家企业 2013–2025 可能多次认定、中途撤销。堆成一排 chips 会丢失时间语义，必须做时间线，并在区块头标注**当前是否有效**。

## F-04 列表页徽章与信息密度 ★M3

**原则**：列数不随字段增加而增加。新数据类型以徽章形式挂在企业名后。

```
杭州某某科技有限公司  [高新] [产业链·人工智能]
存续 · 浙江杭州余杭 · 软件和信息技术服务业 · 注册 1000 万 · 📱
```

徽章优先级：`高新` > `专精特新` > `产业链` > `已上市`，最多展示 2 个 + `+N`。

**为什么重要**：产业链 1.8%、荣誉 0.18% 是全库最稀缺的高价值信号。埋在详情里等于没有——放在列表才能让用户一眼看出「这家有料」。

## F-05 筛选维度门槛 ★M3

**不是入库了就该做筛选项。** 进入侧栏需同时满足：覆盖率 ≥ 5% · 取值可枚举 · 有明确业务语义。

| 维度 | 覆盖率 | 判定 |
|------|--------|------|
| 省市区街 / 行业 / 状态 / 规模 / 触达 | 高 | ✅ 已有 |
| 产业链 | 1.8% | ✅ 已上（低覆盖但高价值，作为**正向筛选**成立） |
| **高新** | 待测 | ✅ 本期新增 |
| 实缴资本 / 曾用名 | 17% / 9% | ⚠️ 进「更多筛选」折叠区 |
| 简介 / 登记机关 / 网址 | 1% / — / 2.3% | ❌ 只展示不筛选 |

低覆盖维度作为**正向筛选**（勾了就是找有的）成立，作为**排除筛选**不成立——UI 上只提供「是」，不提供「否」。

## F-06 来源与新鲜度 ★M3

多源合并后信任的基础。用户一定会问「这个手机号哪来的、什么时候采的」。

```
📱 138****5678   国家工商底册 · 2023-11        [查看完整]
📱 139****1234   产业链基础信息 · 2025-01  🟢  [查看完整]
✉  a***@abc.com  高新资质库 · 2026-03      🟢  [查看完整]
☎  0571-8888xxxx 江浙沪皖全量 · 2024-11    ⚠ 较早
```

- 来源做**人话映射**：`national2023` → 「国家工商底册 2023.11」，**不暴露内部标识**
- 新鲜度：< 1 年 🟢；1–2 年 无标；> 2 年 ⚠「较早」
- 同类型多号按 `collected_at` 倒序，最新在前
- 区块级标注「部分字段来自产业链基础信息补全」

## F-07 脱敏交互 ★M4

本期无额度限制，交互只解决「掩码 ↔ 明文」的顺手程度。

| 状态 | UI |
|------|-----|
| 默认 | 掩码 + 「查看完整」按钮 |
| 点击 | 按钮转 loading → 明文 + 复制按钮，**本次会话内保持明文** |
| 批量 | 详情页「显示全部号码」一键展开，一次审计一条汇总记录 |
| 列表页 | 保持掩码 + 触达芯片，**不提供逐行 reveal**（避免刷号） |

配额相关 UI（顶栏余量、超额弹层、升级入口）**本期不做**，等计费口径定了再加。前端 `api.js` 需预留 `QUOTA_EXCEEDED` 错误码分支，届时不用改调用方。

## F-08 空态与错误态 ★M3

| 场景 | 处理 |
|------|------|
| 0 命中 | 提示 + **自动放宽建议**（去掉最后一个筛选项试试） |
| `TOO_BROAD` | 展示 `suggest_dims`，做成可点击的补充筛选按钮 |
| 接口失败 | 局部区块降级，不整页白屏 |
| 高德加载失败 | 地图区提示 + 「切列表视图」（`now-map-stable`） |
| 懒加载区块 | 骨架屏，超 3s 显「加载较慢」，失败按 `failSilent` 处理 |

## F-09 地图与列表参数同源 ★M3

`filters.js` 统一筛选状态，index 与 map 共用，消除两边重复实现导致的**筛选结果不一致**。切换视图时筛选条件完整保留。

---

# 第四部分 · 非功能需求

## Q-01 性能 SLA

| 路径 | 典型查询 | 目标 |
|------|----------|------|
| 点查 | 信用代码、手机反查、详情 | P95 < 50ms |
| 筛选统计 | 省市区 + 产业链 + 有手机 + 计数 | P95 < 1s |
| 全文语义 | 公司名模糊、经营范围 | P95 < 2s |
| facets 首屏 | — | P95 < 100ms |

**每个对外接口都要在代码注释里标明归属哪一层**，不允许「看起来是点查、实际全表扫」。

## Q-02 可用性

- 公共库 `replication_num` 升到 **≥2**（上线前必须，当前为 1）
- Doris **Workload Group** 划分在线查询 / 批量导入 CPU 与内存配额，**导入永远让路**
- 验收：全量导入进行时，在线查询 P95 劣化 < 20%

## Q-03 安全

| 项 | 要求 |
|----|------|
| Doris | root 空密码 → 强密码；应用用独立只读/读写账号 |
| CORS | 白名单，去掉 `*` |
| 越权 | 无法通过改参数读到他人数据（渗透用例覆盖） |
| 审计 | 明文号码调用 100% 有记录 |
| 密钥 | 高德 Key、JWT Secret 走环境变量，不进仓库 |

## Q-04 测试

| 层 | 内容 |
|----|------|
| 单元 | `baize_core` 黄金用例，覆盖 ≥ 80% |
| 契约 | API 快照测试，A-01 拆分前后响应逐字段一致 |
| 回归 | `q_fields` 各维 + 关联预览 + search/stats 一致 + 0 命中放宽（`now-regression`） |
| 性能 | 固定 20 条代表性查询，每次发版对比 P95 |

## Q-05 可观测

慢查询日志（> 1s 记 SQL 与参数）；ETL 每批次落 `etl_run_log`（文件、行数、耗时、bad_rows）；接口 QPS 与错误码分布。

---

## 5. 排期

```
W1  ██ D-01 baize_core + D-02 黄金用例
W2  ██ D-00 qcc_lab 建库 + S1~S5 样本入库 + 检查清单
    └─ 闸门：七项全过才进 W3；不过则改设计重跑样本
W3  ██ D-04 高新 ETL 定稿 ‖ D-03 01 底册放量开跑（挂机 48h）
W4  ██ D-04 高新放量 + D-05 质量看板 ‖ A-01 main.py 拆分
W5  ██ A-02 facets 物化 + A-03 游标 + A-04 错误码 ‖ F-01 前端模块化
W6  ██ F-02 区块注册表 + F-03 时序展示
W7  ██ F-04 徽章 + F-05 筛选门槛 + F-06 来源新鲜度 ‖ A-05 网关起步
W8  ██ A-05 JWT/租户 + A-06 tenant_core 迁移
W9  ██ A-07 脱敏审计 + F-07 脱敏交互 ‖ Q-02 副本/资源组 + Q-03 安全
W10 ██ A-08 bzid + A-09 Agent 工具集 + Q-04 全量回归
```

**关键路径**：D-01 → **D-00 闸门** → D-03/D-04 放量 → A-01 → A-02/A-03 → F-01 → F-02。

**W2 是唯一的硬闸门**：样本不过就不放量。宁可 W2 多花三天改表结构，也不要 W3 跑完 48 小时才发现要回滚。

**可并行**：放量后 ETL 是挂机任务；前端 F-01/F-02 不依赖网关。  
**硬依赖**：F-02 的资质区块等 D-04；F-07 等 A-05 的租户上下文。

---

## 6. 验收清单

| # | 判据 | 对应 |
|---|------|------|
| 1 | 黄金用例 CI 全绿，全平台清洗规则只有一份 | D-01/D-02 |
| 2 | `qcc_lab` 七项检查清单全过并留档 | D-00 |
| 3 | 01 底册 853 分片跑完，抽检补全正确、原值未被覆盖 | D-03 |
| 4 | 「浙江 + 有效高新 + 有手机」筛选 P95 < 1s，未加主表标志位 | D-04 |
| 5 | 详情有证书时间线，撤销记录置灰可辨 | F-03 |
| 6 | 随机 20 家企业详情**空区块为 0** | F-02 |
| 7 | 任一手机号可追溯来源与采集时间，来源为人话 | F-06 |
| 8 | 点查 P95 < 50ms，筛选 P95 < 1s，facets < 100ms | Q-01 |
| 9 | 导入进行时在线查询劣化 < 20% | Q-02 |
| 10 | 改参数无法读他人数据；明文号码 100% 有审计记录 | A-05/A-07 |
| 11 | 列表深翻第 1000 页与第 1 页耗时同量级 | A-03 |
| 12 | Agent 仅凭 `describe_schema` 完成「按产业链 + 地域找有手机企业」 | A-09 |
| 13 | **开源可复现**：clone 后 `docker compose up` + 一条导入命令即可跑通样例 | 选型 |

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 建模不充分就放量 | 跑完 48h 才发现要改表，回滚代价大 | **D-00 七项闸门**，样本不过不放量 |
| 样本不代表全库 | 富样本上好看，上线一片空白 | 样本集含 S4 稀疏样本（全库 99% 长这样） |
| 01 全量 48h 中断 | 进度回退 | 断点按文件 + 字节数，重跑自动 SKIP |
| 02 税号非 18 位码 | 错挂资质到别家 | 严格校验优先；名称匹配唯一才写，否则进待审，**不猜** |
| 高新 JOIN 筛选慢 | 核心场景体验差 | D-00 实测；不达标改物化窄表，**不往主表加列** |
| A-01 拆分引入回归 | 线上功能坏 | 契约快照测试先行，纯物理移动不改逻辑 |
| 前端不引框架，区块多了难维护 | 技术债 | 注册表 + 单文件单区块已足够；触发重估条件见 §3.1 |
| 网关与之眼同进程，隔离不彻底 | 后续拆分痛 | 接口契约按独立服务设计，只是部署时同进程 |
| 副本升 2 磁盘翻倍 | 空间不足 | 升副本前先测容量；01 入仓约 300GB+ 量级 |
| 后期加计费要返工 | 埋点补不齐 | 本期 `audit_log` 已按计量口径设计，届时只加闸门 |

---

## 8. 已决策

| # | 问题 | 结论 | 依据 |
|---|------|------|------|
| 1 | 导入策略 | **样本建模 → 检查清单 → 放量**，独立 `qcc_lab` | §1.4 · D-00 |
| 2 | 计费口径 | **本期不做**；VIP 无限查阅，只留审计。`audit_log` 按计量口径预埋 | A-07 |
| 3 | 高新是否进主表 | **不加 `is_hightech`**；`qualifications` 小表驱动筛选 | D-04 建模决策 |
| 4 | 前端技术栈 | **原生 ES Modules，不引框架** | §3 · §3.1 |
| 5 | 网关选型 | **FastAPI 自建**，与之眼同进程，契约按独立服务设计 | §3 |
| 6 | 认证方案 | **PyJWT + passlib**，JWT / API Key，不引外部 IdP | §3 |
| 7 | 依赖与迁移 | `pyproject.toml` + pinned `requirements.txt`；SQL 顺序脚本，不用 alembic | §3 |

## 9. 待决策（均不阻塞开工）

| # | 问题 | 选项 | 何时需要 |
|---|------|------|----------|
| 1 | 副本数 | 2 副本省盘 / 3 副本更稳 | W9 前，先测容量 |
| 2 | 开源仓策略 | subtree split 同步 / 退化为产品门面 | 出海立项时 |
| 3 | VIP 与普通账号的实际差异 | 目前仅标记，将来区分什么 | 计费立项时 |

---

## 10. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-09-05 | 初版：M0–M5 六个里程碑，数据工程 5 项 / 架构 9 项 / 前端 9 项 / 非功能 5 项 |
| 2026-09-05 | 改为样本先行（新增 D-00 + `qcc_lab` + 七项闸门）；计费本期不做改为纯审计 + VIP；高新定为不加主表标志位；补技术选型章节（前端无构建 / FastAPI 网关 / PyJWT / 顺序 SQL） |
