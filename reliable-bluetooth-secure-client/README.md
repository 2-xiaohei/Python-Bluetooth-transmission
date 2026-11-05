# Reliable Bluetooth Secure

一个使用 Python 实现的跨平台 BLE 点对点通信客户端。Windows、macOS 和 Linux 使用同一套协议与源码，两台设备都同时提供广播和扫描能力，不需要区分服务端与客户端。

## 功能

- 自动广播并持续扫描附近兼容设备。
- 首次使用六位配对码，后续通过已保存的信任关系免码认证。
- 文本、文件、ACK 和控制消息端到端加密。
- 每个发送动作都有明确结果，未收到 ACK 不会报告成功。
- 连接中断后自动重连、重发和恢复文件传输。
- 接收端按消息 ID 去重，避免 ACK 丢失造成重复显示。
- 支持最大 10 MiB 文件、接收确认、断点续传和 SHA-256 校验。
- 终端显示时间、状态和进度，异步输出后会恢复尚未发送的输入。
- 使用本地 JSON 保存身份和状态，不使用 SQLite。

## 加密模型

客户端采用 X25519 + HKDF-SHA256 + ChaCha20-Poly1305：

- 首次启动生成长期 X25519 身份密钥。
- 首次配对交换身份公钥，并使用六位码认证密钥交换。
- 每次连接生成新的临时 X25519 密钥和会话密钥。
- 文本、文件名、文件大小、SHA-256、文件分片和 ACK 都包含在认证密文中。
- 双向通信使用不同密钥和 nonce 前缀。
- 递增计数器用于拒绝重放数据。
- 密文被修改、密钥错误或关联数据不一致时，消息会被直接拒绝。

BLE 链路观察者仍可能看到连接时间、密文长度和流量特征。六位码适合现场配对，但空间有限；在存在主动攻击风险的环境中，还应核对双方显示的公钥指纹。

## 环境要求

- Python 3.11 或更高版本。
- 支持 BLE 的蓝牙适配器。
- 已启动的系统蓝牙服务。
- 当前终端拥有蓝牙访问权限。
- Linux 当前用户拥有 BlueZ 和 D-Bus 权限，适配器支持 BLE 广播。

## Windows

进入本目录后运行：

```cmd
setup_windows.cmd
run_windows.cmd --name My-Windows-PC
```

安装脚本会创建 `.venv` 并安装运行依赖。再次运行客户端不需要重复执行安装脚本，除非源码或依赖已经更新。

## macOS

先在“系统设置 -> 隐私与安全性 -> 蓝牙”中允许 Terminal、iTerm 或当前终端访问蓝牙，然后运行：

```bash
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh --name My-Mac
```

## Linux

Debian/Ubuntu 可以先安装 BlueZ 和 Python 虚拟环境支持：

```bash
sudo apt update
sudo apt install python3 python3-venv bluez
sudo systemctl enable --now bluetooth
```

然后运行：

```bash
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh --name My-Linux-PC
```

不建议长期使用 `sudo` 启动客户端。遇到权限问题时，应按发行版规则为当前用户配置 BlueZ 和 D-Bus 权限。

## 首次配对

两台设备启动后都会显示本机配对码和密钥指纹：

```text
+------------------------------------------------------+
| Reliable Bluetooth Secure <version>                  |
+------------------------------------------------------+
| Device: My-PC                                        |
| Pairing code: 123456                                 |
| Key fingerprint: A1B2-C3D4-E5F6-0718                 |
| Encryption: X25519 + ChaCha20-Poly1305               |
| Mode: advertising + continuous scanning              |
| Key store: local JSON; do not share                  |
+------------------------------------------------------+
```

配对步骤：

1. 两台设备都启动客户端。
2. 只在一台设备上选择对方，另一台保持等待。
3. 主动连接端输入等待端显示的六位配对码。
4. 双方显示 `Encrypted session ready` 后核对对方指纹。
5. 以后连接同一设备时自动认证，不再要求输入配对码。

如果本机等待选择时先收到并完成了对方连接，设备选择会立即取消并进入通信界面。

## 文本通信

建立会话后，输入普通文本并回车。发送方只有收到对端的加密 ACK 才会显示成功：

```text
[2026-11-05 21:12:38] [ OK ] Text delivered id=b1ecad4b status=success attempts=1
```

每条文本使用稳定的 `message_id`。连接中断后客户端会在新会话中使用同一 ID 继续发送，接收端会把重复消息识别为已处理内容并返回结果，不会重复显示。

## 文件传输

发送不超过 10 MiB 的文件：

