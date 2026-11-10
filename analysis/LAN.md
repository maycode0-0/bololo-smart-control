# LAN 接入与证据边界（v0.1.1）

已实现本地发现、认证、数据点读写，以及默认 LAN 优先、按规则回退云端的 HA 接入。
**Z60/P30 尚未经过实机联调**，具体固件是否开放本地服务仍未知。实现依据来自 APK 静态恢复，测试使用固定向量和回环模拟设备，不能替代设备验证。

## 工件与方法

用户提供 APK 的 SHA-256：
`d163fc6436fb9ee6322c71fa97d5aef81026f43805101f4ac3dbfc2b5cb58172`。
为满足 Android 分析器的后缀要求，复制为 `analysis/lan/bololo.apk`，内容未改动。

先使用已安装 REA 6.4.0 检查 Java 入口、LAN handler、发现工厂与连接配置。
随后用现有 JADX 导出相关 Java 类，补足协程和完整方法源码；这些补充源码不是 REA Evidence。
实际编解码在 JNI 库 `libNativeCommandParser.so` 中。
REA 的 Windows Ghidra 提供方明确拒绝 ARM64 ELF（当前仅支持 PE）；因此用本机已有 Ghidra 12.1.4 做普通静态反编译。
没有执行 APK/JNI/固件，没有扫描用户网络，没有安装新引擎或修改全局配置。

详细函数地址、库哈希、固定向量及补充源码位置见 [原生协议恢复](lan/NATIVE_PROTOCOL.md)。

## 已观察的协议

| 环节 | 静态观察 |
| --- | --- |
| 发现 | 向 UDP 12414 发 discovery3，接收 discovery4/state5；另监听 UDP 2415 |
| 身份 | 响应含长度前缀的 did、MAC、软件版本、productKey |
| TCP | SDK 默认端口 12416 |
| 认证 | bind6/7 取得 passcode，login8/9 登录；原始结果0成功 |
| 帧 | magic `00000003`、变长长度、flag、uint16BE命令；147/148另含uint32BE序号 |
| 数据点 | packetVersion4；属性位图与紧凑bit/byte区，整数大端并应用原始数值变换 |
| 读取 | 请求147，action0x12；响应148，action0x13 |
| 控制 | 请求147，action0x11；SDK接受148确认或147数据报告 |

上述是 SDK 静态行为，不能视作目标设备已开放端口的实测结果。
App 业务模块1291默认在 BLE/MQTT 间选路，通用 SDK 有 LAN 实现不能证明每款固件均支持 LAN。

## 本项目的实现选择

- `auto`：本地读取失败可回退云端；控制仅在物理命令尚未写出时回退。写出后超时、断连或响应无法确认，返回结果不明，绝不跨通道重发。
- `cloud`：只使用云端。`lan`：只使用本地，不刷新云端清单。
- 首次通过账号获得清单，私有 HA Store 只保存最小身份/命名/认证字段。缓存存在时 auto/lan 启动不先登录云端；本地 bind/login 不需要云端取得密钥。
- 手动 IPv4 用于单播发现，仍须匹配 productKey+MAC，避免控制错误地址上的另一台设备。
- 每次操作使用短连接并在结束时关闭，不依赖未验证的心跳周期。单次响应有超时、帧有长度上限，发现有退避。
- 实体标注最近成功通道；云端仍是服务器缓存，不能当作本地实时回读。
- 当前只支持普通flag0帧，未实现flag1网关转发格式。

## 验证与未知

`tests/test_lan_codec.py` 对照独立计算的固定字节，覆盖边界、粘帧/断帧、非法长度、缩放、枚举和跨字节bit区。
`tests/test_lan.py` 通过真实回环 UDP/TCP 模拟发现、bind/login、分段帧、控制ACK/报告、认证拒绝、错误设备、错误序号和超时后禁止重放。
路由及 HA 测试另外覆盖三模式、库存保存、离线启动、配置选项、五个平台、服务调用与卸载。

未验证项：两款具体固件的 LAN 服务和绑定窗口、passcode策略、实际执行、状态时效、跨VLAN发现及长期稳定性。用户暂时无法测试，未进行真实设备探测。

## REA 证据索引

完整 JSON 均保留于 `analysis/lan/`，均为静态结果。

| 操作 | Evidence ID | 文件 |
| --- | --- | --- |
| Android package | `ev_e92421cead70f9a1389bfcb34cdb788a488ca43f59f79b8d4c42dc59a2a3f916` | rea-android.json |
| LAN handler | `ev_8a960c20e13bc735600111cfe8305c9bc7bd7359b86971b91973f5a1fea7875b` | rea-handler.json |
| ConnectionConfig | `ev_8b1b753c9e16519a4ed56daf09e38389fe5707c78329319066c61193cddc7017` | rea-config.json |
| connectToDevice | `ev_a90182e3b2d05d2f6bdb9fd311d2d6a10ead500023a2f97ca4644fc9d1151904` | rea-connect.json |
| 默认TCP端口 | `ev_f0d7f75d7107e7344b35f6ced1c56650223465eca11976cd9cbc78afb425a5db` | rea-default-port.json |
| SDK类搜索 | `ev_bf8d6aec3be88fa26b0f9c120e44743eba7d829d27cf06a493c95163c8e562df` | class-search.json |
| LAN发现工厂 | `ev_a6687b98926a8eac67851278e08aad777408f3d927ff1fad6cfa2516e1c1ec18` | factory-class.json |

REA/JADX初次子进程被沙箱以 `spawn EPERM` 阻止，相同范围静态分析随后获批成功。
原生函数地址来自普通 Ghidra 补充分析，未冒充 REA Evidence。
