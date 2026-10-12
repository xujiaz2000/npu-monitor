# NPU 占用监控看板 (npu_monitor)

持续监控多台华为昇腾 (Atlas) 服务器的 NPU 占用情况：可视化看板 + 桌面悬浮小窗 + **空闲桌面弹窗提醒**。

单文件 Python 应用，仅使用标准库（tkinter + ssh），**无需 pip 安装任何第三方包**。

## 功能特性

- **并行持续监控**：每台服务器独立线程轮询 `npu-smi info`（默认 15s，可调），互不阻塞，超时/失败不会卡死
- **简洁可视化**：状态摘要 + 每卡占用方块（Core%、HBM 进度条、温度着色）+ Core 利用率迷你趋势图——不展示原始报表
- **桌面悬浮小窗（置顶）**：每台服务器一行小方块，随时瞟一眼全部机器的占用状态；可拖动、右键菜单关闭、位置记忆
- **空闲弹窗提醒**：显存/算力阈值 + 连续 N 轮确认 → Win11 风格桌面弹窗 + 提示音；同类事件自动合并（如多台同时断连只弹一条）
- **服务器管理**：添加 / 编辑 / 删除 / 测试连接，阈值可按服务器覆盖；**零默认配置**——首次启动自动读取 VSCode Remote-SSH / OpenSSH 的 SSH 配置
- **轻量**：单文件约 90KB，双击即用

## 环境要求

| 依赖 | 说明 |
|---|---|
| Windows 10 / 11 | 深色标题栏与圆角在 Win11 上效果最佳 |
| Python 3.8+ | 仅标准库（tkinter） |
| OpenSSH 客户端 | Windows 自带 `ssh.exe`（设置 → 应用 → 可选功能） |
| 免密登录 | 本机公钥已配置到目标服务器的 `authorized_keys` |
| npu-smi | 目标服务器已安装昇腾驱动（`npu-smi info` 可用） |

## 快速开始

```bat
:: 方式一：双击启动（无黑窗口）
npu_monitor.bat

:: 方式二：命令行
python npu_monitor.py

:: 演示模式（合成数据，不连真实服务器，验证 UI/弹窗效果）
python npu_monitor.py --demo
```

首次启动会自动从 **VSCode Remote-SSH / `~/.ssh/config`** 导入主机列表（`Host / HostName / User / Port`，跳过通配条目，上限 24 台）；也可以点「⇩ 导入SSH配置」随时重新导入，或「＋ 添加服务器」手工添加。

## 界面说明

### 主看板

- 每台服务器一张卡片（**不可达/未启用的自动沉到列表末尾**，恢复后自动回原位）：

- 标题行：状态点（●绿=空闲 / 橙=占用 / 红=不可达 / 灰=未启用）、空闲计时、行内「测试 / 编辑 / 删除」
- 指标行：卡数、Core 均值、HBM 已用/总量、最高温度、总功耗、上次刷新耗时
- 每卡小方块：`P编号` + 温度（≥70° 标红）+ AICore% + HBM 进度条；**整格一个状态色**，点击查看单卡详情
- 卡片右侧：Core 利用率迷你趋势图（最近 60 次采样）

### 悬浮小窗（置顶）

工具栏「▦ 悬浮窗」开关。无边框置顶小组件，始终可见，可整窗拖动；每台服务器一行，小方块颜色与主界面一致。右键弹出深色菜单 →「关闭悬浮窗」（点击菜单外收起菜单；右上角 ✕ 也可直接关闭）。位置与开关状态记忆在配置中。

### 桌面弹窗（置顶）

- 停靠**主窗口所在屏的桌面右下角**（自动避开任务栏），Win11 风格：左侧事件色条 + 圆角
- **同类事件自动合并**：多台同时连不上只弹一条「连接失败 · N 台」，正文最多两台明细 + 「…等 x 台」
- 从右下角向上堆叠、互不重叠（最多 4 条，超出移除最旧，关闭后自动补位）
- 弹窗按钮：「静默 30 分钟」（对弹窗内所有服务器生效）/「知道了」

