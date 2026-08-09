# 从零复现 MakeSkyBlue 双向 UART/Modbus 被动监控

本文按实际操作顺序说明如何在另一套 AtomS3、逆变器、Wi-Fi 模块和 Home
Assistant 环境中重复本项目。完成后应能：

- 在 G1/G2 同时观察 Wi-Fi 模块请求与逆变器响应；
- 保证 AtomS3 永远不向通信总线发送；
- 在线查看 raw UART hex，并在断网期间暂存于 RAM ring；
- 在 HA 中看到 `Dxxx` sensors；
- 重复 Wi-Fi 模块热插拔实验；
- 运行连续 24 小时的严格完整性验证。

启动协议和解析算法的逐字段说明见
[`wifi-module-startup-sequence.md`](wifi-module-startup-sequence.md)。

## Step 1：准备硬件和工具

硬件：

1. M5Stack AtomS3/本项目所用 AtomS3-Lite；
2. HY2.0-4P 线或能可靠接触 G1、G2、GND 的测试线；
3. 官方 MakeSkyBlue Wi-Fi 模块和对应逆变器；
4. USB 线，仅用于 AtomS3 第一次烧录；
5. 万用表或示波器，用于确认 UART 是 3.3 V TTL；
6. 一台能持续运行 24 小时的 macOS/Linux 主机；
7. Home Assistant，以及需要时可到达 HA 的 WireGuard peer。

软件：Git、Python 3、可用的 C++17 编译器，以及能访问 ESPHome 设备所在
网络的终端。

## Step 2：断电接线

先让逆变器、Wi-Fi 模块和 AtomS3 全部断电。保持 Wi-Fi 模块与逆变器原来的
UART TX/RX 连接，AtomS3 只并联监听：

| AtomS3 HY2.0 线色 | 信号 | 连接位置 |
| --- | --- | --- |
| 黑 | GND | Wi-Fi 模块/逆变器的 UART 公共地 |
| 白 | G1 / GPIO1 RX | Wi-Fi 模块 TX → 逆变器 RX 这根线 |
| 黄 | G2 / GPIO2 RX | 逆变器 TX → Wi-Fi 模块 RX 这根线 |
| 红 | 5 V power | 只能接合适电源，绝不能当 UART 信号 |

上电前检查：

1. G1/G2 对地高电平约 3.3 V，而不是 5 V；
2. G1/G2 没有短接；
3. 三方 GND 相通；
4. AtomS3 没有任何 TX 线接到总线。

不要把 RS485 A/B、RS232 或 5 V TTL 直接接到 GPIO1/GPIO2。

## Step 3：取得固定版本代码

```sh
git clone https://github.com/chinawrj/MakeSkyBlue-HA-Passive-Monitor.git
cd MakeSkyBlue-HA-Passive-Monitor
git checkout v0.1.0-alpha.4
```

如果仓库仍处于公开许可审查阶段，需要仓库所有者先授予读取权限。不要从未知
fork 获取带凭据的预编译 firmware。

