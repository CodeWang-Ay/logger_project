# Logger Project

一个面向 FastAPI 的轻量日志管理示例，提供：

- 基于 `contextvars` 的请求级 trace ID，并发请求之间互不污染
- 支持经过校验的 `X-Trace-Id` 和 W3C `traceparent`
- 普通日志、错误日志和 JSONL 结构化日志
- 每日轮转及默认保留 30 天
- 可选的 YAML 配置热更新
- 自动记录 HTTP 方法、路径、状态码、耗时和未处理异常

## 安装与启动

```bash
uv sync --extra server
uv run python main.py
```

`main.py` 默认监听 `0.0.0.0:8000`。开发时如需自动重载：

```bash
uv run uvicorn server:app --reload
```

验证接口：

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/orders
curl -H "X-Trace-Id: client-request-001" http://127.0.0.1:8000/users/1
```

每个 HTTP 响应都会包含 `X-Trace-Id`。

## 配置

配置文件位于 `logger_utils/config.yml`：

```yaml
log_config:
  name: websocket_proxy_logs
  log_folder: websocket_proxy_logs
  log_level: DEBUG
  to_console: true
  console_format: colored
  console_stream: stdout
  file_output: true
  json_format: true
  rotation_mode: time
  rotation_when: midnight
  rotation_interval: 1
  max_bytes: 52428800
  backup_count: 30
  redaction_enabled: true
  max_message_length: 16384
  max_exception_length: 32768
  capture_sqlalchemy: false
  sqlalchemy_level: WARNING
  redact_fields:
    - password
    - token
    - authorization
    - cookie
    - secret
```

`log_level` 控制控制台最低等级；文件日志仍完整记录 `DEBUG` 及以上信息。

文件输出支持两种轮转模式：

```yaml
# 每天午夜轮转
rotation_mode: time
rotation_when: midnight  # 也支持 S/M/H/D/W0-W6
rotation_interval: 1
backup_count: 30
```

```yaml
# 单文件达到 50 MiB 后轮转
rotation_mode: size
max_bytes: 52428800
backup_count: 5
```

两种文件轮转模式都只适合单进程。多 worker 仍应使用
`file_output: false` 的 JSON stdout 模式。

若需要监听配置文件并实时应用改动：

```bash
uv sync --extra watch
```

然后在应用生命周期中调用：

```python
from logger_utils.logger_manager import logger_manager

logger_manager.start_config_watcher()
# 应用关闭时：
logger_manager.stop_config_watcher()
logger_manager.close()
```

没有安装 `watch` 额外依赖时，普通日志功能仍可正常使用。
热更新可修改等级、目录和输出开关；logger 名称需要重启应用后生效。

## 在业务代码中使用

```python
from logger_utils import get_logger

logger = get_logger(__name__)
logger.info("创建订单 order_id=%s", order_id)

try:
    do_something()
except Exception:
    logger.exception("创建订单失败 order_id=%s", order_id)
```

默认脱敏过滤器会处理密码、令牌、Authorization、Cookie、API Key 等常见字段，
包括嵌套字典、日志消息和异常文本。脱敏是最后一道保护，业务代码仍不应主动记录
银行卡号、身份证号或完整请求体等敏感信息。

仅作为组件安装时，FastAPI 和 Uvicorn 不会进入核心依赖：

```bash
pip install .
pip install ".[watch]"   # 需要配置热更新
pip install ".[server]"  # 需要运行仓库内的演示服务
```

`get_logger(__name__)` 返回默认基础 logger 的子 logger。多个业务模块共享同一组
handler，但日志中的 logger 名称仍能区分模块。单纯执行 `import logger_utils` 不会
创建目录、打开日志文件或启动线程。

## 输出目录

```text
websocket_proxy_logs/
├── debug/debug.log
├── error/error.log
└── json/app.jsonl
```

运行日志已通过 `.gitignore` 排除，不应提交到代码仓库。

> `TimedRotatingFileHandler` 适合单进程运行。多 worker 或容器生产环境建议输出到
> stdout，再交给日志采集器完成轮转与集中存储，避免多个进程同时轮转同一个文件。

## 生产部署模式

多 worker、Docker 或 Kubernetes 环境建议使用 JSON stdout，关闭应用内文件轮转：

```yaml
log_config:
  name: order_service
  log_level: INFO
  to_console: true
  console_format: json
  console_stream: stdout
  file_output: false
  json_format: false
  redaction_enabled: true
  max_message_length: 16384
  max_exception_length: 32768
  capture_sqlalchemy: false
```

对应的数据流：

```text
Python worker → JSON stdout → Docker/Kubernetes runtime
              → Fluent Bit / Vector / Filebeat → Loki / Elasticsearch
```

这种模式不共享日志文件，因此可以安全运行多个 worker。日志传输、重试、缓冲、
压缩和保留周期由采集器负责，应用不会直接依赖 Kafka、Redis 或 Elasticsearch。

单进程或本地开发环境可以继续使用 `file_output: true`。不要让多个进程同时轮转
同一个文件。

仓库已提供等价的生产模板：

```python
from logger_utils import close_logger_manager, get_logger

config = "logger_utils/config.production.yml"
logger = get_logger(__name__, config_path=config)

# 应用关闭时释放 watcher 和 handlers
close_logger_manager(config)
```

SQLAlchemy 日志默认不由组件接管，避免覆盖宿主应用的全局日志设置。确需统一采集时，
设置 `capture_sqlalchemy: true`，并通过 `sqlalchemy_level` 控制最低等级。

`max_message_length` 和 `max_exception_length` 用于限制超大单行日志，避免容器运行时
或日志采集器截断过长记录。

## 测试

测试仅使用 Python 标准库：

```bash
uv run python -m unittest discover -s tests -v
```
