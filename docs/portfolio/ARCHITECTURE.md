# 从业务问题读到领域实现

Foundation 是模块化单服务。模块边界表达职责和写入权，不要求每个模块单独部署。

## 1. 先看整体位置

[整体图](atlas/index.html#overview) → [内部结构](atlas/index.html#foundation-structure)。产品负责业务输入与输出的含义；Foundation 负责公共资源的生命周期、授权与受管操作。HTTP / SDK 是薄适配，Host 是唯一依赖装配处。

## 2. 资料如何进入 Graph

[Evidence / Graph 结构](atlas/index.html#evidence-graph-structure) → [写入](atlas/index.html#write) → [对账](atlas/index.html#graph-recovery)。

阅读顺序：

1. [Graph HTTP 适配](../../backend/agent_foundation/adapters/inbound/graph_http.py)接收结构化命令。
2. [应用服务](../../backend/agent_foundation/modules/graph/application.py)执行授权、政策、来源和版本检查。
3. [窄端口](../../backend/agent_foundation/modules/graph/ports.py)区分提案控制、图写权威、读取及候选来源。
4. [SQLite 实现](../../backend/agent_foundation/adapters/outbound/sqlite_graph_v2.py)保存权威变化与收据。
5. [Graph 权威回归](../../backend/tests/test_architecture_pl14_graph_authority.py)核对异常候选、版本与控制回填故障。

事务保证位于图写权威内部。跨库的后续记账依靠稳定操作 ID 和原写收据，不宣称分布式数据库的统一原子提交。

## 3. 为什么候选还要复核

[当前性读取](atlas/index.html#retrieval) → [来源变化](atlas/index.html#change)。候选索引和算法可以落后于权威状态。Evidence 撤销后，即使索引仍有高分内容，领域仍应阻止它作为当前结果返回。

对照 [Evidence 应用服务](../../backend/agent_foundation/modules/evidence/application.py)、[候选适配](../../backend/agent_foundation/adapters/outbound/projected_candidates_v2.py)及 [Graph 查询](../../backend/agent_foundation/modules/graph/application.py)。

## 4. 经验如何持续使用

[Memory 结构](atlas/index.html#memory-structure) → [经验形成与使用](atlas/index.html#memory-context) → [版本演进](atlas/index.html#memory-evolution)。纠错形成 successor，使新任务读取当前经验；历史记录与普通检索资格分别表达。

领域合同在 [Memory contracts](../../backend/agent_foundation/modules/memory/contracts.py)，规则在 [Memory application](../../backend/agent_foundation/modules/memory/application.py)。Context 只组合声明的数据源，不接管领域写入。

## 5. 如何验证模块独立

[Host 与存储](atlas/index.html#host-storage) → [物理边界](atlas/index.html#physical-boundaries)。

- [HostConfiguration](../../backend/agent_foundation/host/configuration.py)声明启用模块和角色连接。
- [minimal.py](../../backend/agent_foundation/host/minimal.py)选择适配器并注入领域端口。
- [装配矩阵测试](../../backend/tests/test_architecture_pl09_assembly_matrix.py)检查关闭模块不创建其设施。
- [公共路由目录](../../public_contracts/foundation-v1-routes.json)与双 SDK 保持对应。

## 图谱维护

公开图谱复用原有面试图谱中的 20 个结构与机制视图，保留稳定 ID、层级、主体交接和返回。仅携带当前源码可定位的引用；原完整图谱、历史案例和内部原记录继续留在原工作区。

```powershell
node ./docs/portfolio/atlas/build.mjs
```

用于 GitHub Pages 时，加 `--repository owner/repository`，将生成页面中的源码引用转向相应 GitHub 仓库；维护 JSON 仍保留相对路径。

无需 npm 安装或本机 Archify 路径。`atlas/atlas.json` 是维护源，`atlas-layout.mjs` 维护固定侧栏；`vendor/archify` 保存所需渲染器及其 MIT 许可。构建检查结构和几何，不能代替实际浏览器体验核对。
