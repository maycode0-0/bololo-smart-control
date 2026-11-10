# BOLOLO APK 静态协议分析

本文件记录普通 ZIP/文本/JSON 静态检查得到的事实；这些检查不是 REA Evidence。
REA 的成功分析记录、Evidence ID、覆盖范围与限制见 [REA.md](REA.md)。未运行 APK、未使用真实账号、未连接或控制设备。

## 范围与身份

- 输入：`com.gizwits.MainApplication.apk.1`，大小 344852402 字节。
- 完整 APK SHA-256 记录于 `analysis/product-summary.json`。
- `assets/app.config`：React Native/Expo 应用，版本 `2.1.86`，Android package `com.gizwits.xb`。
- `assets/index.android.bundle` 为可读 Metro JavaScript；业务模块也分布在 `assets/js-modules/*.js`。
- `assets/productConfig/*.json` 提供 21 个产品的声明式数据点，包含字段名称、类型、读写权限、枚举与边界。

## 已观察的云端协议

| 问题 | 静态来源 | 观察结果 |
| --- | --- | --- |
| 生产服务器 | bundle 模块 1213 `getServiceInfo` | `https://app.bololoapp3.com/`，cn/us/eu 配置均指向该域名 |
| Android application ID | bundle 模块 1172 `gizwits.androidAppID` | `91732ca20dfd4517b164c38aac162c43`；模块 1163 中另有默认值，应使用实际配置覆盖后的值 |
| AEP 请求封装 | bundle 模块 1161 | JSON headers：`Content-Type: application/json`、`Version: 1.0`、`X-Gizwits-Application-Id`；认证请求追加 `Authorization: token` |
| AEP 请求 body | bundle 模块 1161 `k` | 原 body 被包在 `{appKey: application_id, data: original_body, version: "1.0"}` 中 |
| AEP 成功判定 | bundle 模块 1161 | 响应 JSON 的 `code` 转整数后为 200，业务内容在 `data`；505、526 触发退出登录逻辑 |
| 密码登录 | bundle 模块 1221 `loginByPassword`、1222 `loginByPwd` | POST `app/smart_home/login/pwd`，data 为对象：`account`、`password`、`mobileCountryCode`、`lang`、`refreshToken: true`；调用链未发现密码散列操作 |
| token 更新 | bundle 模块 1161 `w`、1173 `setTokenInfo` | POST `app/user/refresh_token`，data 对象含 `clientId`、`refresh: true`、`refresh_token`；返回包含 `token`、`refresh_token`、`created_at`、`expired_at`、`uid` |
| 列出账号设备 | bundle 模块 1271 `getUserDeviceList`、1238 `getDeviceList` | GET `app/smartHome/v2/users/devices`；解封后的 `data` 被直接当设备数组使用 |
| 设备标识 | bundle 模块 1238 的设备合并/改名代码、1271 | 设备包含 `productKey`、`mac`、`name`、`sno`；部分代码还使用 `did`、`deviceSourceType` |
| 查询缓存状态 | bundle 模块 1271 `getDeviceStatus`、1291 `getDeviceDataPoint` | POST `bololo/app/device/status/{productKey}/{mac}`，data 为 `{}`；响应 data 是平铺的数据点字典，还可能含 `status_time` |
| 请求设备主动上报 | bundle 模块 1271 `sendFullQueryDataApi`、1291 `getDeviceStatus` | POST `bololo/app/device/issueFindStatus/{productKey}/{mac}`，data 默认为 `{}`；静态来源未证明该接口立即返回新状态 |
| 控制 | bundle 模块 1271 `write`、1291 `sendCmdToDevice` | POST `bololo/app/device/control/{productKey}/{mac}`；先 `JSON.stringify(datapoints)` 再套 AEP envelope，因此最终 `data` 为 JSON 字符串，字段如 `switch`、`temp_set` 直接作为其键 |
| 只读分享 | bundle 模块 1291 `checkCanWrite` | 接收分享记录的 `roleType == 1` 时 App 阻止写入；此条件不能单凭设备状态推断 |

上面 AEP 协议与模块 1232/1233 的旧 PaaS API 不同。PaaS 调用使用
`X-Gizwits-User-token`，并提供 `app/bindings`、`app/devdata/{did}/latest`、
`app/control/{did}`；不可把这组 headers/body 与 AEP 的封装混用。

## 数据点与传输方式

模块 1268 `product_list` 明确声明 7 个吸奶器产品 `m2mProtoc: "BLE"`。
其余 13 个有相应 schema 的已配置产品声明 `WIFI`。另有一个打包 schema
`k1mvp0OAMs1000000000000000000000`（消毒泡奶二合一1710W）未出现在配置产品列表，
因此传输方式记作 `unknown`。配置列表还包含一个没有对应 schema 的测试产品，不纳入集成。