## Step 4：建立隔离的 ESPHome 环境

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/esphome version
```

本版本固定 ESPHome `2026.7.0`，避免生成代码因组件版本变化而出现差异。

## Step 5：创建本机配置和密钥

```sh
cp secrets.example.yaml secrets.yaml
cp wifi.example.yaml wifi.local.yaml
cp wireguard.example.yaml wireguard.local.yaml
chmod 600 secrets.yaml wifi.local.yaml wireguard.local.yaml
```

编辑 `secrets.yaml`：

1. 填写主 Wi-Fi 和两个备用 Wi-Fi；不需要备用网络时，从
   `wifi.local.yaml` 删除对应 network 条目；
2. 用 `openssl rand -base64 32` 生成 ESPHome API encryption key；
3. 设置独立 OTA password 和 fallback AP password；
4. 保留：

   ```yaml
   wifi_config: "wifi.local.yaml"
   wireguard_config: "wireguard.local.yaml"
   ```

5. 填写本机 WireGuard 地址、endpoint、peer public key、preshared key 和
   private key。

编辑 `wireguard.local.yaml`。若 HA 主动连接 AtomS3 的 VPN 地址，只需允许 VPN
网段；`netmask: 255.255.255.0` 足够，入站连接不受 netmask 限制。若 AtomS3 还
必须主动访问另一个 HA LAN 网段，仅增加 `peer_allowed_ips` 不会建立路由；按
ESPHome WireGuard 文档使用能覆盖目标的 netmask（全隧道可用 `0.0.0.0`），同时
把允许范围收紧到确切目标网段。跨 NAT 时保留
`peer_persistent_keepalive: 25s`。

这些文件已加入 `.gitignore`。执行以下命令，输出必须为空：

```sh
git status --short -- secrets.yaml wifi.local.yaml wireguard.local.yaml
```

## Step 6：验证严格 RX-only

在每次烧录前运行：

```sh
rg -n 'tx_pin:|uart_write|\.write\(|modbus_controller' \
  makeskyblue-modbus-monitor.yaml components packages
```

输出必须为空。随后让 ESPHome 展开配置：

```sh
.venv/bin/esphome config makeskyblue-modbus-monitor.yaml
```

确认 GPIO1/GPIO2 均只有：

```yaml
mode:
  input: true
  pullup: false
  pulldown: false
```

## Step 7：编译并首次 USB 烧录

```sh
.venv/bin/esphome compile makeskyblue-modbus-monitor.yaml
ls /dev/cu.usbmodem*
.venv/bin/esphome run makeskyblue-modbus-monitor.yaml \
  --device /dev/cu.usbmodem101
```

把设备路径换成实际值。编译结果应显示 ESP32-S3、ESP-IDF，并且没有 TX UART
警告。首次启动后，LED 状态为：

- 黄色慢闪：HA 尚未连接，UART record 正在本地 ring 缓存；
- 绿色常亮：HA 已连接；
- 青色闪烁：刚收到 G1/G2 数据；
- 紫红快速闪烁：ring 已覆盖旧 record，需要调查。

## Step 8：确认 Wi-Fi、OTA 和在线 raw log

找到设备 IP，或使用 mDNS：

```sh
ping makeskybluemodbusmonitor.local
.venv/bin/esphome logs makeskyblue-modbus-monitor.yaml \
  --device makeskybluemodbusmonitor.local
```

应持续出现类似：

```text
boot=305419896 #42 G1/GPIO1 8B RAW_HEX=01.03.00.64.00.33.44.00
boot=305419896 #43 G2/GPIO2 107B RAW_HEX=01.03.66...
```

序号由 AtomS3 上电后从 1 开始，全局覆盖两路 UART。不要用 callback 的 chunk
边界代替 Modbus 帧边界。

后续 OTA：

```sh
.venv/bin/esphome upload makeskyblue-modbus-monitor.yaml \
  --device makeskybluemodbusmonitor.local
