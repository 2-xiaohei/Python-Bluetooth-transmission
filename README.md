# Python Bluetooth Transmission

一个面向 Windows、macOS 和 Linux 的 Python BLE 点对点通信项目。两台设备运行相同客户端即可互相发现、配对和通信，不需要部署中心服务器，也不区分固定的服务端与客户端。

项目提供端到端加密版本和保留兼容性的明文版本。新设备建议使用加密版。

## 项目能力

- 同时进行 BLE 广播和持续扫描，未发现设备时会继续等待。
- 任意一端都可以发起连接，另一端可以保持等待并接受连接。
- 首次连接使用六位配对码认证，建立信任后自动免码重连。
- 文本、文件和控制消息均有应用层 ACK，只有收到对端处理结果才报告成功。
- 链路异常时自动重连并重发；文本保持原消息 ID，接收端自动去重。
- 支持不超过 10 MiB 的文件传输、接收确认、断点续传和 SHA-256 完整性校验。
- 加密版使用 X25519、HKDF-SHA256 和 ChaCha20-Poly1305 保护会话数据。
- 跨平台终端界面带时间戳、状态颜色和文件进度，并能在异步消息出现后恢复正在输入的内容。
- 所有身份、信任关系和传输状态均使用本地 JSON 文件，不依赖 SQLite。

## 客户端版本

| 客户端 | 目录 | 用途 |
| --- | --- | --- |
| Reliable Bluetooth Secure | [`reliable-bluetooth-secure-client`](reliable-bluetooth-secure-client/) | 推荐使用；文本、文件、ACK 和控制消息端到端加密 |
| Reliable Bluetooth Classic | [`reliable-bluetooth-client`](reliable-bluetooth-client/) | 保留的明文协议实现，用于兼容原有设备 |

两个客户端属于不同协议，使用不同的 BLE Service UUID、广播名前缀和握手流程，不能互相通信，也不会自动降级为明文连接。

## 工作流程

1. 两台设备启动同一版本客户端，同时广播自己并扫描周围设备。
2. 只需在其中一台设备上选择对方；如果本机先被连接，设备选择会自动中断。
3. 首次连接时，主动连接端输入对方终端显示的六位配对码。
4. 双方完成身份验证和会话密钥协商后进入通信界面。
5. 每次发送动作等待对端 ACK；未确认的动作会在新会话中继续发送。
6. 后续连接使用本地保存的信任关系认证，不再要求配对码。

加密版的连接结构如下：

```text
发现设备 -> 首次配对/信任认证 -> 临时密钥协商 -> 加密会话
                                                   |
                            文本 ACK <-> 文件分片 ACK
                                                   |
                              失去确认 -> 重连并恢复
```

## 可靠传输

文本消息拥有稳定的 `message_id`。ACK 丢失或连接中断后，发送端仍使用同一个 ID 重发，接收端会识别重复消息并返回成功结果，而不会重复显示内容。

文件由独立请求和 1 KiB 加密分片组成，每个动作都使用唯一 `request_id` 关联 ACK。接收端持续保存已写入偏移，连接恢复后发送方从该位置继续。传输完成后还必须同时通过文件大小和 SHA-256 校验。

如果一个会话连续无法收到确认，客户端会废弃该会话、重新协商密钥并继续待完成动作，避免在失效链路上无限重试。

## 安全设计

加密版采用混合加密：

- 每台设备生成长期 X25519 身份密钥。
- 首次配对交换身份公钥，并用六位码绑定密钥交换。
- 每次连接生成新的临时 X25519 密钥。
- HKDF-SHA256 派生双向独立的会话密钥。
- ChaCha20-Poly1305 加密并认证文本、文件元数据、文件分片和 ACK。
- 每个方向使用递增计数器拒绝重放，认证失败的数据不会进入业务处理。

BLE 监听者仍可能观察连接时间、密文长度和流量特征。首次配对的六位码空间有限，在存在主动攻击风险的环境中，应同时核对两台设备显示的公钥指纹。

## 仓库结构

