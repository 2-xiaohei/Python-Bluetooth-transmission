# Reliable Bluetooth 点对点客户端

这是一个无图形界面的 Python BLE 点对点通信工具。Windows、macOS、Linux 使用完全相同的客户端源码，两台设备都运行同一个 `peer` 命令，不区分服务端和客户端。

主要功能：

- 自动广播，同时持续扫描附近的兼容设备
- 第一次使用六位配对码，之后使用已保存的 HMAC 信任自动认证
- 文本发送、文件发送、ACK 回执、断线重连和自动重发
- 文件最大 10 MiB，4 KiB 分片，支持断点续传和 SHA-256 校验
- 只使用普通 JSON 文件保存状态，不使用 SQLite
- 每条状态带本机时间，并提供彩色状态、发送结果和文件进度条；不支持颜色时自动使用纯文本

## 运行要求

- Python 3.11 或更高版本
- 支持 BLE 的蓝牙适配器
- 操作系统蓝牙已开启，驱动和蓝牙服务工作正常
- 两台设备使用相同版本的本客户端

## Windows

1. 安装 64 位 Python 3.11 或更高版本，安装时勾选 `Add Python to PATH`。
2. 打开蓝牙，并在 Windows 的隐私设置中允许桌面应用使用蓝牙。
3. 解压或复制整个 `reliable-bluetooth-client` 文件夹。
4. 双击 `setup_windows.cmd`，或在 `cmd.exe` 中运行：

```cmd
setup_windows.cmd
```

5. 安装完成后运行：

```cmd
run_windows.cmd
```

也可以传入参数：

```cmd
run_windows.cmd --name My-Windows-PC --auto
```

## macOS

第一次使用前，在“系统设置 -> 隐私与安全性 -> 蓝牙”中允许 Terminal、iTerm 或当前使用的终端访问蓝牙。

在终端进入本文件夹，然后运行：

```bash
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh
```

指定设备显示名称：

```bash
./run_unix.sh --name My-Mac
```

如果系统再次弹出蓝牙权限提示，请选择允许，然后重新运行。

## Linux

Linux 需要 BlueZ、可用的 D-Bus 会话，以及支持 BLE 广播的蓝牙适配器。Debian/Ubuntu 可先安装常用依赖：

```bash
sudo apt update
sudo apt install python3 python3-venv bluez
sudo systemctl enable --now bluetooth
```

然后进入本文件夹运行：

```bash
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh
```

部分 Linux 发行版会限制普通用户访问蓝牙或 D-Bus。遇到权限错误时，应按发行版的 BlueZ/D-Bus 规则授权当前用户，不建议长期使用 `sudo` 运行客户端。部分旧蓝牙适配器只支持扫描，不能同时进行 BLE 广播，这种硬件无法作为可被其他设备发现的一端。

## 第一次配对

两台设备都启动客户端。终端会显示类似内容：

```text
+--------------------------------------------------+
| Reliable Bluetooth                               |
+--------------------------------------------------+
| Device: My-PC                                    |
| Pairing code: 123456                             |
| Mode: advertising + continuous scanning          |
| Storage: plain JSON files                        |
+--------------------------------------------------+
```

程序会一边广播自己，一边持续扫描附近设备。扫描没有整体超时时间，会一直等待；`--scan-timeout` 只表示每一轮扫描持续多少秒。

发现设备后：

1. 只在其中一台设备上输入设备编号并回车，另一台保持扫描等待，不要同时选择。
2. 在主动选择设备的一端，输入等待端终端显示的六位配对码。
3. 终端出现 `[ OK ] Handshake complete` 后才表示双方认证完成。

如果等待选择时对方先连接并认证了本机，当前选择会立即中断，直接进入通信界面。虽然程序会在认证后合并重复连接，但 macOS 和 Windows 同时主动连接时，操作系统可能在认证前拒绝其中一条 GATT 链路并报告 `Unreachable`，因此首次配对应只让一端主动选择。

首次成功后，身份和派生信任密钥保存在：

```text
data/peer_store.json
```

这里不保存原始六位配对码。以后连接同一设备会自动认证，不再要求输入配对码。删除任意一端的 `data/peer_store.json` 会清除该端身份和信任关系，下次需要重新输入配对码。

不要把自己的 `data/peer_store.json` 发给其他人，它包含设备身份和共享信任密钥。

## 发送文本

连接成功后直接输入文本并回车。发送方只有收到对端应用层 ACK 后才显示成功：

```text
[2026-11-03 09:30:15] [ OK ] Text delivered id=12ab34cd status=success attempts=1
```