```

## Step 9：接入 Home Assistant

1. 在 HA 的 **Settings → Devices & services → Add integration** 添加 ESPHome；
2. 输入 AtomS3 的 LAN 或 WireGuard 地址；
3. 输入 `secrets.yaml` 中的 API encryption key；
4. 在 ESPHome integration 选项中开启允许设备执行 Home Assistant actions；
5. 检查设备页是否出现：
   - `Onboard Boot Record`；
   - `G1 Last UART Chunk` 与 `G2 Last UART Chunk`；
   - `D30-D31 observed_network_time`；
   - `Dxxx` confirmed/provisional/raw sensors；
   - CRC、unknown、pending、expired、unpaired、ring overflow；
   - `Wi-Fi Module Startup Markers`。

累计保存 HA event 的 File integration 和 automation 配置见
[`atoms3-g1-g2-modbus-monitor.md`](atoms3-g1-g2-modbus-monitor.md)。先在
Developer Tools 中监听 `esphome.modbus_uart_capture`，确认 BOOT 与 UART record
都包含 boot_id、sequence、source、byte_count 和 hex。

File automation 必须在成功追加记录后调用
`esphome.makeskybluemodbusmonitor_ack_uart_capture`，并把 boot_id 和 sequence
都作为字符串回传。AtomS3 在收到 ACK 前每秒重发，因此文件端需要按
`(boot_id, sequence)` 去重。

使用 YAML packages 的 HA 可以直接复制仓库中的
`home-assistant/makeskyblue_uart_capture.yaml`；仍需先通过 File integration
创建 `notify.modbus_uart_log` 并建立带表头的 CSV 文件。

按 `atoms3-g1-g2-modbus-monitor.md` 创建持久化 `input_text` helper；它在 ACK
丢失导致重发时跳过第二次 File append，再发 ACK。这里确认的是 HA File action
完成，并不等价于主机突然掉电后的 fsync 保证。

## Step 10：建立基线抓包

```sh
mkdir -p captures
baseline_capture="captures/baseline-5min-$(date +%Y%m%d-%H%M%S).log"
.venv/bin/python tools/capture_esphome_logs.py \
  --duration 300 \
  --output "$baseline_capture"
```

采集命令结束后，在保留 `baseline_capture` 变量的同一终端运行：

```sh
.venv/bin/python tools/analyze_uart_capture.py \
  --frames-csv captures/baseline-frames.csv \
  --registers-csv captures/baseline-registers.csv \
  --summary-json captures/baseline-summary.json \
  "$baseline_capture"
```

传输/分帧基线应满足：

- `sequence_gap_count = 0`；
- G1/G2 CRC errors、discarded、incomplete 都为 0；
- `pending_requests = 0`；
- `unpaired_responses = 0`；
- 四段 FC03 共覆盖 178 个 word。

当前候选版本的完整 `--strict` 检查会有意返回非零，因为 CSV 仍明确标记 111 个
unresolved 地址（93 个 raw-only 加 18 个 provisional）；这不是工具故障。
最终 24 小时验收只有在
`unresolved_semantic_addresses = 0` 时才允许通过。开发阶段可从 JSON 输出中
分别审核传输指标和语义覆盖率，绝不能通过忽略退出码来宣称完成。

如果 capture 正好停在一个请求与响应之间，延长采集，而不是删除尾部数据来让
测试通过。

采集器默认拒绝追加到已有文件，并在 `CAPTURE_START` 写入 Git commit、dirty
状态、配置 SHA-256 和 OTA firmware SHA-256。只有 `duration_complete` 且进程退出
码为 0 才算完成；收到 signal 会写 `CAPTURE_END reason=signal` 并返回 130。
采集器每 30 秒写入一个只反映本机采集进程状态的 `CAPTURE_HEARTBEAT`。如果
`esphome logs` 子进程仍存活却连续 120 秒没有任何输出，采集器会先写
`STALE_OUTPUT`，再终止该子进程并重新连接。这个 watchdog 只操作 ESPHome API
日志订阅，不打开 UART，也不发送任何 UART 字节。`STALE_OUTPUT` 说明日志订阅
缺少输出，不能单独证明逆变器 UART 停止；必须结合 sequence 缺号、UART 时间戳
和重连后的 ring/状态恢复结果判断证据是否丢失。

## Step 11：重复 Wi-Fi 模块热插拔实验

1. AtomS3 必须保持供电并保持采集进程运行；
2. 记录当前墙钟时间和最后 sequence；
3. 只拔下官方 Wi-Fi 模块；
4. 等待数秒，再插回 Wi-Fi 模块；
5. 不要拔逆变器，不要重启 AtomS3；
6. 等 UART 常规轮询恢复后，再停止短期抓包。

已知的启动窗口数据模式是：

```text
FC03 polling stops
G1 FC16/0x10 write D30-D31 -> G2 FC16/0x10 acknowledgement
optional G1 00 00 00 FF marker (not present on every restart)
one or more D30-D31 time synchronizations, then the fixed FC03 polling chain
FC03 polling resumes
```

实际等待时间受人工拔插、Wi-Fi、VPN和云端影响，不能写成固定协议延迟。
`000000FF` 是可选的非 Modbus startup marker，不应增加 CRC/unknown/discarded，
但实现不得要求每次重启都出现它。

不能仅凭 D30-D31 连续时间同步判定模块重启：在 2026-08-08 的连续抓包中，
同样的三次 D30-D31 写入在没有再次拔插的情况下约每 5 分钟重复。重启窗口必须
同时有人工操作时间、轮询中断或链路状态变化等外部证据；D30-D31 本身只证明
Wi-Fi 模块正在向逆变器同步网络时间。

## Step 12：验证 D30-D31 时间

从 frames CSV 找 FC10、start register 30、count 2。把四个 data bytes 组合为：

```text
packed = (D30 << 16) | D31
```

按以下位域解码：

```text
year   = 2024 + ((packed >> 26) & 0x3F)
month  =          (packed >> 22) & 0x0F
day    =          (packed >> 17) & 0x1F
hour   =          (packed >> 12) & 0x1F
minute =          (packed >>  6) & 0x3F
second =           packed        & 0x3F
```

解码值应与抓包时间接近。保存原始 hex、request sequence、ack sequence 和时差，
不要只保存显示后的日期字符串。

## Step 13：运行完整 24 小时验证

```sh
.venv/bin/python tools/capture_esphome_logs.py \
  --duration 86400 \
  --output captures/uart-24h.log