```text
.
|-- reliable-bluetooth-secure-client/   加密版源码、安装脚本和使用手册
|-- reliable-bluetooth-client/          明文版源码、安装脚本和使用手册
|-- tests_secure/                       加密版自动化测试
|-- tests/                              明文版自动化测试
|-- tools/                              BLE 联调辅助工具
|-- src/                                明文版开发源码
`-- README.md
```

## 环境要求

- Python 3.11 或更高版本。
- 支持 BLE 的蓝牙适配器。
- 已启用的操作系统蓝牙服务和驱动。
- 终端或 Python 进程拥有蓝牙访问权限。
- Linux 设备支持 BLE 广播，并允许当前用户访问 BlueZ 和 D-Bus。

## 快速开始

### Windows

进入 `reliable-bluetooth-secure-client` 文件夹：

```cmd
setup_windows.cmd
run_windows.cmd --name My-Windows-PC
```

### macOS

先在“系统设置 -> 隐私与安全性 -> 蓝牙”中允许当前终端访问蓝牙，然后运行：

```bash
cd reliable-bluetooth-secure-client
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh --name My-Mac
```

### Linux

Debian/Ubuntu 可以先安装基础环境：

```bash
sudo apt update
sudo apt install python3 python3-venv bluez
sudo systemctl enable --now bluetooth
```

然后运行：

```bash
cd reliable-bluetooth-secure-client
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh --name My-Linux-PC
```

两台设备都启动后，只在一端选择设备编号，另一端保持等待。首次连接时输入对方屏幕上的六位配对码。

## 使用方法

连接成功后直接输入文字并回车即可发送。

发送文件：

```text
/sendfile "/path/to/report.zip"
```

接收端根据显示的请求 ID 选择接受或拒绝：

```text
/accept 12ab34cd
/reject 12ab34cd
```

常用命令：

| 输入 | 功能 |
| --- | --- |
| 普通文本 | 发送聊天消息 |
| `/sendfile PATH` | 发送不超过 10 MiB 的文件 |
| `/accept ID` | 接受文件 |
| `/reject ID` | 拒绝文件 |
| `/quit` | 退出程序 |

指定接收目录：

```bash
./run_unix.sh --receive-dir /path/to/received
```

```cmd
run_windows.cmd --receive-dir D:\BluetoothFiles
```

完整参数：

```bash
./run_unix.sh --help
```

## 本地数据

加密版默认保存以下内容：

```text
data/secure_peer_store.json   设备私钥、可信公钥和信任秘密
received_files_secure/       接收文件和断点状态
.venv/                       本机 Python 虚拟环境
```

这些目录已从 Git 排除。不要上传、分享或在设备之间复制 `secure_peer_store.json`，否则会泄露设备身份和信任秘密。删除它会创建新身份，并要求双方重新进行首次配对。

## 协议兼容性

| 通信组合 | 结果 |
| --- | --- |
| Secure <-> Secure | 支持 |
| Classic <-> Classic | 支持 |
| Secure <-> Classic | 不支持 |

| 项目 | Classic | Secure |
| --- | --- | --- |
| Service UUID | `0000fff0-0000-1000-8000-00805f9b34fb` | `7b9e0001-8f4b-4f3f-a6c5-7a0d1e2c9000` |
| 广播名前缀 | `RB-` | `RBS-` |
| 应用层加密 | 无 | X25519 + ChaCha20-Poly1305 |

## 自动化测试

加密版测试覆盖首次配对、信任重连、消息 ACK、跨会话去重、失效会话恢复、文件续传、完整性校验、密文篡改拒绝和重放拒绝。

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=reliable-bluetooth-secure-client/src \
reliable-bluetooth-secure-client/.venv/bin/python \
  -m unittest discover -s tests_secure -v
```

明文版测试：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python3 -m unittest discover -s tests -v
```

## 故障排查

### 找不到设备

- 确认两端运行相同客户端，Secure 广播名以 `RBS-` 开头。
- 确认蓝牙已打开，终端已经获得蓝牙权限。
- 让设备靠近一些，并确认 Linux 适配器支持 BLE 广播。
- 只在一端选择设备，另一端保持等待。

### 提示公钥变化

对方的长期身份密钥与首次配对记录不同。先确认对方是否清除了本地身份；确认后，让双方删除各自的 `data/secure_peer_store.json` 并重新配对。

### 解密或认证失败

认证失败、被篡改或被重放的数据会被拒绝。如果问题持续出现，检查双方的信任数据是否一致，必要时清除双方信任并重新配对。

### 信号弱或反复重连

客户端会保留未完成动作并自动恢复，但弱信号会显著降低 BLE 文件传输速度。让设备靠近、减少 2.4 GHz 干扰，并避免两端同时主动选择设备。

更完整的加密版说明见 [`reliable-bluetooth-secure-client/README.md`](reliable-bluetooth-secure-client/README.md)。
