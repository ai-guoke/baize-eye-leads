# Baize Eye Leads · 白泽之眼

**白泽云析**旗下 [工商大数据](https://github.com/ai-guoke/baize-eye-leads) 线索池。

汇聚工商登记主体与联系方式，提供多维检索、省市区街筛选与地图拓客，方便销售 / 运营做线索圈选。

> 仓库名：`baize-eye-leads` · 产品名：白泽之眼

## 能力一览

- **检索**：公司名 / 法人 / 手机 / 邮箱 / 信用代码 / 经营范围 / 地址（可多维 OR）
- **筛选**：省市区街、行业、规模、登记状态、触达方式
- **列表**：命中统计、省份 / 行业分布、企业详情与联系方式
- **地图拓客**：视野聚合、区域侧栏、地点搜索（高德）

## 技术栈

| 层 | 选型 |
|----|------|
| 分析库 | Apache Doris |
| API | FastAPI + Uvicorn |
| 前端 | 原生 HTML / JS（列表 + 地图） |
| 地图 | 高德 JS API 2.0 |

## 快速开始

### 1. 环境

- Python 3.10+
- Docker（启动 Doris，见 `docker/`）
- 高德开放平台 Key（地图功能）

```powershell
cd baize-eye-leads
python -m venv .venv
.\.venv\Scripts\activate
pip install fastapi uvicorn pydantic httpx
```

### 2. 密钥

```powershell
copy secrets\amap.env.example secrets\amap.env
# 编辑 secrets\amap.env，填入 AMAP_JS_KEY / AMAP_SECURITY_CODE / AMAP_WEB_KEY
```

### 3. 启动 Doris

```powershell
cd docker
docker compose up -d
```

按 `sql/` 与 `etl/` 文档导入工商数据后：

```powershell
python -m uvicorn api:app --host 0.0.0.0 --port 8765
```

| 页面 | 地址 |
|------|------|
| 列表检索 | http://127.0.0.1:8765 |
| 地图拓客 | http://127.0.0.1:8765/map |

## 目录结构

```
api.py / doris_client.py   # 查询 API
templates/                 # 列表页、地图页
etl/                       # 导入、街道回填、地理编码
sql/                       # Doris 建表
docs/                      # 数据清洗规则等
docker/                    # Doris Compose
secrets/                   # 本地密钥（不入库，仅 *.example）
```

## 数据与合规

本仓库**不包含**工商原始库与联系方式样本。你需要自行准备合法授权的数据源，并遵守当地个人信息与数据安全法规。清洗约定见 [docs/数据清洗规则.md](docs/数据清洗规则.md)。

## 许可

见 [LICENSE](LICENSE)。