| 传输声明 | schema 数量 | 实现含义 |
| --- | ---: | --- |
| WIFI | 13 | 可按已观察的 AEP 云端路径实现，仍需实际账号/设备验证 |
| BLE | 7 | 不能据此宣称可由云端集成控制；需要另外恢复蓝牙协议与配对流程 |
| unknown | 1 | 保留元数据，不能自动认定可云端控制 |

全部 21 个 schema 合计 941 个数据点：bool 220、uint8 311、uint16 178、uint32 45、
binary 158、enum 29。`status_writable` 与 `status_readonly` 明确区分写权限。
包括大量预留、校准、时间同步与二进制程序字段，自动生成普通用户控制项时应避免默认暴露这些字段。

### 用户指定的 Z60 消毒柜与 P30 泡奶机

这两款设备均有明确的打包 WiFi 配置。产品商品名与 schema 的开发名称不同，
不能仅根据 JSON 文件中的 `name` 字段判断是否支持：

| 商品名 | 精确 productKey | schema 开发名称 | 名称证据 |
| --- | --- | --- | --- |
| 刘海妹妹全能Z60系列 | `k1mvpUrX2eb000000000000000000000` | 紫外线消毒器1822CW | bundle 模块 1268 对应产品的 `language.zh.PL_PRODUCT_NAME` |
| 智能泡奶机P30 | `k1mvp9K2mCI000000000000000000000` | 1270w | bundle 模块 1268 对应产品的顶层 `name` 与 `language.zh.PL_PRODUCT_NAME` |

以上为同一 productKey 的静态名称对应关系；尚未读取真实设备来确认具体硬件版本。
`products.json` 的 `name` 优先使用配置中的中文商品名，`schema_name` 保留原始开发名称，
`aliases` 记录两者；字段显示名优先取配置的 `language.zh["DP-" + field]`。
真正存在且不同于原枚举值的翻译另存为可选 `enum_labels`，不会改变协议中的原始 `enum` 数组。

P30 的主要数据点（来源 `assets/productConfig/k1mvp9K2mCI000000000000000000000.json`）：

| 字段 | 含义与声明 |
| --- | --- |
| `switch` | 总开关，bool，可写 |
| `boiling` | 本次煮沸除氯，bool，可写 |
| `mute` | 静音，bool，可写，true 为静音 |
| `temp_current` | 当前温度，整数 0..100，只读 |
| `temp_set` | 温度设置，整数 37..95，可写 |
| `wateryield` | 出水量设置，整数 30..500，可写 |
| `pattern` | 水模式，可写 enum：索引 0=`tap_water`、1=`pure_water` |
| `work_state` | 工作状态，只读 enum：0=`Heating`、1=`Keeping`、2=`Cooling` |
| `fault` | 只读故障值，描述包含 E1/E2/E3（提壶），未恢复完整数字对应关系 |
| `appointment_temp` / `appointment` | 预约温度 37..95、预约时间 0..1440，可写；时间 0 表示取消 |

P30 还声明奶瓶标记、定量适配、沸点检测、自清洁等维护流程；这些流程涉及多字段配合，
不应把所有 schema 声明都当成可任意独立设置的日常控制项。

Z60 的主要数据点（来源 `assets/productConfig/k1mvpUrX2eb000000000000000000000.json`）：

| 字段 | 含义与声明 |
| --- | --- |
| `switch` | 总开关，bool，可写 |
| `auto_switch` / `dry_switch` / `disinfection_switch` | 自动/烘干/杀菌模式开关，bool，可写 |
| `storage_switch` / `anion` / `night_mode` | 保管/等离子/夜间模式开关，bool，可写 |
| `status` | 只读状态，描述：0 关机、1 待机、2 工作、3 暂停 |
| `work_mode` | 可写模式，描述：0 未工作、1 自动、2 烘干、3 消毒、4 智能场景；底层 uint8 范围为 0..255，不能据此认定其他值有效 |
| `temp_mode` | 可写温度模式，描述：0 低温、1 标准；底层 uint8 范围为 0..255 |
| `auto_time` / `auto_time_lowtemp` | 自动标准/低温时间，各为 0..120 分钟 |
| `dry_time` / `dry_time_lowtemp` | 烘干标准/低温时间，各为 0..120 分钟 |
| `disinfection_time` | 杀菌时间，0..120 分钟 |
| `storage_time` | 保管时间，1..4320 分钟 |
| `scene_mode_switch` | 场景代码，描述包含 0 无场景、1 superF、2 温暖存储、3 清净除味、4 夜间、5 酸奶发酵、6 风味溶豆、7 宝宝棉品、255 自定义 |
| `clean_mode_time` | 清净除味时间，实际 UI 范围 3..60 分钟 |
| `night_mode_brightness` | 夜间亮度，1..100% |
| `work_remain_time` | 工作剩余时间，0..99999 分钟，只读 |
| `error` | 只读报错值，描述：0 none、1 e0、2 e1、3 e2、4 e3、5 e4 |

