# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/correlation.py` 负责时频关联与联合证据摘要，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

时频关联规则：区域相同、频点相差不超过 0.05MHz、观测时间相差十分钟以内的来源两两连通（并查集，支持链式传递），构成一个联合证据簇。每个簇摘要包含最早观测时间、最强信号（来源/站号/强度/时间）、涉及站号与共识状态。事件初始观测也作为一个观测点；来源未填区域/频点时回退到所属事件。创建事件时可选传 `expected_stations`，证据不足时会明确指出仍缺哪个预期站号，否则只报告缺少的站数。

停用（suspend）前置条件：联合证据必须覆盖至少 3 个不同监测站，否则返回 `insufficient_consensus`（409）。共识依据随停用授权固化进 `payload.consensus_evidence`；关联结果是实时派生视图（`GET /api/items/<id>` 的 `correlation` 字段），后续纳入新来源只追加 `correlation_updated` 审计事件，不改写已签发授权。每次纳入来源的审计变化类型为 `formed`（新簇）、`joined`（加入已有簇）或 `merged`（归并多个簇），并携带归并前后的簇快照。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/audit`。测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度和版本冲突。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
