# 验证码登录协议静态核对

目标为用户提供的 BOLOLO Android App 2.1.86，APK SHA-256：
`d163fc6436fb9ee6322c71fa97d5aef81026f43805101f4ac3dbfc2b5cb58172`。
未执行 APK、未发送短信、未使用真实账号，也未连接设备。

本次复用已有 REA 通信模块 Evidence
`ev_60d6f99fa33b5ffc90d2000995a51586a757151f93c75ab29364dd04d8d4a123`，
本地完整记录为 `analysis/rea-focused-complete.json`。该 Evidence 覆盖
1161、1221、1222 等已有通信模块。本文补充的 1183、1463、1552、2204、2241
为从同一 APK 提取后进行的普通文本静态核对，不扩充或冒充上述 REA 的覆盖范围。
原始厂商 JavaScript 与完整 REA 记录仅保留在本地，不纳入公开仓库。

## 手机号、地区及版本分支

当前登录 UI 为独立包内模块 `assets/js-modules/2241.js`。它不在主
`index.android.bundle` 的模块集合中；仅搜索主 bundle 会漏掉实际调用者。

- 2241 登录：将 `phoneOrEmail` 复制为 `o`，匹配手机号时执行
  `o = "" + language.phoneCode + o`；`c = language.country`，随后 `login(o, code, c)`。
- 2241 发码同样拼接 `language.phoneCode`；邮箱不加前缀。
- 1183 `setCountry`：`country = e.countryCode`、`phoneCode = e.phoneCode`。
- 1552 中国条目明确为 `{phoneCode: "+86", countryCode: "CN"}`。

因此中国手机号的线协议示例为：

```json
{"mobile":"+8613800000000","mobileCountryCode":"CN"}
```

其中示例号码仅为占位值。`mobileCountryCode` 是 ISO 地区码，拨号区号是
另一个字段，二者不能混用。国家表中的 `phoneCode` 自带 `+`，不能从拼接
语句错误推断为无 `+` 的数字串。

2241 调用模块 1463 的版本比较函数。该函数判断第一参数是否低于第二参数。
版本低于 `1.9.31` 使用普通发码；否则先取 codeToken，再由 store 选择 V2。
当前 APK `2.1.86` 进入取 codeToken 的分支。2204 是仍随包分发的旧 UI，
始终调用普通发码接口，不能据此覆盖当前 UI 的选择。

## 请求合同

所有请求使用模块 1161 的 AEP 封装，base 为 `https://app.bololoapp3.com/`，
application ID 为 `91732ca20dfd4517b164c38aac162c43`。Headers 包含
`Content-Type: application/json`、`Version: 1.0`、`X-Gizwits-Application-Id`。
以下未登录接口的 wrapper 均传 `authenticated=false`，无需 Authorization。

有 body 时最终发送：

```json
{"appKey":"91732ca20dfd4517b164c38aac162c43","data":{},"version":"1.0"}
```

下表仅列出 `data` 内的内容；响应成功为 JSON `code == 200`，业务值在 `data`。

| 用途 | 方法与路径 | 请求 data |
| --- | --- | --- |
| 获取发码 token | GET `app/user/codeToken` | 无 body |
| 当前 UI 短信发码 | POST `app/user/v2/mobileCode` | `{mobile: prefixedPhone, lang: "zh", token: codeToken}` |
| 旧版短信发码 | POST `app/user/mobileCode` | `{mobile: prefixedPhone, lang: "zh"}`；仅当调用者提供 `type` 时添加该字段 |
| 邮箱发码 | POST `app/user/mailCode` | `{mail: email, lang: "zh"}` |
| 图形验证码 | GET `app/captcha/gen` | 无 body |
| 手机验证码登录 | POST `app/user/loginByMobile` | `{mobile: prefixedPhone, code, homeName: "我的家", mobileCountryCode: "CN", lang: "zh"}` |
| 邮箱验证码登录 | POST `app/user/loginByMail` | `{mail: email, code, homeName: "我的家", mobileCountryCode: country, lang: "zh"}` |

2241 对 codeToken 响应仅执行 `response.data || undefined`，直接传入
`loginState.getCode(account, codeToken, captchaId, captchaCode)`，没有访问
`.token` 子属性。静态观察不能证明真实服务端返回值一定是字符串或对象。
1221 `getCode` 中，codeToken 为真值时用 V2，为假值时用旧接口。

`homeName` 来自 1163 `getFirstAreaDefaultName()`。默认 key 是 `MY_HOME`，
1172 实际配置未覆盖 firstArea，包内中文翻译为“我的家”。

