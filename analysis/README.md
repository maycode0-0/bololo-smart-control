# 分析资料说明

Git 仓库保留调查报告、证据 ID、工件哈希和产品元数据摘要。原始 APK、JNI 库、完整 REA 输出、JADX/Ghidra 数据库以及厂商页面源码仅保留在本地分析目录，不上传 GitHub，也不进入安装包。

因此其他人克隆仓库后，报告中引用的部分原始 JSON、源码和二进制文件不会存在。它们的本地路径用于已有分析环境复核，不代表仓库内可直接打开的附件。
可使用自己的同版本 APK 和 `tools/extract_apk.py`、`tools/extract_lan_profiles.py` 复现公开协议元数据；REA 和原生库调查的方法、版本、哈希及覆盖限制见以下文档。

- [云端与设备模板调查](FINDINGS.md)
- [REA 方法和证据](REA.md)
- [LAN 接入与边界](LAN.md)
- [原生 LAN 编解码静态锚点](lan/NATIVE_PROTOCOL.md)

静态观察和模拟设备测试不等同于真实设备验证。Z60/P30 仍需确认具体固件及网络兼容性。
