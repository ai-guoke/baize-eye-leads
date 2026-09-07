# Baize Eye Leads · 白泽之眼

<p align="center">
  <strong>开源的工商企业检索与地图拓客脚手架</strong><br/>
  多维检索 · 行政区划 · 触达筛选 · 高德地图
</p>

<p align="center">
  <a href="https://github.com/ai-guoke/baize-eye-leads"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-ai--guoke%2Fbaize--eye--leads-0d6b55?logo=github" /></a>
  <img alt="Stack" src="https://img.shields.io/badge/Stack-Doris%20%2B%20FastAPI%20%2B%20Amap-1a7a4c" />
  <img alt="License" src="https://img.shields.io/badge/License-MIT-blue" />
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white" />
</p>

用 **Apache Doris + FastAPI** 搭一套可自托管的企业线索检索台：按公司名 / 法人 / 手机 / 邮箱 / 信用代码检索，按省市区街与行业筛选，右侧看详情，地图上圈选拓客。

> **本仓库只开源代码与文档，不附带任何工商原始数据。**  
> 你需要自行准备合法来源的数据，按 `sql/` + `etl/` 导入后使用。

---

## 界面预览

### 关键词检索 + 命中分布

![列表检索](docs/screenshots/01-list-search.png)

### 区域圈选

![苏州列表](docs/screenshots/02-list-suzhou.png)

### 地图拓客

![苏州地图](docs/screenshots/03-map-suzhou.png)

截图来自演示环境；联系方式等字段已脱敏展示。

---

## 功能一览

| 模块 | 说明 |
|------|------|
| **检索** | 公司名 / 法人 / 手机 / 邮箱 / 信用代码 / 经营范围 / 地址，支持维度勾选 |
| **筛选** | 省市区街、国标行业、规模、登记状态、是否有手机/座机/邮箱 |
| **详情** | 基本信息、联系方式、地址、经营范围；空区块自动隐藏 |
| **导出** | 当前筛选结果导出 CSV |
| **地图** | 高德底图、区县聚合、与列表条件互通 |

---

## 适合谁

- 学习 Doris / 企业数据检索的**课程作业、毕设、科研原型**
- 需要**自托管**内部查询台、且已具备合法数据授权的团队
- 想参考「OLAP + 轻前端」怎么把千万级主体做成可用产品的开发者

**不适合**：期望 clone 后立刻拥有全量工商库、或用于商业电销获客的场景（见下方使用边界）。

---

## 技术栈

| 层 | 选型 |
|----|------|
| 分析库 | [Apache Doris](https://doris.apache.org/) |
| API | FastAPI + Uvicorn |
| 清洗规则 | `baize_core/`（可单测的共享规则包） |
| 前端 | 原生 HTML / JS（无构建链） |
| 地图 | 高德 JS API 2.0 |

---

## 快速开始

### 环境

- Python 3.10+
- Docker（启动 Doris，见 `docker/`）
- 高德开放平台 Key（仅地图需要）

### 安装

```bash
git clone https://github.com/ai-guoke/baize-eye-leads.git
cd baize-eye-leads

python -m venv .venv
# Windows: .\.venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp secrets/amap.env.example secrets/amap.env
# 编辑 secrets/amap.env，填入 AMAP_JS_KEY / AMAP_SECURITY_CODE / AMAP_WEB_KEY
```

### 启动 Doris 与 API

```bash
cd docker && docker compose up -d && cd ..

# 先按 sql/ 建表，再用 etl/ 导入你自己的数据，然后：
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

| 页面 | 地址 |
|------|------|
| 列表检索 | http://127.0.0.1:8765 |
| 地图拓客 | http://127.0.0.1:8765/map |

跑通规则单测（可选）：

```bash
pip install pytest
pytest tests/ -q
```

---

## 仓库里有什么 / 没有什么

| 会开源推送 | 不会进公开仓 |
|------------|----------------|
| `app/` API 与页面 | 工商 / 产业链等**原始数据文件** |
| `baize_core/` 清洗规则 + `tests/` | `secrets/amap.env` 等真实密钥 |
| `etl/` 导入与衍生脚本 | `output/` 断点、待审、导出产物 |
| `sql/` 建表脚本 | 本地 `.xlsx` / `.csv` / 数据库快照 |
| `docker/` Doris Compose | 内网运维备忘、实验室报告 |
| `docs/` 说明与截图 | |

---

## 目录结构

```text
app/              # FastAPI + 模板/静态资源
baize_core/       # 共享清洗规则（空值、资本、电话、信用代码…）
etl/              # 导入、街道回填、地理编码
sql/              # Doris DDL
docker/           # 一键 Doris
docs/             # 文档与截图
scripts/          # 运维与工具；archive/ 为历史探测脚本
tests/            # 黄金用例
secrets/          # 仅 *.example 入库
```

---

## 文档

| 文档 | 说明 |
|------|------|
| [docs/TODO.md](docs/TODO.md) | 当前维护重点（产品稳定性） |
| [docs/ops-doris.md](docs/ops-doris.md) | Doris 部署与运维 |
| [docs/数据工程方案.md](docs/数据工程方案.md) | 清洗与入库约定 |
| [docs/PRD.md](docs/PRD.md) | 中长期设想（归档备查） |
| [docs/平台架构方案.md](docs/平台架构方案.md) | 多产品 / Agent 等（归档备查） |

---

## 数据与交流

仓库**不提供**全量工商数据库。若你在做科研、课程或作业，需要样例导入指引或环境联调，可扫码加微信（备注：`白泽之眼` + 用途，如「课程作业 / 毕设 / 科研」）：

<p align="center">
  <img src="docs/wechat-qr.png" alt="微信二维码" width="240" />
</p>

### 使用边界

1. 相关数据与材料**仅供科研、学习、教学与作业练习**。
2. **禁止**用于商业获客、营销外呼、售卖转售或其它营利用途。
3. 请遵守所在地个人信息保护与数据安全法规；违规后果自负。
4. MIT 许可仅适用于**本仓库软件代码**；数据不随仓库分发，不构成商业授权。

请勿将密钥、原始库或 `output/` 推送到公开仓库（已在 `.gitignore` 中排除）。

---

## 许可

[MIT](LICENSE) © 白泽云析 / ai-guoke

欢迎 Issue / PR。若本项目对你有帮助，欢迎 Star。