1222 的验证码登录 wrapper **没有添加 `refreshToken: true`**；该参数由
密码登录 wrapper 单独添加，不应在验证码登录中凭空套用。

## 图形验证码与错误码

发码请求同时存在 captchaId 与 captchaCode 时，1222 将它们放在 HTTP headers：

```text
X-Gizwits-Captcha-Id: <captchaId>
X-Gizwits-Captcha-Code: <captchaCode>
```

这些字段不进入 AEP 的 `data`。1221 `fetchCaptcha` 对返回 data 的处理是：

- ID：`captchaId || id || ""`。
- 图片：`captchaImage || image || imageBase64 || ""`。
- 不以 `data:image` 开头时添加 `data:image/jpeg;base64,` 前缀。

| 场景 | 云端 code / message | App 静态行为 |
| --- | --- | --- |
| 发码 | 610 | 标记需要图形验证码，并获取新图 |
| 发码 | 1000018 | 图形验证码错误；2241 确认弹窗时会再取新图 |
| 发码 | 612 | 图形验证码过期，获取新图 |
| 发码 | 8000005 | 验证码发送频繁 |
| 发码 | 53 | 手机号接收超过频率限制 |
| 发码 | message 精确为 `yunpian send sms response error: code = 8` | 不要频繁获取验证码 |
| 登录 | 1000034 | 验证码错误 |
| 登录 | 1000035 | 验证码过期 |
| 登录 | 504 | 账号错误 |

旧 UI 2204 另对 611 显示图形验证码错误，当前 2241/store 使用 1000018。
发码成功后 App 清除图形验证码状态，并开始 60 秒倒计时。

## 会话与续期

验证码和密码登录都调用统一 1221 `loginSuccess(data)`。读取字段为：

```text
uid
jwtAuthenticationDto.token
refreshToken
createdAt
expiredAt
```

模块 1161 在有效期过半时执行 `POST app/user/refresh_token`，AEP data 为
`{clientId: APP_ID, refresh: true, refresh_token: savedRefreshToken}`，
Authorization 使用当前 raw token。响应字段为 `token`、`refresh_token`、
`created_at`、`expired_at`、`uid`；App 仅在返回非空 refresh_token 时保存。
时间比较使用 Unix 秒。短信登录能否实际返回可续期 token、服务端是否允许
App 和 HA 同时持有会话，仍需账号联调。

## 静态锚点

以下为 UTF-8 解码文本的 **零基字符偏移**，不是字节偏移；模块均为单行。

| 模块 | 字符偏移 | 锚点 |
| --- | ---: | --- |
| 1161 | 2260 / 2532 | AEP 构造函数 `k` / 续期函数 `w` |
| 1183 | 2198 | `setCountry` |
| 1221 | 2259 / 3309 / 6787 / 10728 | `fetchCaptcha` / `login` / `getCode` / `loginSuccess` |
| 1222 | 576 / 1095 / 1546 / 1789 | `getCaptcha` / `getMobileCode` / `getMobileCodeToken` / `getMobileCodeV2` |
| 1222 | 2229 / 2519 | `loginByMail` / `loginByMobile` |
| 1552 | 24660 | 中国 `phoneCode: "+86", countryCode: "CN"` |
| 2241 | 3531 / 4354 / 4460 | 当前 UI 登录 / 取 codeToken / 发码调用 |

用于复核的新补充模块 SHA-256：

```text
1183 7b6e8fa301be22bd1b58b81ef8f37a207dd795f886c8c4400fadf248e2063c76
1463 9a6da642d1cbd85b3a07ebb651051495e0824961f7571f423ea7f435492f6c58
1552 ac6bd4846476946a9eb8934eccffd7a1c0620d2aa1f2d58be04d52e7f7ea09fc
2204 840324d73a1e7fbc415e391301281e545a39966e4e751b355d6fafafb79eaf73
2241 39d0d8685d10f22265a412e2ec7638e82ccd2bbb25e30a0a379a41aa2c0a2e3f
```

## v0.1.2 发码未送达调查

用户报告：HA 提交后进入短信验证码页面，没有红色错误或图形验证；同一账号在官方 App 能收码。
已确认用户在手机号字段填写大陆 11 位裸号，拨号区号单独填 86，不是重复拼接区号。

