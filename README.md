# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/correlation.py` 负责时频关联与共识判定，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

**时频关联**：同一记录下的来源按区域相同、频点相差不超过 0.05MHz、观测时间相差不超过十分钟（并查集单链归组）组成联合证据，摘要含最早观测时间、最强信号、涉及站号。主记录所在组至少集齐三个不同监测站后，协调员才能 `suspend`；证据不足时返回 `insufficient_consensus` 并说明仍缺几站（请求里带 `expected_stations` 时直接点名缺哪个站）。每次纳入新来源都会在同一事务内重算关联、递增版本并追加 `correlation_updated` 审计事件；归并只更新关联摘要，已经签发的停用授权不会被改写，换编号重签返回 `authorization_immutable`，同一编号重放幂等。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/audit`。测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度和版本冲突。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