## 空闲判定与提醒

```
占用中：任一卡  Core > T%  或  HBM 已用 > T GB     （T=0 表示只要非 0 即占用）
空  闲：全部卡都不满足占用条件，且连续 N 轮采样
提  醒：确认空闲后弹窗 + 提示音；可选按间隔重复提醒、恢复占用提醒、连接失败提醒
```

> 经验值：Atlas 910 空闲时 HBM 基线约 3GB；模型驻留显存但无计算的卡（如推理服务空载）会判为「占用」。集群基线不同时在设置里调整即可。所有阈值支持全局默认 + 按服务器覆盖。

## 配置文件 `npu_monitor.json`

首次运行自动生成（含导入的服务器），可手工编辑：

```json
{
  "interval": 15,
  "connect_timeout": 5,
  "exec_timeout": 25,
  "sound": true,
  "alert_idle": true,
  "alert_recover": false,
  "alert_fail": true,
  "renotify_min": 30,
  "defaults": { "idle_core": 5, "idle_hbm_gb": 8, "idle_cycles": 3 },
  "servers": [
    { "id": "s1", "label": "A3-174", "host": "192.168.13.174",
      "user": "root", "port": 22, "enabled": true,
      "idle_core": "", "idle_hbm_gb": "", "idle_cycles": "" }
  ],
  "ui": { "geometry": "...", "mini": { "visible": false, "geometry": "+x+y" } }
}
```

| 字段 | 说明 |
|---|---|
| `interval` | 轮询间隔（秒） |
| `connect_timeout` / `exec_timeout` | SSH 连接 / 远端执行超时（秒） |
| `defaults.idle_core` | 判占用：Core 利用率阈值（%） |
| `defaults.idle_hbm_gb` | 判占用：HBM 已用阈值（GB） |
| `defaults.idle_cycles` | 确认空闲需连续采样轮数 |
| `renotify_min` | 空闲持续期间重复提醒间隔（分钟，0=只提醒一次） |
| `servers[].idle_*` | 按服务器覆盖阈值（留空 = 用全局默认） |
| `ui.mini` | 悬浮小窗位置与开关状态 |

## 目录结构

```
├── npu_monitor.py        # 主程序（单文件应用）
├── npu_monitor.bat       # Windows 双击启动器（pythonw 无黑窗口）
├── README.md             # 本文档
├── LICENSE               # MIT
├── .gitignore            # 排除运行时生成的配置与日志
└── tools/
    ├── check_npu.ps1     # 旧版命令行一次性检查工具（PowerShell）
    └── check_npu.bat     # 旧版工具启动器
```

运行时在脚本同目录生成：`npu_monitor.json`（个人配置）、`monitor_events.log`（事件日志）——两者均已被 `.gitignore` 排除。

## 自检命令

```bash
python npu_monitor.py --selftest   # 解析器自检（内置真实 npu-smi 样例）
python npu_monitor.py --smoke      # 构建 UI + 弹窗 + 悬浮窗，2.6 秒自动退出
```

## 常见问题

| 现象 | 处理 |
|---|---|
| 卡片显示「不可达： 免密登录失败」 | `ssh user@host` 手工验证；将本机 `~/.ssh/id_*.pub` 追加到服务器 `~/.ssh/authorized_keys` |
| 「缺少 npu-smi 命令」 | 服务器未装驱动或不在 PATH；确认 `npu-smi info` 可用 |
| 解析到 0 张卡 | 不同 npu-smi 版本表格差异，提 issue 并附 `npu-smi info` 输出 |
| 显存满却显示「空闲」 | 阈值过宽：调低 `idle_hbm_gb`，或在服务器条目里单独覆盖 |
| 推理服务空载一直「占用」 | 属预期（显存被驻留）；如只想按算力判空闲，将 `idle_hbm_gb` 覆盖为 0 并调高 `idle_core` |
| 弹窗/悬浮窗位置不对 | 多显示器下自动跟随主窗口所在屏；删除 `npu_monitor.json` 可重置全部 UI 状态 |

## License

[MIT](LICENSE)