只读在线检查 `GET app/user/codeToken`：HTTP 200，业务 code 为字符串 `200`，data 为非空字符串，
未设置 cookie。本检查没有提交手机号、没有请求发送短信，也没有记录或输出令牌。
这仅证明调查机器上的令牌接口可达，不能证明用户 HA 的发码请求或短信送达成功。

重新核对包内模块 2241/1223：手机号判断为 `/^(\d)+$/`，不会因 19 等大陆号段而跳过区号拼接。
现有 `+86` 格式与 App 静态代码一致。未找到支持改为裸号、去掉 `+` 或增加猜测签名字段的证据。

现有 HA 配置流程在云端返回业务 code 200 后进入验证码步骤；该状态只能表示请求被接口接受，
不能用作运营商已送达的证据。真实发码请求的响应与网络差异仍未取得，根因尚未确定。

可由用户在保留 HA 验证码页面、不点击重发的条件下，通过官方 App 获取新的验证码并只在 HA 输入，
用来进一步区分短信请求和验证码交换的故障；是否可跨 App 会话使用验证码仍待此验证。


### v0.1.2 发码未送达的定向复核

本轮仅做静态核对，不发送短信、不执行 APK。尚未找到能解释“HA 发码返回成功但未收到短信、官方 App 能收到”的确定协议差异。服务端接受发码请求不能证明短信实际送达。

1. **手机号正则与默认前缀**：2241 的 `phone` 来自 1223，精确为 `/^(\d)+$/`，所有纯数字大陆号段均匹配，不存在 19 号段不拼前缀的问题。启动模块 746 的 `setCountryCode` 依据已存国家码或原生 locale 查国家表，直接设置 `language.phoneCode = t.phoneCode || '+86'`；CN 表为 `+86`。正常初始化后的前缀有明确证据为 `+86`，不是空串或 `86`。1183 的空串只是构造初值。
2. **模板与 AppID**：1163 将默认配置与本地 1172 深合并后将 `f=b`，`getTemplateConfig()` 返回 `f`。1161 每次取得此配置并按 Android/iOS 平台选 AppID。当前包内 Android 值与集成相同。已检查代码未见运行时替换 `f` 的入口；1213 允许 `app.cloudServiceInfo` 覆盖服务器，但包内实际配置未提供此项。静态分析不能证明用户手机实际使用的已更新包/运行时配置与此 APK 始终相同。
3. **JS 发包**：1161→1227→1228→96→107→109→原生 Networking。1227 仅添加 10 秒 timeout、检查 HTTP 状态并解析 JSON；1228 原样转交 Request headers 与字符串 body；107 将 headers 对象转为键值数组并交原生。未发现业务签名、密码学处理或手机号编码转换。RN XHR 默认 `withCredentials=true`，可使用原生 cookie jar；本次未读取手机 cookie，不能确认其实际内容。Sentry 可能附加 `sentry-trace`/`baggage`，未发现它们是短信发码条件的依据。
4. **原生网络**：REA/JADX 定向检查以下三个类。`MainApplication.onCreate` 仅初始化 SoLoader、ReactFeatureFlags 与生命周期分发；`NetworkingModule.sendRequestInternal` 的普通字符串 POST 路径按 UTF-8/媒体类型字符集直接生成 RequestBody，不做业务签名或 body 改写；`OkHttpClientProvider` 默认 builder 配置 timeout 和 ReactCookieJarContainer。网络层可补默认 User-Agent，但没有证据表明仿造 User-Agent 能解决送达问题。
5. **自定义网络入口**：全 APK 对 `NetworkingModule.setCustomClientBuilder`、`OkHttpClientProvider.setOkHttpClientFactory` 的 REA 静态引用结果均为 0、coverage=`complete`。这缩小了通常的自定义签名拦截器入口，不排除反射、其他未覆盖入口或用户手机版本差异。此次遵循限定范围未扩展其他原生类。`extractHeaders` 的反编译循环含明显不合理控制流，不能据此断言实际运行会丢弃 headers；其文本仅作为反编译结果保留。

原始证据保存在本地 `analysis/sms-native/`（不上传源码/完整记录）：

