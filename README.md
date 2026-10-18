# 基于大模型工具调用的网络故障诊断系统

用 Python 实现的轻量级网络运维工具：定时巡检主机的连通性、端口、网页和磁盘，基于历史基线做动态阈值告警；告警触发后，由大语言模型（DeepSeek）通过**工具调用**自主完成多步排查，给出根因和处置建议。

项目的重点在于**评测**：通过故障注入构造 11 类场景，把 AI 诊断与两种规则方法做三方对比，并通过错误分析和对照实验改进了 AI 诊断。

## 评测结果

11 类场景 × 3 轮，共 33 次。诊断出的故障类别集合与实际**完全一致**才算正确（复合故障漏报一个、单一故障多报一个都算错）。

| 场景组 | 次数 | 简单规则 | 专家规则 | AI 诊断 |
|---|---|---|---|---|
| 无故障对照 | 3 | 100% | 100% | 100% |
| 已知单一故障 | 12 | 75% | 100% | 100% |
| 易混淆故障 | 9 | 0% | 0% | **89%** |
| 复合故障 | 9 | 0% | 67% | **100%** |
| **总计** | **33** | **36%** | **64%** | **97%** |

AI 平均排查约 14 次工具调用，平均耗时约 11 秒。

**三种诊断方法：**
- **简单规则**：只看告警指标类型，例如"端口不通 → 服务停止"。
- **专家规则**：模拟运维人员手写的决策树，利用连接失败类型（timeout → 防火墙，refused → 服务停止），并能同时报出多个故障。
- **AI 诊断**：大模型自主调用工具取证后下结论。

**结论：**
- 对**规则编写时考虑过的故障**，专家规则已经足够好（100%）。
- AI 的优势在**现象相同、原因不同的易混淆故障**：例如防火墙 REJECT、监听端口配置错误，表现都是端口 refused，和服务停止一模一样。规则只能看到现象，AI 会进一步检查服务状态、监听端口、防火墙规则和目录权限来区分。
- **唯一的错误**：config_error 有一轮被判为 `other`。AI 在"配置错误"和"其他"这两个类别之间的分类边界不够稳定。

## 错误分析与改进：AI 把历史日志当成了当前证据

早期版本中，AI 在复合故障上的准确率只有 33%，低于专家规则。所有错误都是同一个模式：**没有注入权限故障，AI 却多报了 `permission_error`**。

排查 AI 的工具调用记录后发现：nginx 错误日志会保留历史记录，前几轮权限故障场景留下的 `Permission denied` 一直残留在日志里，AI 读日志时把这些旧记录当成了当前故障的证据。

**改进**：日志工具不再直接 `tail`，而是只返回"上次巡检全部正常"之后的记录，也就是本次异常开始以后的日志。

**对照实验**（易混淆 + 复合故障，各 9 次；两次实验提示词完全相同，只切换 `config.yaml` 中的 `log_filter` 开关）：

| | 易混淆故障 | 复合故障 | 合计 |
|---|---|---|---|
| 关闭日志过滤 | 44% | 44% | 44% |
| 开启日志过滤 | **100%** | **100%** | **100%** |

关闭过滤时 10 次错误全部是多报 `permission_error`。两次实验的提示词中都包含"每个类别都要有当前状态的直接证据"这条约束，说明**仅靠提示词提醒不足以纠正问题，在工具层控制 AI 能看到的信息才可靠**。

## 故障场景

| 组 | 场景 | 注入方式 | 告警现象 |
|---|---|---|---|
| 对照 | none | 不注入 | 无 |
| 已知单一 | service_down | `service nginx stop` | 端口 refused |
| 已知单一 | firewall_block | iptables DROP 80 端口 | 端口 timeout |
| 已知单一 | network_latency | tc netem 增加 300ms 时延 | 时延超过动态阈值 |
| 已知单一 | disk_full | 写满 100MB 测试盘 | 磁盘使用率 95% |
| 易混淆 | firewall_reject | iptables REJECT 80 端口 | 端口 refused（同服务停止） |
| 易混淆 | config_error | nginx 改为监听 8081 | 端口 refused（同服务停止） |
| 易混淆 | permission_error | 网站目录权限改为 000 | 网页返回 403 |
| 复合 | service_down + disk_full | 两者同时注入 | 端口 refused + 磁盘告警 |
| 复合 | firewall_block + network_latency | 两者同时注入 | 端口 timeout + 时延告警 |
| 复合 | permission_error + network_latency | 两者同时注入 | 403 + 时延告警 |