```

保持主机不休眠、Wi-Fi稳定。每 30 分钟检查：

```sh
wc -c -l captures/uart-24h.log
pgrep -af capture_esphome_logs.py
mkdir -p reports/30min
checkpoint="reports/30min/$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python tools/analyze_uart_capture.py captures/uart-24h.log \
  --summary-json "$checkpoint" >/dev/null
shasum -a 256 -c "${checkpoint}.sha256"
```

同时在 HA 检查：设备 online、sequence 增长、ring overwritten 为 0、CRC/unknown/
discarded/unpaired/expired 为 0，以及 independently confirmed semantic
coverage 没有回退。当前 clean-room 候选基线是 67/178；新增语义必须
附带本项目抓包或 IoTRix 交叉验证证据。
分析报告里的 `missing_sequence_count` 才表示真正缺号；
`sequence_out_of_order_count` 表示日志输出次序与采集序号不同。后者必须保留
上下文供审计，但只要所有序号都存在且按序重组后帧完整，就不等同于数据丢失。
`transport_connect_attempt_count`、`transport_disconnect_count`、
`transport_stale_output_count` 和完整的 `capture_transport_events` 用来区分采集端
重连与线上的 UART 活动。报告不得只保留前若干 gap 或 parse issue；当前分析器
会保存每个缺号范围和每个解析问题的方向、序号、时间与 raw hex 上下文。
通信地址则查看 `Observed Modbus Communication Addresses`：它来自合法帧首
字节，不来自 D 寄存器。本机应为 `1`；若出现多个地址，必须在报告中保留各自
请求/响应原始帧并分别验证配对。

24小时结束后运行严格验收：

```sh
mkdir -p reports/final
.venv/bin/python tools/analyze_uart_capture.py --strict \
  --frames-csv captures/uart-24h-frames.csv \
  --registers-csv captures/uart-24h-registers.csv \
  --summary-json reports/final/uart-24h-strict.json \
  captures/uart-24h.log >/dev/null
shasum -a 256 -c reports/final/uart-24h-strict.json.sha256
shasum -a 256 captures/uart-24h.log \
  captures/uart-24h-frames.csv captures/uart-24h-registers.csv \
  reports/final/uart-24h-strict.json \
  > reports/final/uart-24h.sha256
