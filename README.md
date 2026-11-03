# Python Bluetooth Transmission

一个只使用 Python 的无图形界面蓝牙点对点通信项目。两台设备运行同一份客户端，可以自动扫描、选择并配对，支持断线重连、消息重发、文本聊天和不超过 10 MiB 的文件传输。

当前推荐使用 **Reliable Bluetooth Secure 0.3**。它在 0.2 的点对点传输能力上增加了端到端会话加密：

- 第一次配对时交换 X25519 身份公钥，并使用六位配对码完成认证。
- 每次连接生成新的临时 X25519 密钥，通过 HKDF-SHA256 派生双向会话密钥。
- 文本、文件元数据、文件分片和 ACK 都使用 ChaCha20-Poly1305 加密并认证。
- 支持密文篡改检测、防重放、失败重发和断线续传。
- 首次配对后保存信任关系，再次连接不需要重新输入配对码。

## 版本说明

| 版本 | 目录 | 说明 |
| --- | --- | --- |
| `0.3` 加密版，推荐 | [`reliable-bluetooth-secure-client`](reliable-bluetooth-secure-client/) | 文本和文件端到端加密，使用独立 BLE UUID 与 `RBS-` 广播名 |
| `0.2` 明文版 | [`reliable-bluetooth-client`](reliable-bluetooth-client/) | 保留原有兼容性；文本和文件不经过应用层加密 |

两个版本不能互相通信。通信双方必须运行相同主版本。

### 版本兼容矩阵

| 通信组合 | 是否支持 | 校验情况 |
| --- | --- | --- |
| `0.3` ↔ `0.3` | 支持 | 已通过本地与 Windows 自动化测试，并完成 macOS ↔ Windows 真实 BLE 联调 |
| `0.2` ↔ `0.2` | 支持 | 两个平台使用相同源码和协议，已通过自动化测试 |
| `0.2` ↔ `0.3` | 不支持 | 已做代码层兼容性校验；尚未专门进行这两个版本之间的真机连接尝试 |

代码层校验确认两个版本的 BLE Service UUID、特征 UUID、广播名前缀和握手协议均不同：

| 项目 | `0.2` 明文版 | `0.3` 加密版 |
| --- | --- | --- |
| Service UUID | `0000fff0-0000-1000-8000-00805f9b34fb` | `7b9e0001-8f4b-4f3f-a6c5-7a0d1e2c9000` |
| 广播名 | `RB-` | `RBS-` |
| 应用层加密 | 无 | X25519 + ChaCha20-Poly1305 |

扫描器只接受当前版本自己的 Service UUID。因此 `0.2` 不会把 `0.3` 列为兼容节点，`0.3` 也不会把 `0.2` 列为兼容节点。即使操作系统蓝牙页面显示了对方名称，客户端也不会自动配对或降级成明文连接。

## 功能

- 不分服务端和客户端，两端运行相同程序。
- 自动循环扫描附近兼容设备，扫描不设总超时。
- 任一端可以主动选择设备，另一端保持等待。
- 首次配对输入一次六位码，后续使用已保存的身份信任免码重连。
- 双向发送文本，每条消息都有明确 ACK，只在收到成功回执后显示成功。
- 重发使用同一消息 ID，接收端去重，避免 ACK 丢失后重复显示。
- 支持最大 10 MiB 的文件，逐块 ACK，接收方可以接受或拒绝。
- 文件传输支持断线续传和 SHA-256 完整性校验。
- 所有终端状态与进度行都带有时间戳。

## 仓库结构

```text
.
├── reliable-bluetooth-secure-client/   0.3 加密版完整源码与启动脚本
├── reliable-bluetooth-client/          0.2 明文版完整源码与启动脚本
├── tests_secure/                       0.3 测试
├── tests/                              0.2 测试
├── tools/                              BLE 联调辅助工具
├── src/                                0.2 开发源码
└── README.md
```

## 准备设备

需要两台支持 BLE 的设备，例如 Windows + macOS。每台设备都需要：

- Python 3.11 或更高版本；
- 可用的蓝牙适配器；
- 操作系统蓝牙服务与驱动；
- 允许终端访问蓝牙的权限。

macOS 首次运行前，请在“系统设置 -> 隐私与安全性 -> 蓝牙”中允许当前终端访问蓝牙。

## 安装加密版

### Windows

在 `reliable-bluetooth-secure-client` 文件夹中运行：

```cmd
setup_windows.cmd
run_windows.cmd
```

指定显示名称：

```cmd
run_windows.cmd --name My-Windows-PC
```

### macOS

```bash
cd reliable-bluetooth-secure-client
chmod +x setup_unix.sh run_unix.sh
./setup_unix.sh
./run_unix.sh
```

### Linux

