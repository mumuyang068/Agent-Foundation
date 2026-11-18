# 运行与截图

## 可以复现什么

按仓库首页的快速运行启动新服务，再执行 `examples/portfolio/graph_roundtrip.py`。默认只使用 SQLite，图控制与写入分文件；无需产品服务或模型。

脚本完成真实 HTTP 往返：创建短期会话、登记 Evidence、提出 Graph 变化、拒绝未授权审核、由独立主体审核和执行、幂等重放，再撤销来源并核对查询结果。收据不包含原始凭据。

这条示例服务于理解公共合同。它不启动 TraceWise 或 TeachFlow，两个产品仍有各自的登录、项目权限、业务处理和页面。

本次实际结果和截图摘要见 [运行记录](evidence/verification.json)，完整合成业务收据见 [graph-roundtrip.json](evidence/graph-roundtrip.json)。分析员审核返回 `403 / grant_scope_denied`；独立审核者完成应用，同操作返回相同收据；撤销来源前后，Alpha 的普通查询命中数为 `1 → 0`。

## 截图索引

| 图片 | 内容与范围 |
| --- | --- |
| [Foundation API](images/foundation-api.jpg) | 2026-11-18 普通安装包在隔离环境实际运行的服务 API；Access / Evidence / Graph 启用 |
| [TraceWise 采用](images/tracewise-evidence-graph.png) | 2026-09-16 隔离运行留存的真实产品页面，演示资料与精确字符定位；不作为当前包完整验收 |

## 运行约定

- `prepare.py` 仅创建新目录，拒绝覆盖既有数据。
- 同一服务内重跑业务脚本会创建新的演示 scope；收据文件名也应使用新名字。
- bootstrap token 从环境变量读取，仅用于初始化；普通领域操作使用逐主体会话。
- 停止服务后 SQLite 数据保留。不要提交 `.demo/`、token 文件或数据库。

## 采用方如何使用

Python 安装 `sdk/foundation-python` 后可使用 `FoundationClient`。TypeScript 客户端从 `sdk/typescript` 构建；两个客户端只处理 HTTP 和公共合同，不携带 Foundation 服务实现。

TraceWise 负责资料解析、项目权限、审核者选择和原文展示；Foundation 保存 Evidence 版本与受管 Graph 变化。教学场景的课程判断和工具仍属于产品，Memory / Context 用于长期经验和单次上下文。
