# OSS Runbook

仅在用户明确要求实际远程写入时使用本手册。缺少仓库、目标、渠道、版本、桶或前缀时，先暂停并询问；不要猜测正式路径。

## 目录与命名约定

当前正式 Android `app_release` 的键为：

```text
版本归档：dist/android/app_release_<version>.apk
固定 latest：dist/app_release.apk
```

例如 0.3.4 应为 `dist/android/app_release_0.3.4.apk`。本地源文件可以使用构建系统生成的其他文件名（如 `com.creativekoalas.psygo_0.3.4.apk`），但上传后的 latest 文件名必须保持 `app_release.apk`。Windows、Linux 等目标必须使用配置明确的对应操作系统目录及大小写，不得自行推断。

## 预检

```bash
test -f /path/to/artifact.apk
sha256sum /path/to/artifact.apk
uv run --env-file .env distkeeper --config /path/to/distkeeper.yaml --json plan \
  /path/to/artifact.apk \
  --repository psygo --target android --version 0.2.6
```

确认输出中的 versioned key、latest key、bucket、region 和 prefix；如果 JSON 中 `requires_confirmation` 为 `true`，不得直接写入。对象键应类似：

```text
dist/android/app_release_0.3.4.apk
dist/app_release.apk
```

## 首次接管与发布

如果 latest 尚未被 distkeeper 管理，先归档一次已有版本：

```bash
uv run --env-file .env distkeeper --config /path/to/distkeeper.yaml --json adopt \
  --repository psygo --target android --version 0.2.5
```

然后发布新制品：

```bash
uv run --env-file .env distkeeper --config /path/to/distkeeper.yaml --json publish \
  /path/to/artifact.apk \
  --repository psygo --target android --version 0.2.6
```

默认要求源文件在 `dist/` 且写入键在 `dist/` 或 `.distkeeper/`。若确实需要越界，先查看 `plan` 并取得用户明确确认，再追加：

```bash
uv run --env-file .env distkeeper --config /path/to/distkeeper.yaml --json publish \
  /path/to/artifact.apk \
  --repository psygo --target android --version 0.2.6 \
  --confirm-outside-scope
```

每个目标（Android、Windows、Linux）分别执行；目标含多个扩展名时显式传 `--extension`。同一命令可安全重试，但版本已存在且 SHA-256 不同则必须停止。

## 发布后检查

```bash
uv run --env-file .env distkeeper --config /path/to/distkeeper.yaml --json verify \
  --repository psygo --target android
uv run --env-file .env distkeeper --config /path/to/distkeeper.yaml --json list \
  --repository psygo --target android
uv run --env-file .env distkeeper --config /path/to/distkeeper.yaml --json tree dist
```

## 已有错误归档的迁移

如果发现版本归档位于错误目录（例如 `dist/app_release_0.3.5.apk`），按以下顺序操作；不要直接删除后再上传：

1. 列出旧键、新键和对应清单，确认只涉及用户指定的版本，并记录旧对象的 ETag、大小和 SHA-256。
2. 使用服务端 copy 创建 `dist/android/app_release_0.3.5.apk`，禁止覆盖；复制完成后下载校验完整 SHA-256。
3. 更新 `.distkeeper/.../releases/0.3.5.json` 的 `artifact.key` 和 `artifact.filename`，保留版本、摘要和 latest 键；确认 active latest 清单没有并发变化。
4. 再次校验新对象和清单后，才删除错误的 `dist/app_release_0.3.5.apk`；删除后确认旧键确实不存在。
5. 用 `verify`、`list` 和 `tree dist` 检查最终布局。迁移归档不应改变 `dist/app_release.apk` 的内容或键。

若要迁移其他版本，必须单独列入本次授权范围；不要因为目录规则不一致而顺手移动未指定的制品。

## 回滚与报告

回滚前确认目标版本的清单和对象都存在：

```bash
uv run --env-file .env distkeeper --config /path/to/distkeeper.yaml --json rollback \
  --repository psygo --target android --version 0.2.5
```

随后再次 `verify`。向用户报告实际前缀、完整逻辑键、版本、大小、SHA-256、每步结果及是否修改正式 latest。测试对象应按用户要求保留或清理；远程删除不是默认步骤。
