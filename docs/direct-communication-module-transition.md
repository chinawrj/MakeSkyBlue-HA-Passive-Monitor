# 从被动监听器切换为直接通信模块

本文是切换准备文件，不是启用 TX 的授权。当前已发布配置仍是严格 RX-only：
GPIO1/GPIO2 都是浮空输入，没有 `tx_pin`，也没有 UART write。只有在原 Wi-Fi
模块已经物理移除、接线复核完成、单独的 direct-role YAML 已发布并通过审计后，
AtomS3 才能从监听器变成主动通信模块。

## 1. 项目和角色命名

角色中立的项目族名称使用 **MakeSkyBlue Local Link**。两个角色必须始终显示在
文件名、HA 设备信息和日志中：

| 角色 | 建议名称 | 总线能力 | 当前状态 |
| --- | --- | --- | --- |
| `passive_monitor_rx_only` | Passive Monitor | G1/G2 仅接收 | 已实现、默认 |
| `direct_readonly_bridge` | Direct Read-Only Bridge | G1 发送 FC03；G2 接收响应 | 尚未实现 |

这里的 “Read-Only Bridge” 表示不写逆变器寄存器。它仍必须在物理线路上发送
FC03 读请求，所以绝不能和“电气 RX-only”混为一谈。

仓库现有名称保留被动监听阶段的证据血缘。角色中立入口是
`makeskyblue-local-link.yaml`；目前它只能 include
`makeskyblue-modbus-monitor.yaml`。未来的主动角色必须使用新的独立文件，例如
`makeskyblue-direct-readonly.yaml`，不能向当前 monitor YAML 塞入一个容易误开的
`tx_pin` 开关。

## 2. 为什么不用一个布尔 TX 开关

ESPHome substitutions 只是文本替换，不能作为可靠的硬件安全边界。如果同一份
YAML 同时包含 RX 和 TX 路径，即使默认关闭，也可能因本地 substitution、package
合并或重构而意外启用。因此采用两个独立的顶层配置：

```yaml
# 当前 makeskyblue-local-link.yaml
packages:
  selected_role: !include makeskyblue-modbus-monitor.yaml
```

未来切换必须是一次可审计的源码变更、commit 和 prerelease；不得仅在未跟踪的
secrets 文件里偷偷切换。CI 要分别检查生成的 `main.cpp`：

- passive role 必须恰好有 GPIO1/GPIO2 两个 `set_rx_pin`，且没有 `set_tx_pin`、
  `uart_write`、`write_array` 或 `.write(`；
- direct role 必须只有预期的 G1 TX 和 G2 RX，且构建需要显式的物理切换确认；
- 两个角色的固件文件名、项目版本和 SHA-256 必须不同。

## 3. 当前接线事实和未来引脚角色

当前实测为 3.3 V TTL、9600 baud、8 data bits、no parity、1 stop bit，并与
AtomS3 共地：

| 线路 | 被动模式 | 原模块移除后的候选角色 |
| --- | --- | --- |
| G1 / GPIO1 | RX，观察 Wi-Fi → inverter | TX，AtomS3 → inverter RX |
| G2 / GPIO2 | RX，观察 inverter → Wi-Fi | RX，inverter TX → AtomS3 |

G1 变成 TX 前必须逐项满足：

1. 原 Wi-Fi 模块已从 TX/RX 线路物理拔除，不能只在软件中停用；
2. 断电状态下复核 G1 确实通往逆变器 RX，G2 确实来自逆变器 TX；
3. 上电后复核高电平不超过 3.3 V，不接 5 V TTL、RS232 或 RS485 A/B；
4. 共地可靠，线路上不存在第二个发送器；
5. 先使用限流/串联保护和示波器或逻辑分析仪确认波形；
6. 主动固件必须来自已经发布且 CI 通过的 tag。

任何一项不成立，就继续使用 passive role。

## 4. 已收集的原 Wi-Fi 模块行为

所有结论来自本项目 clean-room 双向抓包。Modbus RTU 地址是帧首字节，当前所有
结构正确的请求/响应均使用地址 `1`。D 地址是寄存器地址，不是通信地址。

### 4.1 固定读轮询

Wi-Fi 模块按下列顺序循环发送 FC03：

| 顺序 | 请求 hex | 地址范围 | word 数 |
| ---: | --- | --- | ---: |
| 1 | `01 03 00 00 00 3D 84 1B` | D0–D60 | 61 |
| 2 | `01 03 00 64 00 33 44 00` | D100–D150 | 51 |
| 3 | `01 03 00 C9 00 10 94 38` | D201–D216 | 16 |
| 4 | `01 03 00 97 00 32 75 F3` | D151–D200 | 50 |

