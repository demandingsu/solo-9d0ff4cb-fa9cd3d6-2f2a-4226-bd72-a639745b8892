# 基金多层持仓穿透服务

按季度接收基金持仓表，计算基金经多层互持后落到各只证券的合并权重，
并把无法穿透的份额单独归类汇总。FastAPI + SQLite，单容器 Docker Compose 启动。

## 业务规则

- 每张持仓表按 **（季度, 基金）** 报送，每行包含对象类别（`fund` / `security`）、
  代码、**整数基点**权重，整表合计 **至多 10000** 基点（允许空表）。
- 同一证券从不同持有路径分到的权重 **合并累加**。
- **有出口的互持组**：组内存在证券、缺报或余额出口时，建立线性方程组
  按比例分摊，流出到证券的份额算足，不会因环而漏算或重复。
- **无出口互持组（封闭 SCC）**：组内互持且没有任何出口，组内基金自身
  100% 计未穿透；外部流入该组的份额在流入方计一次未穿透，不重复分摊。
- 未穿透三类来源，分开列示并给出总计：
  | reason | 含义 | detail |
  |---|---|---|
  | `missing` | 缺报：持有从未报送过的基金 | 缺报基金代码 |
  | `residual` | 表中未列的余额：合计不足 10000 的缺口 | `unallocated_residual` |
  | `closed_group` | 流入无出口互持组的份额 | 组成员代码（排序拼接） |
- 每只报送基金的穿透结果恒满足：证券权重 + 未穿透 = **10000 基点**。
- 计算全程使用 `fractions.Fraction` 精确有理数，接口同时返回 6 位小数
  （`weight_bp`）和精确分数（`weight_bp_exact`，如 `50000/9`）。

## 增量重算范围

补报或替换某基金一张表时，只重算 **同季度** 内：

1. 该基金本身；
2. 在基金持有图上直接或间接持有它的全部基金（祖先集合，取替换前后并集）。

其他季度、其他基金的结果完全不动（响应中 `recomputed_funds` 即本次范围）。

## 事务与一致性

一次报送在单个 SQLite 事务内完成「替换报送表 → 重算 → 刷新结果」：

- 任何一步失败整体回滚，**旧结果继续可查**，不会留下半张表；
- 结果表只在事务提交后对外可见，查询永远只读到完整提交的结果；
- WAL 模式下读不阻塞写、写不阻塞读；应用层对写操作加互斥锁串行化。

## 启动

```bash
docker compose up --build
# 打开交互文档
open http://localhost:8000/docs
```

SQLite 数据文件存放在命名卷 `fund_data`（容器内 `/data/fund_holdings.db`）。

本地无 Docker 时：

```bash
pip install -r requirements.txt
FUND_DB_PATH=./fund_holdings.db uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/funds/holdings` | 报送/替换（`replace=true`）一张表 |
| GET | `/quarters/{quarter}/funds/{fund}/penetration` | 完整穿透结果 |
| GET | `/quarters/{quarter}/funds/{fund}/securities/{security}` | 单证券合并权重 |
| GET | `/quarters/{quarter}/unpenetrated?fund_code=...` | 未穿透总计（省略基金为全季度） |
| GET | `/quarters/{quarter}/reports` | 已完整提交的报送表清单 |
| GET | `/health` | 健康检查 |

季度格式 `YYYYQn`（如 `2026Q3`）。校验失败返回 422；重复报送未带 `replace`
返回 409；查不到基金返回 404；提交过程失败返回 500 且旧结果不变。

### 报送示例

```bash
curl -X POST http://localhost:8000/funds/holdings \
  -H 'Content-Type: application/json' \
  -d '{
    "quarter": "2026Q3",
    "fund_code": "F001",
    "replace": false,
    "rows": [
      {"target_type": "fund", "target_code": "F002", "weight_bp": 6000},
      {"target_type": "security", "target_code": "600519", "weight_bp": 3000}
    ]
  }'
```

整表合计 9000，剩余 1000 基点自动计为 `residual` 未穿透。

## 测试

```bash
pip install -r requirements.txt pytest httpx
PYTHONPATH=. pytest -q
```

- `tests/test_solver.py`：穿透核心（多层合并、有/无出口互持、缺报、余额、自环）
- `tests/test_api.py`：端到端报送/查询、增量范围、校验失败回滚、未穿透汇总
- `tests/test_advanced.py`：事务原子性、季度隔离、互持组补报后“开封”、空表
- `scripts/demo.sh`：对运行中的服务做一遍完整冒烟（两种互持 + 补报 + 替换）

## 目录结构

```
app/
  solver.py    # 穿透计算：Tarjan SCC + 线性方程组求逆（精确分数）
  db.py        # SQLite 连接、建表、分数序列化
  schemas.py   # Pydantic 模型与输入校验
  services.py  # 报送、祖先追溯、增量重算、查询组装
  main.py      # FastAPI 路由与异常映射
tests/         # pytest 套件（24 个用例）
scripts/demo.sh
Dockerfile, docker-compose.yml, requirements.txt
```
