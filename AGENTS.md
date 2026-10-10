# AGENTS.md

本项目是 AstrBot 聊天机器人的本子与里番聚合插件。所有代码注释、日志、文档、用户可见字符串均使用**中文**。

## 项目结构

```
main.py                  # 插件入口 HentaiCollectorPlugin(Star)：指令注册、权限、去重、异常兜底
_conf_schema.json        # AstrBot WebUI 配置 schema
metadata.yaml            # 插件清单
requirements.txt         # aiohttp / curl_cffi / jmcomic / pillow
docs/                    # 文档索引、架构、配置说明、源站接口备忘
core/
  constants.py           # 全局常量（Config 类）
  logger.py              # 全局日志实例
  errors.py              # CollectorError 异常族（消息直接面向用户）
  types.py               # 数据契约：WorkInfo / SearchQuery / WorkPage / ImageTask / MediaBundle / FetchResult
  config_manager.py      # 配置 dataclass + 类型兜底 + 源站工厂
  net/                   # HttpClient（aiohttp / curl_cffi 双后端）
  source/                # SourceManager + platform/ 下各源站（继承 BaseSource）
  downloader/            # 图片并发下载、视频下载
  packager/              # 流式 PDF、ZIP、文件命名
  storage/               # 任务目录缓存、搜索会话、推送记录
  service/               # WorkService 业务编排、指令参数解析
  message_adapter/       # Delivery 投递、WorkPresenter 展示、格式化、封面拼图
  push/                  # 推送任务解析、调度循环
```

## 分层约定

- 依赖只允许自上而下：`main.py` → `message_adapter/` / `push/` → `service/` → `source/` / `downloader/` / `packager/` / `storage/` → `net/` / `types.py`。
- 只有 `main.py`、`message_adapter/`、`push/` 可以导入 AstrBot；`service/` 及以下必须可在无 AstrBot 环境下导入与测试。
- 指令处理函数只做参数解析与委托，业务逻辑放 `WorkService`，展示逻辑放 `WorkPresenter`；指令与推送共用同一套展示代码，不重复实现。
- 源站之间互不导入；跨源站共享的纯函数只放 `core/source/utils.py`。只被一个源站使用的辅助模块放该源站子包内，模块名只描述职责（如 `api.py`、`client.py`、`page.py`）。
- 源站只通过 `types.py` 的数据契约与上层交互，不返回站点原始结构。

## 源站顺序

源站在文档、配置、工厂、导出与帮助文本中统一使用以下顺序：E-Hentai（`ehentai`）、nhentai（`nhentai`）、JMComic（`jmcomic`）、哔咔（`pica`）、Hanime1（`hanime1`）。新增源站时同步调整所有列表，步骤见 `docs/ARCHITECTURE.md` 第六节。

## 编码规范

### 命名

| 类型 | 规则 | 示例 |
|------|------|------|
| 类 | PascalCase | `WorkService`, `EHentaiSource` |
| 函数 / 方法 | snake_case | `extract_refs`, `random_work` |
| 私有 | 前导 `_` | `_fetch_fresh` |
| 常量 | UPPER_SNAKE_CASE | `SORT_PARAMS`, `API_BASE` |

### 导入顺序

```python
# 1. 标准库
import asyncio

# 2. 第三方
import aiohttp

# 3. 项目内（logger 优先并单独空一行；其余按相对层级由深到浅）
from ...logger import logger

from ...errors import SourceError
from ..utils import to_int
from .base import BaseSource
```

### 注释与 docstring

- 每个模块首行必须有单行中文 docstring。
- 类和公开方法使用 Google 风格中文 docstring；私有方法可只写单行 docstring。
- 段落分隔使用 `# ── 名称 ──────` 箱线注释。

### 异常处理

- 可预期失败抛 `CollectorError` 子类，消息写成用户能看懂的中文。
- 捕获具体异常；只有“单个目标失败不应影响整体”的边界（推送目标、提示消息、调度循环、指令兜底）允许捕获 `Exception`，并必须记录日志与注明原因。
- `asyncio.CancelledError` 必须重新抛出。
- 配置值用 `config_manager` 中的 `as_bool` / `as_int` / `as_float` / `as_choice` 转换，非法值回退默认值。

### 日志

使用 `from <相对层级>.logger import logger`，不要自建 logger。`debug` 记录内部流程，`info` 记录管理操作，`warning` 记录可恢复失败，`exception` 记录意外异常。

## 运行环境

- 插件在 AstrBot 4.x 内运行，入口类继承 `astrbot.api.star.Star` 并通过 `@register` 注册。
- 使用 AstrBot 内部 API 前，必须在 AstrBot 最新源码中确认真实定义与相同导入路径的调用。
- 运行时状态（哔咔 Token、推送记录）写入 `StarTools.get_data_dir()` 返回的插件数据目录；下载缓存写入配置的缓存目录。AstrBot 更新插件会替换插件目录，不得把运行时文件写入插件目录。

## 测试

- 测试放在根目录 `test/`（已 gitignore，不提交），使用 `unittest`，不依赖 pytest 与第三方 mock 框架。
- `test/stubs.py` 通过 `sys.modules` 注入 AstrBot stub，并把仓库根目录注册为包 `hentai_collector`。
- 运行：`python -m unittest discover -s test`。
- 源站解析器改动需配合 `test/fixtures/` 中的真实页面样本测试；涉及网络的验证只在本地手动运行，不写入单元测试。

## 文档范围

- `README.md` 面向使用者：功能、安装、指令、条件写法、注意事项。
- `docs/CONFIG.md` 与 `_conf_schema.json` 保持一一对应，修改配置项时同步更新。
- 接口、参数映射、站点改版记录放 `docs/SOURCE_MEMO.md`；模块职责与流程放 `docs/ARCHITECTURE.md`；版本变更放 `CHANGELOG.md`。

## Git 约定

- 提交信息使用中文，直接描述具体改动。
- 修改前先检查工作区状态，保留用户已有改动，只修改任务范围内的文件。
