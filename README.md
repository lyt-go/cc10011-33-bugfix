# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 订阅推送投递链

订阅推送把「订阅配置 → 待推送分析数据 → 洞察快照 → 订阅推送数据记录」串成一条链，
告警恢复和用户洞察发布是两个上游触发点。

| 环节 | 存放位置 | 关键约束 |
| --- | --- | --- |
| 订阅配置 | `subscription` | 保留接收人、推送渠道、静默时段、下次推送时间；状态只能 草稿→评审中→已启用↔已暂停 |
| 待推送分析数据 | `pendinganalysis` | 非实时频率的新内容先登记，调度时按频率窗口合并 |
| 洞察快照 | `userinsightsnapshot` | 每版发布生成不可变快照，消息正文引用快照摘要 |
| 订阅推送数据记录 | `subscriptionpush` | 唯一键 = 内容版本 + 接收人 + 推送渠道；每条带静默判定与下次推送时间 |

关键规则：

- **编辑不清空**：`PATCH /api/subscription/{id}` 按字段合并，显式提交空值的
  接收人/推送渠道/静默时段/下次推送时间会保留原值并记入 `保留字段`。
- **暂停不投递**：暂停后调度、告警恢复、洞察同步都不会新增投递记录；启用（恢复）
  只重算下次推送时间，不补发暂停期间的消息。
- **只投一次**：同一内容版本 + 接收人 + 渠道并发触发时，仓库层唯一键保证只创建一条，
  其余触发合并到已有记录并累计 `重复触发次数`。
- **静默判定**：同时按接收人本地时区与渠道统一时区判断跨天时段，任一命中即静默；
  两时区结论冲突时按一个规则投递，并把冲突与判定依据写进记录。
- **可追溯**：`GET /api/subscription/trace` 把配置、快照、静默判定、下次推送时间串起来。
- **可恢复**：`APP_STORE_FILE` 开启文件持久化，先写临时文件再原子替换并保留 `.bak`；
  写整批数据时先构造后替换，中断不会覆盖上一份可用数据集。

调度默认关闭，需要时打开：

```bash
# 后台轮询（秒）
APP_SCHEDULER_ENABLED=1 APP_SCHEDULER_INTERVAL=60 ./run.sh
# 或手动触发一轮
curl -X POST http://127.0.0.1:8000/api/subscription/schedule
```

归属控制：请求头 `X-Operator` 传入当前用户（中文需 URL 编码）。共享渠道对非本人只读，
越权修改返回 403；`值班管理员`/`admin` 可维护全部订阅。

测试：

```bash
cd backend && .venv/bin/python -m unittest discover -s tests -v
```

领域：数据工程
子方向：数据分析与数据应用