如果 ACK 丢失，程序会使用同一个消息 ID 重发。对方会把最近 2048 个已处理文本 ID 保存到 `data/peer_store.json`，因此即使断线重连也不会再次显示同一条内容，而是返回 `status=duplicate`。明确失败会显示 `[FAIL]` 和原因；蓝牙断开则等待自动重连后继续发送。

退出：

```text
/quit
```

## 发送文件

发送不超过 10 MiB（`10 * 1024 * 1024` 字节）的文件：

```text
/sendfile /Users/name/Desktop/report.zip
```

Windows 路径或包含空格的路径使用引号：

```text
/sendfile "C:\Users\name\Desktop\my report.zip"
```

接收方会看到文件请求和八位 ID：

```text
[2026-11-03 09:31:02] [RECV] Incoming file [12ab34cd]: report.zip (8192 bytes)
Use /accept 12ab34cd or /reject 12ab34cd.
```

接受或拒绝：

```text
/accept 12ab34cd
/reject 12ab34cd
```

文件请求、每个 4 KiB 分片和最终校验都有独立 `request_id` 与 ACK，延迟 ACK 不会串到其他动作。断线后会从接收方已写入的偏移继续。发送结束后，只有接收方通过大小和 SHA-256 校验，发送方才显示 `File delivered`。

接收文件默认保存在：

```text
received_files/
```

指定其他目录：

```bash
./run_unix.sh --receive-dir /path/to/received
```

Windows：

```cmd
run_windows.cmd --receive-dir D:\BluetoothFiles
```

断点状态是普通 JSON 文件，保存在 `received_files/.transfers/`。未完成的数据使用 `.part` 文件保存。

## 动作回执机制

所有需要对端处理的发送动作都有唯一关联键和明确结果：

| 动作 | 关联键 | 成功结果 |
| --- | --- | --- |
| 配对与认证 | `nonce` | `peer_hello_result: success` |
| 文本消息 | `message_id` | `peer_ack: success/duplicate` |
| 文件请求、分片、完成 | `request_id` | 对应文件 ACK 与状态 |

配对流程为“认证请求 -> 认证应答 -> 确认 -> 最终结果”。只有最终结果成功，双方才保存首次信任并进入通信界面。底层 BLE 写入成功只代表数据写入蓝牙链路，不代表对端业务处理成功；终端显示 `[ OK ]` 表示已经收到对端应用层回执。

ACK 是一个动作的最终结果，因此 ACK 本身不再要求 ACK，否则双方会无限互相回执。

## 常用参数

查看全部参数：

```bash
./run_unix.sh --help
```

常用示例：

```bash
# 自动选择信号最强的兼容设备
./run_unix.sh --auto

# 指定每轮扫描 5 秒；程序仍会无限循环扫描
./run_unix.sh --scan-timeout 5

# 自动化发送一条文本后退出
./run_unix.sh --auto --pair-code 123456 --send "hello"
```

Windows 将 `./run_unix.sh` 替换为 `run_windows.cmd` 即可。

## 故障排查

### 一直找不到设备

- 确认两边都运行了客户端，并且蓝牙已打开。
- 确认终端已经获得蓝牙权限。
- 两台设备尽量靠近，关闭后重新打开蓝牙再试。
- Linux 检查 `systemctl status bluetooth`，并确认适配器支持 BLE 广播。

### 出现 `Unreachable` 或反复重连

- 不要在两台设备上同时输入设备编号。重启两边客户端，只在一端选择，另一端保持等待。
- 输入的是对方屏幕上的六位码，不是本机自己的六位码。
- 如果一端已经显示 `First connection: enter the six-digit code shown on the peer`，直接输入等待端显示的配对码并回车。

### 出现权限错误

- macOS：检查“隐私与安全性 -> 蓝牙”。
- Windows：检查蓝牙和桌面应用权限，并更新蓝牙驱动。
- Linux：检查 BlueZ、D-Bus 和当前用户权限。

### 安装失败

- 运行 `python --version` 或 `python3 --version`，确认版本不低于 3.11。
- Windows 优先从 python.org 安装官方 Python。
- Linux 确认已安装 `python3-venv`。

### 旧版本无法互连

本版本增加了最终握手结果和逐动作 `request_id`。两台设备必须使用同一版本；更新时应把整个客户端文件夹一起替换，但不要复制另一台设备的 `data/peer_store.json`。

本目录的 0.2 只能与其他 0.2 客户端通信。0.3 加密版使用不同的 BLE Service UUID、`RBS-` 广播名和加密握手，无法与本版本配对。该互斥关系已做代码层校验，但尚未专门进行 `0.2 ↔ 0.3` 真机连接尝试。
