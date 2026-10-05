# 矿山安全监测管理平台

面向矿山井下环境监测、瓦斯治理、顶板管理、通风系统与人员定位的一体化矿山安全监测管理后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
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
| 矿区台账 | `minearea` | 矿区 | 矿区编号、矿区名称、开采矿种 |
| 瓦斯监测 | `gas` | 瓦斯测点 | 测点编号、所在区域、瓦斯浓度 |
| 通风系统 | `ventilation` | 通风设备 | 设备编号、设备类型、额定风量 |
| 顶板管理 | `roof` | 顶板监测 | 监测编号、所在工作面、离层量 |
| 水害防治 | `waterhazard` | 水文监测 | 监测编号、所在区域、涌水量 |
| 冲击地压 | `rockburst` | 微震监测 | 监测编号、所在区域、微震能量 |
| 人员定位 | `personnel` | 定位终端 | 终端编号、携带人员、所在位置 |
| 粉尘防治 | `dust` | 粉尘测点 | 测点编号、所在区域、粉尘浓度 |
| 防灭火 | `fireprevent` | 防火监测 | 监测编号、所在区域、束管监测 |
| 皮带运输 | `belt` | 运输皮带 | 皮带编号、所属巷道、运输长度 |
| 提升系统 | `hoist` | 提升机 | 提升机编号、提升类型、提升高度 |
| 供电系统 | `power` | 供电设备 | 设备编号、设备类型、电压等级 |
| 应急救援 | `rescue` | 救援装备 | 装备编号、装备名称、装备类别 |
| 安全培训 | `training` | 培训记录 | 培训编号、培训主题、培训对象 |
| 入井管理 | `shift` | 入井记录 | 记录编号、入井人员、所属班组 |
| 爆破管理 | `explosive` | 爆破记录 | 爆破编号、爆破区域、炸药用量 |
| 巷道维修 | `roadway` | 维修任务 | 任务编号、维修巷道、维修内容 |
| 监测分站 | `monitorstation` | 监测分站 | 分站编号、分站名称、所在位置 |
| 持证管理 | `certificate` | 持证人员 | 人员编号、姓名、证书类别 |
| 应急演练 | `emergencydrill` | 演练记录 | 演练编号、演练主题、演练区域 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

### 监测分站台账导入导出

监测分站的通信地址与接入传感器清单从设备台账导出再导入，口径统一如下：

- `GET /api/monitorstation/template` 下载导入模板，表头顺序与设备台账明细一致：
  分站编号、分站名称、所在位置、通信地址、接入传感器、信号强度、后备电源、分站状态。
- `POST /api/monitorstation/import` 导入 CSV/JSON 文件（`{ filename, content }`）：
  - 先做文件级校验（空文件、表头不符、JSON 非法整批不落库，返回 422 并说明原因）；
  - 行级校验不通过（缺必填、通信地址与接入传感器数量不符）只退回该行，其余行照常入账；
  - 分站编号或分站名称重复即同一分站：库内已存在则跳过，文件内重复只留第一条；
  - 响应为逐行回执 `{ total, imported, skipped, rejected, rows, message }`，
    `rows` 逐行给出文件行号与「入账/跳过/退回」原因。
- `GET /api/monitorstation/export` 导出 CSV，字段顺序与模板一致，导出再导入内容不变。
- 列表与详情都取自同一次落库的记录（`MonitorstationService._view` 统一口径）。
- `GET /api/monitorstation/link-report` 为链路运行报表：分站状态变化即时同步，
  存量分站在服务启动时按「接入时间」回填。
