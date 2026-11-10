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
