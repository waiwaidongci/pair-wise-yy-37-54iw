# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

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
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 续期影响清单（带版本）

设施更换治理设备后，续期材料、设备台账和现场检查不再各记一份：每份续期清单在提交时绑定当前设备台账快照和未关闭整改快照，材料、版本和审计在同一事务内落库。

- `POST /api/items/{id}/equipment`：登记治理设备（applicant）。
- `GET /api/items/{id}/equipment`：查看设备台账。
- `POST /api/items/{id}/equipment/{eq}/status`：设备启停（applicant），触发影响评估。
- `POST /api/items/{id}/inspection-corrections`：检查记录更正（inspector），触发影响评估。
- `POST /api/items/{id}/renewals`：提交续期清单（applicant），按`application_no`幂等；写入失败后以同一申请号重试即恢复续传，返回`resumed: true`和已保存的材料与版本。
- `GET /api/items/{id}/renewals`、`GET /api/renewals/{id}`：查看清单及其绑定的快照、材料和影响记录。
- `POST /api/renewals/{id}/materials`：补交材料（applicant），必须提交`expected_version`；两人同时补交同一设施时先保存者占用当前版本，后到者收到409及`details`中的差异清单（added/removed/changed）和冲突材料。被退回（`returned`）的清单补交后回到`pending`待审。
- `POST /api/renewals/{id}/approve`：审批（compliance_manager），仅`pending`可批；退回的清单须核对重新提交后才能审批。

影响评估规则：设备启停、检查记录更正或补交材料后，未审批清单退回核对（`returned`）；已审批许可保留原结论（`conclusion`不变），在`affected_scope`中列出受影响范围。清单每次变更都会增加`version`并写入审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
