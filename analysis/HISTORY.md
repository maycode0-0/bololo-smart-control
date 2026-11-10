# 记录与奶瓶添加的静态来源

本次使用仓库已保留的 APK JavaScript 与官方 P30/Z60 模板做普通静态文本核对，
没有执行第三方 JavaScript、登录真实账号或连接设备。没有新增 REA Evidence；
这些来源不是在线请求或真机执行的证明。

## 官方出水历史

- `analysis/assets/js-modules/3754.js` 的 `getFoamDetailAPI`（字符偏移约 2122）
  使用正常 AEP 封装：POST `bololo/appdataSata/foam/detail/{sno}/{type}`，
  body 对象为 `{current,size,query:{startTime,endTime}}`。
- 官方 P30 `analysis/templates/p30/com.bololo.rn.paonaiji.1270.1/index.js`
  的 `HistoryScreenNew` 查询（约 206619）传 `type:3`，未加宝宝筛选，是全部出水历史。
  约 59673 的解析读取 `data.records`、`data.total`，记录 `date` 和 JSON 字符串
  `data`，其中 `waterOutput` 为出水量。约 166193 的 `type:2` 另有宝宝筛选，
  不作为设备全部出水历史。
- 旧模块 1271 同名历史请求使用 `disabledWapper:true`。实现依据新版 3754 的
  对象封装，不能将新旧请求封装混为一谈；真实服务兼容性仍待验证。

完整分页回填、最近一天增量核对、私有 Store、显示最近 50 条均是本集成的设计，
不声称官方 App 采用同样的缓存策略。首次历史窗口从时间戳 0 开始；服务器的保留期及
当前账号可查询的记录范围未知。更早历史的编辑和删除不在增量核对窗口内。

## 奶瓶添加

官方 P30 模板约 153737–155900：开始/取消发送 `{bottle_tag:0,new_bottles:0}`，
清空托盘后发送 `{bottle_tag:1}`，放置空瓶后发送 `{bottle_tag:2}`。
`new_bottles` 1–253 为有效编号，0 等待；255 提示未放瓶或重量太轻。
254 的日志/步骤文案为重量重复，但弹窗文案为奶瓶数量上限，因此只报告标记拒绝，
不擅自确定唯一原因。约 160773–161304 使用设备上报的 `bottle_weight` 原值保存，
不在传云端前除以 10。奶瓶名限制 7 字符（约 29868）。

模块 3754 的 `queryBabyListAPI`、`queryBottleByFamilyAPI` 与 `createBottleAPI`：

| 操作 | 请求 |
| --- | --- |
| 宝宝列表 | GET `bololo/app/baby/{productKey}/{mac}` |
| 奶瓶列表 | GET `bololo/app/bottle/{productKey}/{mac}` |
| 保存奶瓶 | POST `bololo/app/bottle`，对象 `{name,icon,num,babyId,bottleWeight}` |

官方保存后还会启用 `auto_unlock`。本集成不修改该偏好，只重置奶瓶标记状态。
称重命令及保存 POST 均不自动重放；保存不明时由用户在 App 核实。

## 本地烧水、消烘与覆盖缺口

产品 JSON 的 `boiling` 表示本次煮沸中，`detect_status` 表示出水中；
`record` 是本次出水量，不是累计水表。消毒柜的 `status` 为 0 关机、1 待机、
2 工作、3 暂停，`work_mode` 的 1/2/3 对应自动/烘干/杀菌；双舱带 `cabin1_`、`cabin2_`。
据此记录本地观察到的运行及退出，不将停止推断为正常完成，也不统计未知智能场景。

对保留的全部 `analysis/assets/js-modules/*.js`、P30/Z60 官方模板做有界搜索，
未找到可核实的烧水或消烘历史接口。不能用出水接口的其他 type 数字猜测这两类历史。
因此官方出水历史已实现静态契约，官方烧水/消烘历史仍未实现；后续需要匹配版本的官方
页面、接口文档或脱敏请求样例以继续核对。本地轮询记录不能替代这两类官方历史。

## 工件摘要

- P30 `index.js` SHA-256：`a5e6f4c5e23a0139b3a6ef504c1fd4be6ec9b29e71d1856aa7bae4baf81c4b43`。
- 模块 `3754.js` SHA-256：`ae786e3a0590742f858eb1a697a8d84b39ca8f81ec23aad26403c3b78aec2732`。
- 模板下载 URL、ZIP 摘要与既有设备控制证据见 [FINDINGS.md](FINDINGS.md)。