Debian/Ubuntu 先安装 BlueZ 和 Python 虚拟环境支持：

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
./run_unix.sh
```

Linux 适配器必须支持 BLE 广播，当前用户还需要有 BlueZ 和 D-Bus 权限。

## 第一次配对

1. 在两台设备上启动加密版客户端。
2. 两端启动面板都会显示六位配对码和公钥指纹。
3. 只在一台设备上选择附近设备，另一台保持等待。
4. 主动连接端输入等待端显示的六位配对码。
5. 配对成功后核对终端显示的对方公钥指纹。

启动面板示例：

```text
+--------------------------------------------------+
| Reliable Bluetooth Secure                        |
+--------------------------------------------------+
| Device: Mac-Secure                               |
| Pairing code for new peers only: 123456          |
| Key fingerprint: A1B2-C3D4-E5F6-0718             |
| Encryption: X25519 + ChaCha20-Poly1305           |
| Mode: advertising + continuous scanning          |
| Key store: local JSON; do not share              |
+--------------------------------------------------+
```

连接成功后显示：

```text
[2026-11-04 10:30:00] [ OK ] Encrypted session ready with Win-Secure
[2026-11-04 10:30:00] [INFO] Peer key fingerprint: 1234-5678-90AB-CDEF
```

以后两端再次运行相同客户端时，会自动认证已保存的信任关系，不再询问配对码。

## 发送文本

连接成功后直接输入文本并回车：

```text
hello from macOS
```

发送方只有在收到对方加密 ACK 后才显示：

```text
[2026-11-04 10:31:00] [ OK ] Text delivered id=9ceaa883 status=success attempts=1
```

网络中断时，程序会自动重连，并使用相同消息 ID 重发。

## 发送文件

文件上限为 10 MiB：

```text
/sendfile /path/to/report.zip
```

路径包含空格时使用引号：

```text
/sendfile "/Users/name/Desktop/my report.zip"
```

Windows：

```text
/sendfile "C:\Users\name\Desktop\my report.zip"
```

接收端会显示八位请求 ID，然后输入：

```text
/accept 12ab34cd
```

或拒绝：

```text
/reject 12ab34cd
```

指定其他接收目录：

```bash
./run_unix.sh --receive-dir /path/to/received
```

```cmd
run_windows.cmd --receive-dir D:\BluetoothFiles
```

文件会在接收端解密并校验 SHA-256 后以普通文件保存。当前版本只提供传输加密，不加密接收文件所在的磁盘。

## 常用命令

| 输入 | 功能 |
| --- | --- |
| 普通文本 | 发送聊天消息 |
| `/sendfile PATH` | 发送不超过 10 MiB 的文件 |
| `/accept ID` | 接受文件 |
| `/reject ID` | 拒绝文件 |
| `/quit` | 退出程序 |

查看命令行参数：

```bash
./run_unix.sh --help
```

```cmd
run_windows.cmd --help
```

## 安全模型

第一次配对使用长期 X25519 身份密钥和临时 X25519 密钥。六位码参与身份绑定和信任密钥派生，后续连接通过保存的共享信任秘密验证双方身份。

BLE 监听者仍然可能观察连接时间、密文长度、会话 ID 和计数器，但看不到聊天文本、文件名或文件内容。

六位码的取值空间有限。在可能存在主动攻击者的环境中，首次配对必须同时核对双方显示的完整公钥指纹。

## 密钥和运行数据

加密版默认在本机保存：

```text
data/secure_peer_store.json
```

其中包含本机 X25519 私钥、已信任设备公钥和派生信任秘密。不要上传、复制或发送给其他人。

其他运行数据包括：

```text
received_files_secure/
.venv/
```

它们都属于本机数据，已从 Git 和发布包中排除。

如果需要完全重置加密版身份，先确认双方都要重新配对，再删除两端各自的 `data/secure_peer_store.json`。只删除一端会导致身份验证失败。

明文版使用独立的 `data/peer_store.json` 和 `received_files/`，不会与加密版混用。

## 分享给别人

分享 0.3 加密版时，可以发送整个 `reliable-bluetooth-secure-client` 源码目录，但必须排除：

```text
.venv/
data/
received_files_secure/
__pycache__/
*.pyc
```

接收者运行对应平台的安装脚本，即可创建自己的虚拟环境和设备身份。

## 测试

加密版测试覆盖首次配对、免码重连、文本 ACK 重发、跨重连去重、文件多分片、断线续传、密文篡改拒绝、重放拒绝以及线路上不出现明文内容。

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=reliable-bluetooth-secure-client/src \
.venv/bin/python -m unittest discover -s tests_secure -v
```

加密版在本地和 Windows 的测试结果均为 `38/38` 通过。macOS 与 Windows 也已进行真实 BLE 联调，验证了首次配对、双向加密文本、文件传输、SHA-256 一致性和免码重连。

`0.2` 与 `0.3` 的互斥关系通过源码常量、广播过滤条件和握手协议做了代码层校验，但没有专门进行一次 `0.2 ↔ 0.3` 真机连接尝试。文档不会把代码层校验描述为真机验证。

明文版测试：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python3 -m unittest discover -s tests -v
```

## 常见问题

### 找不到设备

- 确认两端运行的都是同一个版本。
- 加密版广播名以 `RBS-` 开头，明文版不能与加密版配对。
- 确认两台设备都已打开蓝牙并允许终端访问。
- 只在一端选择设备，另一端保持扫描等待。

### 提示公钥变化

这表示对方的身份密钥与首次配对时不同。先确认对方是否删除过密钥文件或重新安装。确认后让两端都清除各自的信任文件，再重新完成首次配对。

### 解密或认证失败

程序会拒绝认证失败、被篡改和被重放的密文。如果持续出现，先重新连接；若两端信任数据不一致，让两端分别删除 `data/secure_peer_store.json` 后重新配对。

### 为什么不是直接用 RSA 加密文件

大文件不适合直接用非对称算法加密。本项目使用 X25519 完成身份和会话密钥协商，再使用 ChaCha20-Poly1305 对称加密每个分片，兼顾安全性和 BLE 传输效率。
