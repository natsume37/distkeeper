# distkeeper

`distkeeper` 是一个使用 Python 和 uv 构建的跨平台制品版本管理 CLI。它把 APK、Windows、Linux 等发布包保存为不可变版本，并维护固定的最新下载地址。

核心模型是：

```text
repository + target + channel + version
```

仓库名、平台、扩展名以及所有对象路径都来自 YAML 配置。发布服务只依赖通用 `Storage` 协议，目前实现了本地目录和阿里云 OSS，后续可以独立增加 S3 或 MinIO 驱动。

## 环境

```bash
uv sync
cp distkeeper.example.yaml distkeeper.yaml
```

使用 OSS 时，从环境变量读取凭据：

```bash
export OSS_ACCESS_KEY_ID='...'
export OSS_ACCESS_KEY_SECRET='...'
# 使用 STS 时再设置 OSS_SESSION_TOKEN
export OSS_BUCKET='your-bucket'
```

凭据不要写进 YAML 或 Git 仓库。

## 发布流程

首次接管已经存在的 `dist/Android/psygo.apk`：

```bash
uv run distkeeper adopt \
  --repository psygo \
  --target android \
  --version 0.2.38
```

预览和发布新版本：

```bash
uv run distkeeper plan dist/psygo.apk \
  --repository psygo --target android --version 0.2.39

uv run distkeeper publish dist/psygo.apk \
  --repository psygo --target android --version 0.2.39
```

查询、校验和回滚：

```bash
uv run distkeeper list --repository psygo --target android
uv run distkeeper --json status --repository psygo --target android
uv run distkeeper tree
uv run distkeeper tree Android --limit 500
uv run distkeeper --json tree Android
uv run distkeeper verify --repository psygo --target android
uv run distkeeper rollback \
  --repository psygo --target android --version 0.2.38
```

面向 AI 或自动化调用时，先使用 `plan` 或 `rollback --dry-run`，再把返回的
`plan_id` 传给实际变更命令。计划绑定了源文件摘要、当时的 latest 版本和对象指纹；
如果执行前状态或输入发生变化，命令会失败并要求重新规划：

```bash
plan_json=$(uv run distkeeper --json plan dist/psygo.apk \
  --repository psygo --target android --version 0.2.39)
plan_id=$(python -c 'import json,sys; print(json.load(sys.stdin)["plan_id"])' <<<"$plan_json")
uv run distkeeper --json publish dist/psygo.apk \
  --repository psygo --target android --version 0.2.39 \
  --plan-id "$plan_id"
```

所有变更前都应检查 `status`。如果不使用 `plan_id`，可以传入
`--expected-current-version`，防止回退或发布覆盖已经变化的 latest。使用
`--json` 时，业务错误也会返回带有 `code` 和 `retryable` 字段的 JSON；AI 应根据
错误码决定重试、重新规划或请求确认。

发布顺序为：写入不可变版本对象、写入版本清单、切换固定下载对象、最后写入 latest 清单。同一版本和同一 SHA-256 可以安全重试，同一版本对应不同内容会被拒绝。

不要绕过工具直接覆盖 `dist`。建议同时开启 OSS Bucket 版本控制，将其作为误覆盖后的恢复保障。

同一个 `repository + target + channel` 应保持单发布者；在 CI 中使用 concurrency group 或等价机制串行化发布。OSS 开启 Bucket 版本控制后会忽略禁止覆盖请求头，因此版本控制负责灾难恢复，CI 串行化负责避免并发发布竞争。

默认安全边界是：本地发布文件来自配置文件旁的 `dist/`，OSS 制品写入 `dist/`，内部清单写入 `.distkeeper/`。如果源文件或目标键超出白名单，先查看 `plan`，确认无误后在变更命令中显式添加 `--confirm-outside-scope`。
