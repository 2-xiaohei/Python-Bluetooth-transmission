# Reliable Bluetooth Secure 0.3

这是独立的加密版 Python BLE 点对点客户端。Windows、macOS、Linux 使用相同源码和相同命令，两台设备都运行客户端，不区分服务端和客户端。

原来的 `reliable-bluetooth-client` 0.2 文件夹保持不变。加密版使用独立 BLE Service UUID 和 `RBS-` 广播名，不会把 0.2 客户端误认为兼容设备。

## 加密设计

本版本采用混合加密，而不是直接使用非对称算法加密大文件：

- 每台设备首次启动生成长期 X25519 身份密钥。
- 第一次配对交换 X25519 公钥，并使用六位配对码认证密钥交换。
- 每次重新连接都生成新的临时 X25519 密钥，提供新的会话密钥和前向保密。
- X25519 共享值、首次配对秘密和会话 nonce 通过 HKDF-SHA256 派生双向密钥。
- 文本、文件名、文件大小、SHA-256、文件分片、发送回执都封装在 ChaCha20-Poly1305 密文中。
- 每个方向使用独立密钥和 nonce 前缀，并使用 64 位递增计数器拒绝重放。
- 篡改密文、使用错误密钥或修改关联数据都会导致认证失败，不会交给消息或文件处理逻辑。

BLE 链路上仍可观察连接时间、密文长度、会话 ID 和计数器，但看不到文本、文件名和文件内容。

接收方解密并校验成功后，文件会以普通文件保存到 `received_files_secure/`。本版本提供传输中加密，不提供接收文件的磁盘加密。

## 密钥文件

设备身份、X25519 私钥、已信任设备公钥和派生信任秘密保存在：

```text
data/secure_peer_store.json
```

不要把这个文件发送给其他人，也不要把一台设备的文件复制到另一台设备。macOS 和 Linux 会把权限设置为仅当前用户可读写。

删除 `data/secure_peer_store.json` 会生成新身份，同时清除全部信任关系。另一台设备也需要删除自己的对应信任记录；最简单的重置方式是两边都删除该文件后重新配对。

## 首次配对和指纹

启动面板会显示本机六位配对码和公钥指纹：

```text
+------------------------------------------------------+
| Reliable Bluetooth Secure                            |
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

1. 两台设备都启动加密版。
2. 只在一台设备上选择对方，另一台保持扫描等待。
3. 主动连接端输入等待端显示的六位配对码。
4. 连接成功后终端显示对方公钥指纹。
5. 首次配对建议核对该指纹是否等于对方启动面板中的 `Key fingerprint`。

成功状态：

```text
[2026-11-03 15:30:00] [ OK ] Encrypted session ready with My-PC
[2026-11-03 15:30:00] [INFO] Peer key fingerprint: A1B2-C3D4-E5F6-0718
```

六位码的取值空间有限。在可能存在主动攻击者的环境中，必须同时核对公钥指纹；只核对六位码不等同于高强度身份验证。

## Windows 安装和运行

要求 Python 3.11 或更高版本。进入本文件夹后运行：

```cmd
setup_windows.cmd
run_windows.cmd
```

指定名称：

```cmd
run_windows.cmd --name My-Windows-PC
```

## macOS 安装和运行

在“系统设置 -> 隐私与安全性 -> 蓝牙”中允许当前终端访问蓝牙，然后运行：

```bash
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh
```

## Linux 安装和运行

Debian/Ubuntu 可先准备 BlueZ 和 Python：

```bash
sudo apt update
sudo apt install python3 python3-venv bluez
sudo systemctl enable --now bluetooth
```

然后运行：

```bash
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh
```

Linux 适配器必须支持 BLE 广播，当前用户也必须有 BlueZ 和 D-Bus 权限。

## 发送文本

加密会话建立后直接输入文本并回车：

```text
hello
```

只有收到加密 ACK 后才显示发送成功。ACK 丢失时使用相同消息 ID 和新的加密计数器重发；接收方跨重连保存最近 2048 个消息 ID，避免重复显示。

退出：

```text
/quit
```

## 发送文件

文件上限仍为 10 MiB：

```text
/sendfile /path/to/report.zip
```

Windows 路径包含空格时：

```text
/sendfile "C:\Users\name\Desktop\my report.zip"
```

接收端使用请求 ID 接受或拒绝：

```text
/accept 12ab34cd
/reject 12ab34cd
```

文件元数据和每个 4 KiB 分片都单独加密并认证。每个动作仍有独立 `request_id` 和加密 ACK；断线后重新协商新会话密钥，再从接收方已保存的偏移继续。最终必须通过文件大小和 SHA-256 校验。

指定接收目录：

```bash
./run_unix.sh --receive-dir /path/to/received
```

Windows：

```cmd
run_windows.cmd --receive-dir D:\BluetoothFiles
```

## 版本兼容

- 0.3 加密版只能与 0.3 加密版通信。
- 0.2 原版只能与 0.2 原版通信。
- 两个版本的运行目录、虚拟环境、BLE UUID、广播名和默认密钥文件互相独立。
- 给其他人使用时，应发送整个 `reliable-bluetooth-secure-client` 文件夹，不要包含 `.venv/`、`data/`、`received_files_secure/` 或 `__pycache__/`。

| 通信组合 | 是否支持 | 校验情况 |
| --- | --- | --- |
| `0.3` ↔ `0.3` | 支持 | 本地与 Windows 测试通过，并完成 macOS ↔ Windows 真实 BLE 联调 |
| `0.2` ↔ `0.2` | 支持 | 两个平台使用相同源码和协议，已通过自动化测试 |
| `0.2` ↔ `0.3` | 不支持 | 已做代码层兼容性校验，尚未做专门的跨版本真机连接尝试 |

0.2 使用 Service UUID `0000fff0-0000-1000-8000-00805f9b34fb` 和 `RB-` 广播名；0.3 使用 Service UUID `7b9e0001-8f4b-4f3f-a6c5-7a0d1e2c9000` 和 `RBS-` 广播名。0.3 扫描器只接受自己的 Service UUID，因此不会把 0.2 当作兼容设备，也不会自动降级成明文连接。

## 常见问题

### 找不到设备

- 确认两端运行的都是 0.3 加密版，广播名应以 `RBS-` 开头。
- 确认蓝牙已打开，终端具有蓝牙权限。
- 只在一端选择设备，另一端保持等待。

### 提示公钥变化

这表示对方的长期身份密钥与首次配对时不同。不要直接忽略。先确认对方是否删除了 `secure_peer_store.json` 或重新安装；确认后两边清除旧信任并重新配对。

### 解密或认证失败

程序会丢弃认证失败、被篡改或重放的密文。持续出现时应重新连接；如果信任文件不一致，两边清除 `data/secure_peer_store.json` 后重新配对。
