# 发版工作流

GitHub 仓库：[maycode0-0/bololo-smart-control](https://github.com/maycode0-0/bololo-smart-control)。
流程参考 ORVIBO Smart Control，适配本项目的 pytest、Ruff、Home Assistant 兼容环境及 ZIP 布局。

## 触发和检查

- 推送 `main`、推送 `v*.*.*` 标签或手动运行 **Publish BOLOLO Smart Control HACS release**。
- 其他分支推送与 PR 只运行验证；手动从其他分支运行也不发布。
- `unit-tests` 在 Ubuntu / Python 3.12 安装 `requirements-dev.txt`，运行 pytest、Ruff 和打包校验。
- HACS validation 校验集成与 HACS 清单；hassfest 检查 HA 元数据和翻译。
- 三项全部通过才执行发布，写权限仅给发布 job。无需配置 PAT，使用 GitHub 提供的 `GITHUB_TOKEN`。
- HACS 的 `brands`、`topics` 两项暂时排除：BOLOLO 尚无上游品牌条目，仓库尚无 topics。这只影响目录元数据检查。添加仓库 topics 后可移除 `topics` 排除，提交品牌后可移除 `brands` 排除。

工作流文件：[发布](../.github/workflows/hacs-release.yml)、[验证](../.github/workflows/validate.yml)。

## 发布新版本

1. 更新 `custom_components/bololo/manifest.json` 的版本，同时更新 README、CHANGELOG、诊断版本和 `docs/releases/<version>.md`。
2. 本地执行测试、Ruff 和 `python tools/build_release.py --expected-version <version>`。
3. 提交并推送到 `main`。检查通过后，流程创建 `v<version>` 标签和普通 GitHub Release / Latest。
4. 也可预先推送对应标签；标签必须与 manifest 版本一致。`bN` 版本同样发布为普通 Release，便于 HACS 识别更新。

版本号支持 `0.1.2` 和 `0.1.2b1`。已存在的标签不移动，已发布的安装包不覆盖。
如果 `main` 仍使用已发布版本，后续提交只验证，不将旧标签指向新提交；发布新代码必须递增版本。
如果流程在创建 draft 或上传一个资产后失败，可在同一提交重跑。已有资产先比较 SHA-256；相同则复用，不同则失败并要求新版本。
若 Release 存在而标签被删除，流程会停止，避免自动为历史版本重新打标签。

## 安装包布局

| 文件 | ZIP 内布局 | 用途 |
| --- | --- | --- |
| `bololo-smart-control.zip` | 根目录直接含 `manifest.json`、Python 模块及 JSON | HACS；手动使用时解压到 `<ha-config>/custom_components/bololo/` |
| `bololo-<version>.zip` | `custom_components/bololo/...` | 手动解压到 `<ha-config>/` |

脚本统一从当前源码生成两个 ZIP，固定条目时间与权限，排除缓存和日志，并回读校验每个文件。
`hacs.json` 的 filename 与 HACS 资产名保持一致。APK、原生库、反编译结果和本地环境均不进入 Git 或 Release。

## 仓库首次配置

仓库需启用 Actions，并允许工作流按 job 声明申请 `contents: write`。若组织策略禁止写入，检查会通过但发布会被 GitHub 拒绝，需要仓库管理员调整对应策略。
本项目使用 [MIT](../LICENSE)；这是集成源码的许可证，不改变厂商应用和第三方工件的权利。
