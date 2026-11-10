# LAN 原生协议恢复（v0.1.1）

目标 APK SHA-256：`d163fc6436fb9ee6322c71fa97d5aef81026f43805101f4ac3dbfc2b5cb58172`。
APK 内 `lib/arm64-v8a/libNativeCommandParser.so` SHA-256：
`27e0eafeef4e516a5b3abda95f360d8c1152ece52397dd91cec1ebb91d0c6c0e`。

## 方法与边界

先用已安装 REA 6.4.0 尝试打开此 ELF。REA 明确返回 Windows Ghidra P0 只支持 PE，完整响应在 `native-open.json`；这是提供方能力限制，不能据此声称已用 REA 反编译此原生库。此前 Java 入口的 REA 结果仍保留在本目录。

补充分析使用机器上已经安装的 Ghidra 12.1.4，对该 ARM64 ELF 做静态导入和反编译；脚本 `ExportLanNative.java`、日志 `ghidra-export.log` / `ghidra-export2.log`、函数索引和伪代码 `native-code/` 可复核。这些是普通 Ghidra 补充观察，**不是 REA Evidence**。Android JNI、APK、固件均未执行；没有访问用户设备。

以下地址来自 Ghidra 默认映射下的函数入口，伪代码文件以地址命名；不是原始 C 源码。

## 帧、认证和发现

| 观察 | 静态锚点 |
| --- | --- |
| 帧：`00000003` + MQTT 式 remaining-length varint + flag byte + command uint16BE + payload；不含校验和 | `GizWifiSDKBuildHead` `00118228.c` |
| 无参数请求 3/6 为 body 长度 3；8 为登录 | `parseData` `0010976c.c`、`packNoneDataCmd` `00108efc.c` |
| 只有 147/148 增加 uint32BE SN，位于 command 后 | `packTransBusinessDataWithSN` `00109528.c`、`unParseTransBusinessDataWithSNCmd` `0010ab90.c` |
| 登录请求 payload：uint16BE 字节长度 + passcode 文本；bind7 响应相同格式 | `packLoginDataCmd` `00108f9c.c`、`unParseBindDeviceCmd` `0010a3e8.c` |
| 登录响应首字节 0 成功，非 0 失败 | `unParseLoginDeviceCmd` `0010a504.c` 将非零映射成 loginResult1；Java `LoginDeviceResponseCommand.isLoginSuccess()` 要求 loginResult0 |
| 发现响应 4/5：依次为 uint16BE 长度前缀的 did、MAC、Wi-Fi 软件版本、productKey，后有8字节设备信息及可选扩展 | `unParseFindDeviceCmd` `00109f58.c` |
| MAC 长度12为 ASCII 十六进制，其他长度按原始字节转hex | 同上 |

SDK `GizLanModuleFactory` UDP 发现目标端口12414，另监听2415；`ConnectionConfig` 和 `GizLanBaseDeviceHandler` 使用 TCP12416。Java 补充源在 `sdk-source/`。

当前实现仅编码/接受 flag0 的普通直连命令；带 flag1 和附加 did 的网关转发格式未实现。单帧限制64KiB、varint最多4字节、严格检查实际长度是本项目的防御性设计。

## 变量数据点

13 个 Wi-Fi 产品均为 `packetVersion=0x00000004`、`protocolType=var_len`，原始 schema 的 id 连续从0开始。`lan_profiles.json` 保留 APK 的原始字段宽度、类型、id和数值变换；不使用 UI 覆盖后的范围决定二进制偏移。

- 读取 action `0x12` 后接 `ceil(所有attrs数量/8)` 字节 flags。全读取填 `FF`，包括末尾padding位。锚点 `GizWifiSDKEncodeGetStatus` `0010f790.c`。
- 写入 action `0x11` 后接相同宽度的 flags。数据存在位对应属性 id，id0 是整个位图的最低位；位图按大端字节存储。
- 仅存在于 flags 的字段参与数据打包，按 id 升序。所有 bool/enum 先组成紧凑 bit 区；第一个字段占最低位，跨字节时整个 bit 区按大端存储。随后所有 uint/binary 字段按 id 升序排列，uint16/32 为大端。
- 数值编码 `raw=(engineering-addition)/ratio`，解码 `engineering=raw*ratio+addition`。锚点 `FUN_001179d0.c` 和 `FUN_001150f0.c`。
- 读取响应 action `0x13`、上报 `0x14`，解码布局与写入相同。静态解码器也接受 `0x11`。锚点 `GizWifiSDKDecode` `001142e0.c`。
- flags宽度、bit/byte区大小的计算：`FUN_00111994.c`；编码位序与按id排序：`FUN_00111dcc.c`、`FUN_001114f4.c`；解码位序：`FUN_001150f0.c`。

这些观察确认该格式是**位图与紧凑字段数据**，不是逐字段 TLV。

## 响应匹配

Java `GizBaseDeviceHandler.getDp` 的 extraResponseCommands 为空，通用 callback 只接受 request.cmd+1，也就是148；`ReadDataPointResponseCommand` 要求非空 data。`sendDp` 额外允许147，所以控制允许148空确认或147数据报告。`subscribeDp` 另外监听145/147/148。

通用 callback 源文件：
`sdk-source/com/gizwits/smart/sdk/core/module/GizBaseDeviceHandler$writeCommandWithCallback$4$responseDeferred$1$responseResult$1.java`。

## 固定向量与验证

`tests/test_lan_codec.py` 使用从上述布局独立计算的固定字节，覆盖断帧、粘帧、恶意长度、只读写入拦截、枚举和缩放、多于8 bit的位组：

| 操作 | payload十六进制 |
| --- | --- |
| P30全读取（32attrs） | `12ffffffff` |
| P30 switch=true,temp_set=45 | `1100008001012d` |
| P30 switch=true,temp_set=45,temp_current=42读取响应 | `1300018001012d2a` |
| Z60全读取（47attrs） | `12ffffffffffff` |
| Z60 auto=true,dry=false,disinfection=false | `1100000000007001` |
| 合成9bool跨字节+uint16缩放示例 | `1103ff010100ff` |

codec 的25项测试通过；根任务另外维护真实 loopback UDP/TCP 模拟设备测试。固定向量来自静态恢复后人工计算，**不是实际设备抓包或执行原生解析器得到的 oracle**。

仍未确认：Z60/P30具体固件是否开放该SDK的LAN服务、绑定窗口和passcode策略、跨VLAN可达性、真实超时与重连行为。实现与模拟测试不代表已完成实机联调。
