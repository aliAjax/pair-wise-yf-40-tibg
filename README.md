# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点；`isolation_order`：阳性批次隔离处置单。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/isolation/dashboard`：棚位占用、待复查数量、待处置批次和处置记录。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 隔离处置单

- 管理员通过`POST /api/isolation_order`一次登记多个阳性（已隔离检疫）批次，每个批次需给出`consignment_id`、`bay`（棚位）、`disinfection`（消杀方式）和`recheck_date`（复查日期）。
- 任一棚位与在途处置单冲突、或单内棚位重复时，整张单不保存，错误信息会指出冲突批次；携带`Idempotency-Key`重发同一处置单会取回第一次的结果。
- 通过`POST /api/entities/<id>/actions`提交`recheck`动作（`{"consignment_id":...,"passed":true|false}`）：合格只释放棚位，不改动批次本身；不合格的批次回到待处置，可再次登记处置单。单内所有批次复查完毕后处置单自动结案。
- 缺少隔离信息的旧批次仍可通过`GET /api/consignment`按原清单查询，并计入看板的待处置批次。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
