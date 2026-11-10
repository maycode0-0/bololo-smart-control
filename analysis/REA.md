# REA 分析记录

## 已完成的 REA 操作

使用 `rea-agents@6.4.0` CLI，未更改 Codex 的 MCP 配置，未运行 APK。

| 分析 | Evidence ID | 保留文件 |
| --- | --- | --- |
| APK 归档图与成员清单 | `ev_0377fec1a47399b053a0bd5d59773271eaaad74da6989e3a6314cf9a1b6fb02d` | `rea-artifact.json` |
| 9 个通信/认证模块的 JavaScript 静态分析 | `ev_60d6f99fa33b5ffc90d2000995a51586a757151f93c75ab29364dd04d8d4a123` | `rea-focused-complete.json` |

输入 APK SHA-256：`d163fc6436fb9ee6322c71fa97d5aef81026f43805101f4ac3dbfc2b5cb58172`。
输入文件虽然名为 APK，但后缀为 `.apk.1`，REA 将容器格式识别为 ZIP。

通信分析范围为从包内 Metro bundle 提取的 `1161.js`、`1172.js`、`1213.js`、`1221.js`、`1222.js`、`1233.js`、`1238.js`、`1271.js`、`1291.js`。REA 成功解析 9 个文件，读取 87,627 字节文本，访问 26,035 个 AST 节点，解析失败数为 0。Metro 的 `__d` 注册并非标准 ESM/CommonJS 模块；REA 统计中 `modules=0`，不能据此声称已自动恢复 Metro 的跨模块调用图。业务调用关系同时通过包内模块依赖数组与原始文本人工静态核对，具体锚点见 FINDINGS.md。

完整 JSON 保留图和静态语义事实；便于阅读的身份、统计和限制摘要位于 `rea-summary.json`，可运行 `python tools/summarize_rea.py` 重新生成。

## 已处理的环境限制

- 本会话没有连接 REA MCP 工具，因此使用已获取的 CLI。没有为此修改用户全局配置。
- 初轮 `inspect-android-package` 因 `.apk.1` 的格式识别将目标视为 ZIP 而拒绝 Android 专用分析；云端协议由 React Native JavaScript 和产品 JSON 恢复。后续 LAN 调查复制同内容为 `.apk` 并完成 Java/Dex 入口分析，证据见 [LAN记录](LAN.md)。
- 首次全量 JavaScript 分析在 Windows 沙箱的子进程源文件传递阶段收到 `EPERM`，没有完成语义解析。保存的 `rea-javascript.json` 和 `rea-focused.json` 是失败/部分记录，不能替代成功证据。随后对上述 9 个明确模块获批执行 REA 静态分析，生成成功的 `rea-focused-complete.json`。
- APK 绑定的两份官方设备页面模板仅作为文本下载、解包和核对，没有执行其 JavaScript。该部分属于普通静态检查，不冒充 REA Evidence；来源、摘要与源码偏移保留于 `templates/evidence.json`。

## 结论账本与覆盖

| 原始问题 | 结果 | 证据与边界 |
| --- | --- | --- |
| App 和设备身份 | 已回答 | APK 归档 Evidence + 包内 app.config、product_list；精确匹配 P30/Z60 产品键 |
| 登录、认证与续期 | 已回答（静态） | 通信模块 Evidence 中的 1161/1221/1222；真实云端是否接受用户账号尚未测试 |
| 列表、状态、控制的请求结构 | 已回答（静态） | 1271/1291/1238 + 客户端请求契约测试；控制 data 是字符串 |
| 两款设备的用户控制语义 | 已回答已实现部分 | 官方模板静态锚点；采用真实 UI 范围与原始线协议类型 |
| 其他 Wi-Fi 产品 | 部分回答 | 13 款产品有 Wi-Fi 声明和数据点；仅 P30/Z60 完成模板级定向核对 |
| BLE、完整校准向导、日常远程出水 | 未实现 | BLE 另需协议调查；P30 校准出水不能证明日常远程出水可用 |
| LAN 帧、认证、数据点 | 已恢复并实现（静态/离线） | REA Java入口 + 普通 JADX/Ghidra补充；固定向量与回环UDP/TCP模拟设备验证，见 [原生协议](lan/NATIVE_PROTOCOL.md) |
| HA 加载与服务调用 | 已验证（离线） | 真实 HA 2024.12.5 五平台加载、注册、服务派发和卸载；云端、硬件及 Windows 不支持的持久化写入被模拟 |
| 实际设备执行、时效、并发 App 会话 | 未验证 | 没有账号、抓包、设备或 HA 主机连接信息；未宣称实机完成 |

默认优先读取 LAN，回退云端时状态来源是服务器缓存。成功 HTTP 请求不证明设备当前在线；`status_time` 原值作为属性/诊断字段保留。所有“已观察”结论均指静态工件或离线测试，不代表实际设备运行观察。