```

不要因为某个地址 24 小时都为 0 就猜测它的语义。可以分类为“observed、固定
为零、语义未确认”，但最终目标要求每个出现的帧类型都有明确分类和原始证据。

## Step 14：与 IoTRix 交叉比对

在同一时刻记录 IoTRix 页面值和 HA 的 `Dxxx` 值：

1. 先匹配单位和缩放；
2. 对 power factor 等打包字节分别比较；
3. 把 UART/HA 有值、IoTRix UI 没有展示的字段标为
   `⚠ IoTRix界面未显示`；
4. 对差异记录 D 地址、raw word、解析值、IoTRix值、时间戳和页面位置；
5. 不以 IoTRix UI 没显示作为寄存器无效的证据。

建议至少取两个相隔 30 秒以上的动态快照，并让 IoTRix 时间与 UART frame 的
UTC 时间误差不超过 2 秒。对每个候选字段保存 `D地址、raw_u16、转换公式、
IoTRix显示值、UTC时间、证据状态`。已经由本项目重复得到的转换基线是：

| D 地址 | HA 字段/转换 |
| --- | --- |
| D7-D12、D23 | 电压或电流 `raw / 10`；D11=820 显示 82.0 A |
| D13 | `inverter_max_output_power_kw = raw / 10` |
| D14-D15 | charging/discharging SOC 百分比，raw 直接显示 |
| D30-D31 | 两个 word 组成 32 位网络时间，见 Step 12 |
| D32-D33 | force-charge start/end packed word，保留 packed 原值 |
| D104、D107 | Hz/A，`raw / 10` |
| D108-D109 | A，`(raw - 1000) / 10` |
| D110 | 低字节/100 为 load PF，高字节/100 为 inverter PF |
| D111-D113 | W，`raw - 30000` |
| D114 | battery V，`raw / 10` |
| D115 | signed int16 charging A，`raw_s16 / 10` |
| D122、D125 | PV1 V/A，`raw / 10` |
| D132-D133 | coherent UINT32 total kWh，`((D132<<16)|D133) / 10` |
| D128、D134 | PV1 W、system error code，raw 直接显示 |
| D135-D138 | °C，`(raw - 400) / 10` |
| D145 | `major=raw>>10, minor=(raw>>6)&0xF, patch=raw&0x3F` |
| D201-D206 | V/A，signed D206；均除以 100 |
| D208 | battery SOC 百分比，raw 直接显示 |

CSV 中 `confirmed_semantic` 表示已有上述交叉证据；`provisional_semantic` 表示
名称有 UI/设置相关性但还欠受控变化；`observed_raw` 表示仅确认该 word 确实被
轮询。UART/HA 有数据但 IoTRix 页面没有对应控件的地址，报告中必须醒目标注
`⚠ IoTRix界面未显示`，不得从结果中删除。

本机 2026-08-08 的 90 秒窗口中，93 个 raw-only 地址里只有 D131 非零，并且
始终为 84；其余 raw-only 地址均为 0。D131 不等于同刻 IoTRix 的 daily energy
18.6 或 SOC 81，常见 `/10`、`/100`、signed/offset 转换也没有形成匹配，因此
仍保留为 `observed_raw`。这类“非零但未匹配”数据应优先高亮调查，但不能靠
数值接近来命名。

## Step 15：最终交付证据

另一位开发者应能只使用以下内容复核结果：

- 精确的 Git tag 和 ESPHome 版本；
- 接线照片或接线表及 3.3 V 测量结果；
- 未含密钥的 YAML/config；
- 完整 24h log、frames CSV、registers CSV；
- strict analyzer JSON；
- HA diagnostics 截图；
- Wi-Fi 热插拔序号窗口；
- IoTRix 对照表；
- 每个解析结论的原始 frame 和 D 地址。

任何一次固件或解析器改动都应生成新 release；固件改动后，24小时验证窗口从
新固件上线时间重新计算。
