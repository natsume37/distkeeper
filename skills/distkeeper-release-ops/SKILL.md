---
name: distkeeper-release-ops
description: "规范 distkeeper 面向 AI 的 CLI 制品发布、OSS 测试与验证流程；适用于 publish、adopt、verify、rollback 等操作，不用于构建制品或图形界面。"
---

# Distkeeper Release Operations

## 适用范围

将已经构建好的 APK、EXE、DEB 等制品归档到 distkeeper 配置的对象存储，并维护不可变版本和固定 latest 地址。该仓库是纯 CLI 工具；优先使用 `distkeeper --json ...`，不要新增或恢复 GUI/PySide6 代码。

## 不可违背的不变量

- 版本对象必须不可覆盖；latest 是可切换的固定键，版本清单和 latest 清单必须同步写入。
- 对象键使用相对 POSIX 路径，不带开头 `/`；版本归档目录与 latest 固定键可以不同。当前正式 Android `app_release` 约定为 `dist/android/app_release_{version}.apk`，latest 为 `dist/app_release.apk`；文件名大小写必须逐字匹配配置和现网约定。
- Windows、Linux 等目标使用各自明确配置的 `dist/<操作系统>/` 归档目录；不要从 latest 键猜目录或大小写，也不要把一个目标的目录规则套到另一个目标。
- 默认本地源目录白名单是配置文件旁的 `dist/`，默认 OSS 写入白名单是 `dist/` 和工具内部的 `.distkeeper/`；白名单可在 `safety.allowed_source_roots` 与 `safety.allowed_write_prefixes` 中收紧或扩展。
- 同一 `repository + target + channel` 的发布应串行；同版本不同 SHA-256 必须停止并报告冲突。
- OSS 凭据只能来自环境变量或受控的 `.env` 加载，不得打印、写入 YAML 或提交 Git。
- 任何真实 OSS 写入、覆盖 latest、回滚或远程清理，都必须在当前任务中获得明确授权；授权不等于可以扩大目标范围。

## 操作流程

1. 先检查输入文件、扩展名、版本格式、目标配置和 SHA-256，输出将要修改的逻辑键；先执行 `plan`，不要跳过预览。
2. 测试优先使用临时本地存储。若必须测试真实 OSS，使用唯一测试前缀和明确的测试制品名，确认桶、区域、前缀及保留/清理策略后再写入。测试 fixture（如 `test.md`）应使用临时配置允许的扩展名，不要放宽正式生产配置。
3. 首次管理已有 latest 时只执行一次 `adopt <旧版本>`；后续发布使用 `publish <新版本>`，不要手工重命名、删除或覆盖版本对象。版本对象必须落在对应的 `dist/<操作系统>/` 目录，latest 只使用配置指定的固定键。
4. 写入后立即执行 `verify`，再用 `list`/`tree` 检查清单和最终键。需要回退时先确认目标版本完整，再执行 `rollback` 并再次校验；发现历史归档路径错误时，按 [OSS runbook](references/oss-runbook.md) 的迁移流程处理。
5. 用 JSON 结果和进程退出码判断成功：`plan` 返回 `scope_violations` 和 `requires_confirmation`；普通错误为非零，完整性校验失败必须阻断后续自动发布。报告版本、逻辑键、大小、SHA-256、测试前缀和是否触碰正式对象。
6. 若计划显示越界，先停止并让用户确认；只有在明确确认后，才为变更命令添加 `--confirm-outside-scope`。不要把该参数作为默认自动化参数。

## 错误归档路径的修正

当版本对象已经写入错误目录（例如误写成 `dist/app_release_0.3.5.apk`）时，不能先删除再上传。只处理用户明确指定的版本：服务端复制到正确的 `dist/android/` 键，完整校验大小和 SHA-256，更新对应版本清单的 `artifact.key` 与 `filename`，再次确认 latest 未被并发改变，最后才删除旧键。迁移后必须检查旧键不存在、新键存在、清单和 `tree` 一致；latest 固定键本身不应因归档迁移而改变。

## 实现与回归要求

修改代码时保持 `ReleaseService` 与存储驱动解耦，并为每个行为补充 pytest 回归测试；至少运行 `uv run pytest`、`uv run ruff check .` 和 `uv run mypy src`。OSS SDK 可能把 404 包装在通用异常中，`OssStorage.stat()` 必须解包后将其视为“对象不存在”，并保留对应测试。

涉及真实写入的详细命令顺序见 [references/oss-runbook.md](references/oss-runbook.md)。