在一段 21 分钟样本中，每个范围约读取 178–179 次。正常完整轮询周期中位数约
3.08 秒；同一轮相邻请求的间隔通常约 0.3–0.9 秒。观察到的最长 UART 静默约
30.4 秒，因此替代模块不能把单次 30 秒空档直接判成设备掉线。

响应完成延迟的样本中位数约为：D0 段 158 ms、D100 段 132 ms、D201 段
60 ms、D151 段 134 ms。首版 direct role 应使用明确超时、有限次数重试和整轮
退避，不能在超时后高速重发。

### 4.2 原模块的写事务

抓包还观察到：

- FC10 写 D30–D31，用于同步打包网络时间；通常三次一组，约每五分钟重复；
- 设备特有的 FC06 写 D10/D11，响应并不按标准 FC06 回显请求值。

这些写事务必须继续保留为协议证据，但 **direct_readonly_bridge 首版全部禁用**。
首版只发送上述四个 FC03 请求，不发送 FC06、FC10 或广播，也不提供 HA 写服务。
不做 D30–D31 校时不会阻止读取遥测；如果以后确需校时或控制，必须另做受控实验、
独立许可开关、独立 release 和读回验证。

### 4.3 启动行为

多次重启观测表明，原模块联网后会发送 D30–D31 时间同步，然后恢复四段 FC03
轮询。一次出现过无 CRC 的 `00 00 00 FF`，其他重启没有出现，所以它只是可选
标记，不是握手必需条件。完整时间线和打包时间格式见
`docs/wifi-module-startup-sequence.md`。

替代模块不应等待该 magic，也不需要模拟云端登录。它在网络尚未连接时即可启动
本地 FC03 轮询，并把结果先保存在本地状态/ring 中；Wi-Fi、WireGuard 和 HA
连接只影响上报，不应阻塞 UART 初始化。

## 5. direct role 的最小状态机

以下是下一阶段的实现约束，不是当前固件行为：

1. 初始化 G2 RX、解析器、请求队列和本地缓存；
2. 只有通过物理切换确认的 direct 固件才初始化 G1 TX；
3. 依次发送四个 FC03 请求，每次等待地址/function/byte-count/CRC 都匹配的响应；
4. 更新与当前被动解析器相同的 D0–D216 缓存和 HA sensors；
5. 超时记录 raw 请求、等待上下文和计数，有限重试后退避；
6. Wi-Fi/HA 断线时继续本地轮询并缓存诊断；
7. 不实现 FC06、FC10、线圈写、广播或任意透传写接口。

主动请求也需要一个独立的 `uint32` transaction sequence；它不能替代捕获 chunk
sequence。日志必须同时保存发送 hex、接收 hex、时间、地址、功能码、请求序号、
响应配对和超时结果。

## 6. 切换前必须保留的数据

在拔除原 Wi-Fi 模块前，至少保存：

- 一个完整正常轮询窗口，覆盖全部 178 个 D 地址；
- 至少一次由操作者明确记录时刻的 Wi-Fi 模块重启；
- FC03 四段请求的周期、顺序、响应延迟和超时分布；
- 所有 FC06/FC10 原始 hex、方向、时间、sequence、响应及后续 FC03 读回；
- UART 静默区间、CRC/discarded/residual、sequence gap 和 log reorder；
- 同刻 IoTRix UI 只读快照，用于确认字段而不是复制 APK 资源；
- 当前 passive firmware 自报版本、配置 SHA、固件 SHA（如已留存）和 boot ID。

若旧固件二进制没有留存，报告必须写 `missing/not preserved`，不能拿当前工作区
里尚未刷入的二进制哈希冒充设备固件。

## 7. 切换执行清单

1. 停止并封存最后一条 pre-cutover 日志，生成 JSON 摘要和 SHA-256；
2. 记录逆变器、Wi-Fi 模块、AtomS3 的供电状态和接线照片；
3. 整机断电，物理拔除原 Wi-Fi 模块；
4. 万用表/示波器复核 G1/G2 方向、电压和共地；
5. 发布 direct-role prerelease，等待 CI 和生成代码 TX/RX 审计通过；
6. 首次上电只运行 FC03，观察首个请求和响应后再允许后续轮询；
7. 比对被动阶段基线、HA sensors 和逆变器面板，不写任何设置；
8. 从该 direct-role 固件上线时刻重新开始独立 24 小时窗口；
9. 每 30 分钟检查在线、序号、CRC、超时、heap、缓冲区和 178 地址覆盖。

如果 direct role 不能稳定取得响应，应断电回滚接线或刷回 passive 固件；不得通过
发送探测写命令来猜协议。