```text
/sendfile "/path/to/report.zip"
```

Windows 示例：

```text
/sendfile "C:\Users\name\Desktop\report.zip"
```

接收端根据终端显示的八位请求 ID 接受或拒绝：

```text
/accept 12ab34cd
/reject 12ab34cd
```

文件元数据和每个 1 KiB 分片都独立加密并等待 ACK。接收端保存已写入偏移，断线重连后从该位置继续。完成时必须同时通过文件大小和 SHA-256 校验，发送方才会显示成功。

指定接收目录：

```bash
./run_unix.sh --receive-dir /path/to/received
```

```cmd
run_windows.cmd --receive-dir D:\BluetoothFiles
```

## 可靠性机制

底层 BLE 写入成功只说明数据进入蓝牙链路，不代表对方已经处理。客户端使用应用层 ACK 确认配对、文本和文件动作：

| 动作 | 关联字段 | 成功条件 |
| --- | --- | --- |
| 配对和认证 | 会话 nonce | 双方完成最终握手确认 |
| 文本消息 | `message_id` | 收到 `success` 或 `duplicate` ACK |
| 文件请求、分片和完成 | `request_id` | 收到对应文件 ACK 和有效状态 |

如果一个会话连续无法确认数据，客户端会发送加密重连请求并废弃旧会话。重连后文本保留原消息 ID，文件从已确认偏移继续。扫描和等待没有整体超时时间，程序会持续尝试恢复，直到动作成功、被明确拒绝或用户退出。

## 命令和参数

| 输入 | 功能 |
| --- | --- |
| 普通文本 | 发送聊天消息 |
| `/sendfile PATH` | 发送文件 |
| `/accept ID` | 接受文件 |
| `/reject ID` | 拒绝文件 |
| `/quit` | 退出程序 |

查看参数：

```bash
./run_unix.sh --help
```

常用参数：

```text
--name NAME          设置显示名称
--auto               自动选择信号最强的兼容设备
--scan-timeout SEC   设置每轮扫描时长，不限制总等待时间
--receive-dir PATH   设置接收目录
--send TEXT          发送一条文本后退出
```

## 本地身份和数据

设备身份和信任关系保存在：

```text
data/secure_peer_store.json
```

该文件包含本机 X25519 私钥、可信设备公钥和派生信任秘密。不要上传、分享，或从一台设备复制到另一台设备。

接收文件和断点状态默认保存在：

```text
received_files_secure/
```

这些路径和 `.venv/` 都属于本机数据，并已从 Git 排除。删除 `secure_peer_store.json` 会创建新设备身份；需要重置配对时，应让双方都清理各自的信任数据后重新配对。

## 兼容性

Secure 客户端使用：

```text
Service UUID: 7b9e0001-8f4b-4f3f-a6c5-7a0d1e2c9000
广播名前缀: RBS-
```

它只能与相同 Secure 协议客户端通信。仓库中的 Classic 客户端使用其他 UUID、`RB-` 广播名和明文握手，双方不会互相发现为兼容节点。

## 分享客户端

可以分享整个 `reliable-bluetooth-secure-client` 源码目录，但必须排除本机生成的数据：

```text
.venv/
data/
received_files_secure/
__pycache__/
*.pyc
```

接收者运行对应平台的安装脚本后，会生成自己的虚拟环境、设备身份和配对码。

## 自动化测试

测试覆盖配对、密钥协商、免码重连、ACK 重发、跨会话去重、文件续传、失效会话恢复、密文篡改拒绝和重放拒绝。

从仓库根目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=reliable-bluetooth-secure-client/src \
reliable-bluetooth-secure-client/.venv/bin/python \
  -m unittest discover -s tests_secure -v
```

## 故障排查

### 找不到设备

- 确认两端都运行 Secure 客户端，广播名应以 `RBS-` 开头。
- 检查蓝牙开关、终端权限和系统蓝牙服务。
- 确认 Linux 适配器支持 BLE 广播。
- 只在一端选择设备，另一端保持等待。

### 提示公钥变化

对方身份与本地信任记录不同。先确认对方是否清除了设备身份；确认后，让双方删除各自的 `data/secure_peer_store.json` 并重新配对。

### 解密或认证失败

认证失败、被篡改或重放的数据都会被丢弃。如果问题持续出现，检查双方信任数据，必要时同时重置身份后重新配对。

### 信号弱或频繁重连

未完成动作会保留并自动恢复，但 BLE 文件吞吐量会随信号质量明显下降。让设备靠近、减少 2.4 GHz 干扰，并避免两端同时主动选择设备。