## 功能

| 模块 | 说明 |
|---|---|
| 自动巡检 | ping 时延/丢包、TCP 端口、HTTP 状态与响应时间、磁盘使用率 |
| 数据存储 | SQLite 保存全部历史数据 |
| 动态阈值告警 | 时延阈值 = 历史均值 + max(3σ, 最小增量)，异常数据不参与基线计算 |
| 告警推送 | 企业微信 / 钉钉 / 飞书群机器人 |
| AI 故障诊断 | 大模型可调用 ping、端口检测、白名单只读命令、历史数据查询等工具，按"网络层 → 传输层 → 应用层 → 系统资源"逐步排查，支持识别多个并发故障 |
| 安全控制 | AI 只能执行白名单内的只读命令，拒绝其他任何命令 |
| 故障注入评测 | 11 类场景自动注入、诊断、恢复，并在每轮恢复后做验证巡检 |
| 可视化看板 | Streamlit 网页：状态、时延趋势、告警、AI 排查过程、评测结果、中文问答 |

## 架构

```
 巡检(inspector) ──► 告警判断(alert) ──► 推送(notify)
       │                  │
       ▼                  ▼
   SQLite(db) ◄──── AI 诊断(diagnose) ──► 工具：ping / 端口 / 白名单命令 / 日志 / 历史查询
       │
       ▼
   看板(dashboard)        评测(evaluate) + 故障注入(fault_inject)
```

## 快速开始

### 1. 准备 Linux 环境

**Windows**：以管理员身份打开 PowerShell，执行 `wsl --install`，重启后按提示设置 Ubuntu 的用户名和密码。

**Mac**：故障注入需要 Linux 的 iptables / tc，建议用 Multipass 开一台 Ubuntu 虚拟机：`brew install multipass && multipass launch --name lab && multipass shell lab`。

### 2. 安装

```bash
cd ~/net-inspector
bash setup.sh
```

### 3. 配置大模型 API Key

```bash
echo 'export DEEPSEEK_API_KEY=sk-你的key' >> ~/.bashrc
source ~/.bashrc
```

### 4. 运行

```bash
./venv/bin/python main.py --once        # 巡检一次
./venv/bin/python main.py --ai          # 持续巡检 + 告警时 AI 自动诊断
./venv/bin/streamlit run dashboard.py   # 看板：浏览器访问 http://localhost:8501
```

### 5. 故障注入评测

```bash
sudo ./venv/bin/python fault_inject.py list                                  # 查看所有场景
sudo DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY ./venv/bin/python evaluate.py --rounds 3
sudo ./venv/bin/python evaluate.py --no-ai                                   # 只测规则方法
sudo DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY ./venv/bin/python evaluate.py --groups confuse compound
```

## 局限性

- 场景是在单机实验环境中人为构造的，比真实网络环境简单得多。
- 每个场景只测了 3 轮，样本量有限。
- 提示词中写入了部分运维经验（如 refused / timeout 的含义），AI 的表现部分依赖这些领域知识。
- 专家规则只覆盖了已知故障；如果为易混淆故障补写规则，规则方法同样可以判对，代价是每出现一种新故障模式都需要人工维护。

## 常见问题

- **磁盘故障注入失败**：测试盘在 WSL 重启后会失效，重新执行 `bash setup.sh`。
- **`network_latency` 注入失败**：当前内核不支持 tc netem，评测会自动跳过该项。
- **iptables 报错**：WSL 中可执行 `sudo update-alternatives --set iptables /usr/sbin/iptables-legacy` 后重试。
- **模型名报错**：以 DeepSeek 官方文档当前的模型名为准，修改 `config.yaml` 中的 `llm.model`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `config.yaml` | 所有配置：巡检目标、阈值、推送、大模型、日志过滤开关 |
| `inspector.py` | 巡检检测函数 |
| `alert.py` | 固定阈值与动态阈值告警 |
| `db.py` | SQLite 数据库读写 |
| `notify.py` | 群机器人推送 |
| `diagnose.py` | AI 工具调用诊断、日志过滤、两种规则对照方法 |
| `main.py` | 巡检主程序 |
| `fault_inject.py` | 故障场景定义与注入/恢复 |
| `evaluate.py` | 自动化评测与三方对比 |
| `dashboard.py` | 可视化看板 |