两款设备的状态读取和单字段控制复用上文 AEP 路径，以 productKey 与 MAC 定位设备。
静态 schema 证明字段存在及其编码，不证明每个模式切换的完整业务前置条件。

### 精确绑定的官方设备模板补充证据

APK 内未发现对应设备模板的 ZIP 或 JS，模块 1268 通过 `templateDownloadUrl`
明确绑定以下静态官方资源。此次只下载这两个精确 URL，并离线读取其中 `index.js`，
未运行模板、未探测其他 URL、未使用登录账号：

- P30：`https://xbgroup-static.gizwits.com/templates/com.bololo.rn.paonaiji.1270.1/com.bololo.rn.paonaiji.1270.1.1789032843.zip`
  SHA-256：`6d86c594242d1f9b1616c9a54cf0d314891683654afb72ce1c1e6949f4171910`。
- Z60：`https://xbgroup-static.gizwits.com/templates/com.bololo.1822cw/com.bololo.1822cw.1772529230.zip`
  SHA-256：`04502529c24a388101afc9455a3626d5d7d6435c35d5cf396a1ffb6bca8a8bc5`。

原文件位于 `analysis/templates/{p30,z60}.zip`。JS 成员、JS SHA-256、准确字节/字符偏移、
源码片段和有界搜索次数记录在 `analysis/templates/evidence.json`；这些仍是普通静态证据，
不能冒称 REA Evidence 或设备运行观测。

Z60 主模式切换的实际命令：

```javascript
// 模板 handleModeAlertConfirm 的等价表达；一次 sendCmd 请求。
const mapping = {auto: 'auto_switch', drying: 'dry_switch', sterilization: 'disinfection_switch'};
const command = {[mapping[targetMode]]: true};
if (previousActiveModeKey) command[previousActiveModeKey] = false;
sendCmd(command);
```

`work_mode` 的 1=自动、2=烘干、3=杀菌在上述调用后仅作为 App 缓存更新；
不应把其 uint8 全范围做成普通用户可随意写入的 0..255 控件。
`activeModeAttrKey` 精确优先级为 `auto_switch` → `disinfection_switch` → `dry_switch`，
取第一个真值字段；所有字段都为假时为 null。模板使用这组 bool 判断应关闭的前一个主模式，
而圆环状态展示另行使用 `work_mode`。存在状态未同步时，不能用后者臆测前者。
同模式停用发送 `{activeModeKey: false}`；场景停用实际发送 `{scene_mode_switch: 0}`。
没有观察到额外的 HTTP 请求序列或延时确认要求。
设置页的启动命令还会带上当前页的时间/温度设置；主页面的快捷启动不附带这些设置。

Z60 温度模式明确通过 `{temp_mode: 0}` 选择低温柔烘（45°C），
通过 `{temp_mode: 1}` 选择标准深烘（70°C）。实际用户滑块比底层 schema 的 0..120 更窄：

| 字段 | 模板实际范围 | 步长 |
| --- | --- | ---: |
| `auto_time` | 40..80 分钟 | 10 |
| `auto_time_lowtemp` | 80..100 分钟 | 10 |
| `dry_time` | 20..60 分钟 | 10 |
| `dry_time_lowtemp` | 50..90 分钟 | 10 |
| `disinfection_time` | 10..30 分钟 | 5 |

Z60 的 `status == 2` 在实际页面显示工作中，其余有模式状态显示暂停。
实际报错显示使用 `error & (1 << index)`，bits 0..4 分别显示 E0..E4，
因此 schema 中类似顺序枚举的错误描述不能作为准确译码依据。
App 主控制区域还参考 `error & 128` 来禁止操作，未恢复 bit 7 的物理含义。

P30 的模板确认以下行为：

- `pattern` 控制值是字符串：`{pattern: "tap_water"}`（自来水）、
  `{pattern: "pure_water"}`（直饮水）；状态显示函数同时兼容字符串与数组下标。