| 定向操作 | Evidence ID |
| --- | --- |
| MainApplication.onCreate | `ev_80c20be1d7a87f861dd56ab20a6965d00a3b1cbcd4abe42c0e2ca781c2a83f5b` |
| NetworkingModule.sendRequestInternal | `ev_f0f0dea47e6f6394715c6cbd4816b516b5afa7232243569f294045dcd4a27b8f` |
| NetworkingModule.extractHeaders | `ev_f0345c2f9ec081cfe3c6449458ab46d87d76b164dbc57e53e202900df3d81199` |
| OkHttpClientProvider.createClientBuilder | `ev_72b86f95377965c17fcab3e85deb9999d56fabb098c125338f3e9d10dfa75a69` |
| OkHttpClientProvider.createClient(Context) | `ev_15f87b032e9b38f7b410bfb417e9a4614a451108920b7023e98cfd12a919d44a` |
| setCustomClientBuilder incoming references | `ev_61452f39ac21d906005551c98c85713314e11d4528ec0d7b81b0ab7cae71a97f` |
| setOkHttpClientFactory incoming references | `ev_3860f08c55fe5e975f0c8cf8201bb35575f059815ce9a411069f83f29a856498` |

这些证据说明的是给定 APK 的静态调用与反编译结果，不是用户手机的实际网络抓包。当前应记录安全的阶段/HTTP状态/业务code诊断，配合实际验证码交换结果继续定位，不能凭猜测修改号码格式或添加签名 headers。

### 配置页面截图的新证据

用户随后提供截图，页面实际字段为 `captcha_code`，并非短信验证码步骤的 `code`。
由此可确定流程已经进入图形验证码分支，不能继续依据用户最初对页面的描述推断“短信发码 code 200”。
图形验证尚未通过可以解释没有发出短信。截图同时缺少新步骤的中文标签、标题和图片描述，
疑似使用升级前的前端翻译缓存；需由重启 HA 与无痕窗口验证，不能仅凭截图确认缓存根因。

另外复现两个独立提交问题：`code` 与 `resend=true` 同时提交会优先重发而跳过验证码验证；
图形验证码错误/过期重新获取图片时未显示错误，使用户看到提交后回到原页面。
对应修复为有验证码时优先验证，以及显示图形验证码错误/过期提示。

### 图片显示故障与兼容性修复

用户在重启并使用无痕窗口后确认中文标签恢复、图片仍不显示，排除“仅翻译缓存”足以解释全部问题。
已确认 v0.1.2 的内嵌图片未在用户配置页显示，阻断人工图形验证；具体 HA/浏览器版本的运行限制仍未知。

核对 Home Assistant 官方前端源码：

- `src/dialogs/config-flow/show-dialog-config-flow.ts` 的 `renderShowFormStepDescription` 设置
  `.allowDataUrl=${step.handler === "zwave_js"}`。
- `src/components/ha-markdown.ts` 和 `ha-markdown-element.ts` 默认 `allowDataUrl=false`。
- **继续追踪到 `markdown-worker.ts` 后发现，上述 allowDataUrl 专门放行 data href 下载链接，不能据此推断图片 src 必然被过滤。**
  当前 `dev` 使用的 `xss@1.0.17` 的 `safeAttrValue` 明确允许 `data:image/`，也允许以 `/` 开头的同源路径。
  因而先前“HA 配置流程统一过滤所有 data 图片”的推断不成立，已更正；不能把源码观察当作用户环境的运行证明。

只读 `GET app/captcha/gen` 返回 HTTP 200、业务 code 字符串 200，captchaImage 为原始 base64，
解码为 4802 字节、具有 JPEG 文件头的图片；本次只记录格式和大小，未输出、保存或解答挑战内容。
这排除了当前调查机器上该端点总返回非图片字符串的假设，不替代用户 HA 的响应观测。

来源为本次只读取得的官方 frontend `dev` 分支源码：
[配置流程描述渲染](https://github.com/home-assistant/frontend/blob/dev/src/dialogs/config-flow/show-dialog-config-flow.ts)、
[Markdown 组件](https://github.com/home-assistant/frontend/blob/dev/src/components/ha-markdown.ts)、
[渲染 Worker](https://github.com/home-assistant/frontend/blob/dev/src/resources/markdown-worker.ts)、
[xss 1.0.17 属性规则](https://unpkg.com/xss@1.0.17/lib/default.js)。
本地副本保留在 `analysis/tmp/ha-frontend/`。这些是源码观察，不冒充前述 APK REA Evidence。

v0.1.3 改为同源 HA 图片 view，保留 `requires_auth=True`，通过 `async_sign_path` 给单张图片 5 分钟访问链接。
图片短时保存在内存，限制类型、大小和数量；刷新/发码成功移除旧图、到期自动清理。
HTTP 回归使用 HA 真正的认证中间件验证签名、401 与 404 行为；未执行真实短信请求或代填挑战。
同源图片能否在用户实际 HA 前端显示仍需升级后确认。
