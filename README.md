# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 资源占用与派单

- `POST /api/resources`：登记资源，字段`code`（编号，唯一）、`kind`（`person`/`vehicle`）、`type`（人员：commander/firefighter/driver/medic/signal；车辆：engine/tanker/carrier/command/rescue）。
- `GET /api/resources?kind=&status=`、`GET /api/resources/{id或code}`：查询资源，响应含`status`（available/occupied）和`current_hold`（当前占用事件、请求编号）。
- `POST /api/allocations`：派单，字段`request_no`（请求编号，幂等键）、`resource_codes`（资源编号数组）、可选`item_id`、`note`。
  - 同一`request_no`重复请求（任意频道）返回原分配单，`replayed=true`，不重复占用。
  - 任一资源已被其他未结束事件占用时返回`409`，`details.occupied`列出每个被占资源及其占用方（allocation_id、request_no、item_id、created_by、created_at）；整单不落库。
- `POST /api/allocations/{id}/release`：办结释放，可在请求体带`request_no`校验；重复释放幂等。
- `GET /api/allocations?status=active&item_id=`、`GET /api/allocations/{id}`：查询派单及其资源明细。
- 事件（item）流转到`closed`时自动释放关联的全部活动派单，释放清单在响应的`released_allocations`中。

独占保证在数据库层实现：活动占用写入`resource_holds`（`resource_id`为主键），派单在单事务内完成“幂等检查→占用检查→写占用→更新资源状态”，并发请求只有一单成功。登记/派单允许 field_commander、logistics；释放另允许 incident_commander；查看允许全部角色。


## 测试

```bash
python3 -m unittest discover -s tests -v
```