- 温度列表为 37..95、步长 1，另有常温选项，常温命令为 `{heat_off: true}`。
- 出水量设置列表为 30..500 mL，步长 5（schema 声明步长为 1）。
- 静音命令是 bool `{mute: true/false}`；夜间模式命令是整数 `{night_mode: 1/0}`，
  不是 bool；可选的二进制夜灯字段命令为 `{night_light: "01"}` 或 `{night_light: "00"}`。
  夜间模式下模板禁止静音按钮，设备关机、离线或有故障时也会禁用相应主控制。
- `work_state` 显示顺序为加热中、保温中、降温中、煮沸中；schema 原枚举只含前三项。
  页面也根据单独 `boiling` 字段优先显示煮沸，不扩充协议枚举来推断固件支持。
- 只在定量适配校准流程发现 `{water_output: true}`，该流程先提示接水盘、奶瓶放置等步骤。
  完整目标模板内 `detect_status` 仅出现两次（默认值与读取解构），未发现写入路径。
  不能把这些字段解释为已证实的日常远程出水功能。
- `auto_unlock` 是“放置已绑定奶瓶自动解开童锁”的设置；模板会读取账号偏好并设置设备，
  也提供用户显式切换。集成不能为了出水主动改变这个设置。
- 定量适配使用 `{quantitative_fit: true}` 开始，期间可能单独设置 `error_value`，
  退出使用 `{quantitative_fit: false}`；沸点检测开始为
  `{boiling_ponit_detect: true, function_status: 2}`，结束为
  `{boiling_ponit_detect: false, function_status: 0}`。
  清洁开始为 `{self_cleaning: true}`，取消为
  `{self_cleaning: false, function_status: 0, water_output: false}`。
  这些是带现场提示与多步骤状态的维护流程，裸 bool 实体不足以完整重现。
- 故障表实际显示：值 1 缺水；2 NTC 温度传感器短路、联系客户服务；4 水壶未放置。
  值 0、3 对应空文本。该事实来自模板，不代表不存在其他固件故障值。

`custom_*` 字段涉及场景记录同步与多字段组合，建议默认禁用实体；
单独改其数值不能据此保证完成 App 的自定义场景流程。
P30 的 `quantitative_fit`、`boiling_ponit_detect`、`self_cleaning`、`bottle_tag`、
`del_bottles`、`error_value`、`bottle_weight`、`bottle_list` 同样属于维护/标记字段，
应默认禁用或不提供普通独立写控件。`detect_status` 以只读状态处理，
`water_output` 不作为日常远程出水功能暴露。

模块 1291 的状态归一化函数 `Q` 直接遍历响应 `data` 的键：

- bool 值转布尔值。
- enum 字符串转为对应 `enum` 数组的下标；已经是数字的值保持数字。
- uint 值用 `parseFloat` 转数字，不再次缩放。
- binary 十六进制字符串转字节数组。

uint schema 的 `uint_spec` 可能包含非零 `addition`。实际 App UI 的
`ui.sections[].elements[].object.uint_spec` 已提供物理量边界。例如：

- `clean_mode_time` raw 0..57、addition 3；UI 为 3..60。
- `cabin1_clean_mode_time`、`cabin2_clean_mode_time` 同样为 3..60。
- 1711W 的 `back_home_time` raw 0..4319、addition 1；UI 为 1..4320。

生成的 `custom_components/bololo/products.json` 保留最小协议元数据，使用 UI 的物理量边界，
没有复制字体、图片、JS UI 代码、应用 secret 或产品 secret。

## 复现

在仓库根目录执行：

```powershell
py -3 tools/extract_apk.py com.gizwits.MainApplication.apk.1 --summary analysis/product-summary.json
```

工具只读取 ZIP 成员、JSON 产品定义与 bundle 的静态 product_list 声明，
不使用 eval、不导入或运行 APK JavaScript。生成结果为 13 wifi、7 ble、1 unknown。

## 覆盖与未解决问题

- 已回答：云端 host、请求封装、认证字段、列举/读状态/写控制路径、缓存状态形状、21 个 schema 与 20 个生产产品传输声明。
- 部分回答：token 生命周期、网络在线判定、分享权限；有静态调用证据，尚无实际响应记录。
- 未解决：服务器当前兼容性、真实登录/设备返回、控制确认延迟、设备是否即时上报、Home Assistant 中实际运行表现。
- 7 个 BLE 产品的 GATT/配对/数据包仍未实现。v0.1.1 后续增加本地直连与缓存启动，协议及验证边界见 [LAN记录](LAN.md)；尚未确认具体固件的本地兼容性。
- 静态声明是打包时行为的证据，不等于服务端当前行为或设备运行观测。
