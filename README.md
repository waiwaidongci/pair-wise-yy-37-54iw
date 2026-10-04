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

## 续期材料：带版本的影响清单

设施换过治理设备后，续期材料、设备台账和现场检查各记一份。续期材料做成带版本的影响清单：

- **提交绑定**：提交续期申请时绑定当前设备台账和未关闭整改（快照）。
- **环境变更联动**：设备启停、检查记录更正或补交材料后，未审批清单退回核对（状态回到`verifying`）；已审批许可保留原结论，并在`impact_scope`中列出受影响范围。
- **并发控制**：两人同时补交同一设施时，先保存的占用当前版本；后到者收到`409`，响应体携带`diff`（材料、台账、整改的差异清单）和`conflicting_materials`（双方都修改过的冲突材料）。
- **失败恢复**：写入失败后按原申请号（`application_no`）恢复续传。材料、审计和版本在同一事务中保存；响应丢失后用相同`expected_version`和相同材料重试，返回幂等结果而不升版本。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

设备台账：

- `GET /api/items/{id}/equipment`
- `POST /api/items/{id}/equipment`，body：`{"name": "治理设备A"}`
- `POST /api/items/{id}/equipment/{eid}/start`
- `POST /api/items/{id}/equipment/{eid}/stop`

检查记录更正：

- `POST /api/items/{id}/records/{rid}/correct`，body：`{"detail": "更正后的内容"}`

续期（影响清单）：

- `GET /api/items/{id}/renewals`
- `POST /api/items/{id}/renewals`，body：`{"application_no": "可选，不填则服务端生成"}`
- `GET /api/renewals/{application_no}`，返回续期单与当前材料
- `GET /api/renewals/{application_no}/resume`，返回续期单、材料、版本历史和环境变更事件（用于失败后按申请号恢复续传）
- `PUT /api/renewals/{application_no}/materials`，body：`{"expected_version": 1, "materials": [{"name": "报告", "content": "..."}]}`
- `POST /api/renewals/{application_no}/submit`，body：`{"expected_version": 2}`
- `POST /api/renewals/{application_no}/approve`，body：`{"expected_version": 3, "conclusion": "同意"}`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。设备启停和续期提交/补交由applicant执行，检查记录更正由inspector执行，续期审批由compliance_manager执行。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
