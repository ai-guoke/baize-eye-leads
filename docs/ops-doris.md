# 白泽之眼 · 工商大数据线索池

**白泽云析**旗下工商大数据线索平台（Doris 版）。

导入全国工商主体与触达信息，提供多维筛选、省市区街四级筛选与地图拓客。

## 服务

| 组件 | 地址 |
|------|------|
| 查询前台 | http://127.0.0.1:8765 |
| 地图拓客 | http://127.0.0.1:8765/map |
| Doris FE Web | http://127.0.0.1:8030 |
| Doris MySQL | `127.0.0.1:9030` user=`root` 无密码 |
| Stream Load | `127.0.0.1:8040`（直连 BE） |

## 常用命令

在**仓库根目录**执行：

```powershell
cd docker
docker compose up -d
cd ..

python etl\load_jzh.py --workers 8
python etl\load_monthly.py
python etl\load_tags.py
python etl\backfill_street.py
python etl\geocode_amap.py --limit 10000   # 需 AMAP_WEB_KEY
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
```

## 密钥

`secrets/amap.env`（gitignore）：

- `AMAP_JS_KEY` / `AMAP_SECURITY_CODE`：地图 Web JS（已配置）
- `AMAP_WEB_KEY`：批量地理编码（需另开「Web服务」Key）

## 数据基准

- 主体约 1305 万；触达率约 85.5%
- 街道解析约 604 万（标准库匹配）
- 地理坐标：优先街道中心点免费回填（`etl/geocode_by_street.py`）

## 数据清洗规则

详见 **[docs/数据清洗规则.md](docs/数据清洗规则.md)**（空值、省份/状态/资本、联系方式、街道、坐标、检索定稿）。
