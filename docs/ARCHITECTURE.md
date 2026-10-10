# 架构文档

本文件按当前实现描述插件的分层、模块职责与主流程。使用方式见 [README](../README.md)，配置项见 [配置说明](CONFIG.md)，源站接口细节见 [源站接口备忘](SOURCE_MEMO.md)。

## 一、分层与依赖方向

```text
main.py（AstrBot 入口：指令注册、权限、去重、异常兜底）
   │
   ├── message_adapter/   展示层：WorkPresenter 渲染结果，Delivery 统一被动回复与主动推送
   ├── push/              推送层：任务解析、调度循环、去重
   │        │
   │        ▼
   ├── service/           业务层：WorkService 编排查询与“下载→打包→缓存”，参数解析
   │        │
   │        ▼
   ├── source/            源站层：SourceManager 注册表 + 各源站实现（BaseSource）
   ├── downloader/        下载层：图片并发下载、视频流式下载
   ├── packager/          打包层：流式 PDF、ZIP、文件命名
   ├── storage/           存储层：任务目录缓存、搜索会话、推送记录
   ├── net/               网络层：HttpClient（aiohttp / curl_cffi 双后端）
   └── types.py / errors.py / config_manager.py / constants.py / logger.py
```

依赖只允许自上而下：

- 只有 `main.py`、`message_adapter/`、`push/` 导入 AstrBot；`service/` 及以下不依赖 AstrBot 运行时，可在单元测试中直接调用。
- 源站之间互不导入；跨源站共享的纯函数只放在 `core/source/utils.py`。
- 源站只通过 `types.py` 中的数据契约与上层交互（`WorkInfo`、`SearchQuery`、`WorkPage`、`ImageTask`、`MediaBundle`）。

## 二、目录结构

```text
astrbot_plugin_hentai_collector/
├── main.py                         # HentaiCollectorPlugin(Star)
├── _conf_schema.json               # WebUI 配置 schema
├── metadata.yaml / requirements.txt
├── docs/                           # 文档
└── core/
    ├── constants.py                # 全局常量
    ├── logger.py                   # 全局 logger
    ├── errors.py                   # CollectorError 异常族（消息直接面向用户）
    ├── types.py                    # 数据契约
    ├── config_manager.py           # 配置 dataclass、类型兜底、源站工厂
    ├── net/http_client.py          # HttpClient：代理、Cookie、重试、流式下载
    ├── source/
    │   ├── manager.py              # SourceManager：别名、链接路由、引用解析、随机源
    │   ├── utils.py                # 源站共享纯函数、RateLimiter
    │   └── platform/
    │       ├── base.py             # BaseSource、FetchContext
    │       ├── ehentai/            # api.py（访问层）+ source.py
    │       ├── nhentai.py
    │       ├── jmcomic.py
    │       ├── pica/               # client.py（签名与登录）+ source.py
    │       └── hanime1/            # page.py（HTML 解析）+ source.py
    ├── downloader/                 # image.py / video.py
    ├── packager/                   # pdf.py / zip.py / naming.py / manager.py
    ├── storage/                    # job_store.py / search_session.py / push_history.py
    ├── service/                    # command_args.py / work_service.py
    ├── message_adapter/            # formatter.py / cover_grid.py / sender.py / presenter.py
    └── push/                       # schedule.py / manager.py
```

## 三、核心契约

### 3.1 BaseSource

| 成员 | 说明 |
|------|------|
| `key` / `name` / `short` / `aliases` | 源站键、显示名、文件名前缀、指令别名 |
| `media_type` | `COMIC` 或 `VIDEO` |
| `link_patterns` / `id_pattern` | 自动解析用的链接正则、显式 ID 校验 |
| `ranking_periods` / `sort_options` | 支持的排行周期与排序 |
| `unavailable_reason` | 非空表示当前不可用（缺账号 / 缺依赖） |
| `get_work(id)` | 必须实现，返回 `detailed=True` 的完整信息 |
| `search(query)` / `ranking(period, page)` | 可选，未实现抛 `UnsupportedOperation` |
| `random_work(query)` | 默认实现：搜索得总页数 → 随机页 → 随机条目；有专用接口的源站覆盖 |
| `list_images(work)` | 漫画源返回 `ImageTask` 列表 |
| `fetch(work, ctx)` | 模板方法：默认 `list_images` + 共享下载器；JM（库内解码）与 Hanime1（视频）覆盖 |

`ImageTask.resolve` 支持延迟解析：E-Hentai 需先请求单页 HTML 才能拿到图片地址，下载器在每次重试时重新解析，以便换到新的图片服务器。

### 3.2 WorkInfo

列表接口返回的条目 `detailed=False`，展示卡片、推送与获取文件前由 `WorkService.ensure_detail` 补全；补全失败保留原条目，不中断流程。

### 3.3 异常

所有可预期失败都抛 `CollectorError` 子类，消息是面向用户的中文说明；`main.py::_guarded` 统一回复给用户。其他异常记录堆栈并回复“处理失败，请查看日志”。

## 四、主流程

### 4.1 指令

```text
AstrBot 事件 → main.py 指令处理函数
  → _guarded：发送者是机器人自身？权限？同一消息已处理？ → stop_event()
  → split_command + parse_args（源站 / 关键词 / 页码 / 排序 / 周期 / 条件）
  → WorkService.search / ranking / random_work / get_work
  → SearchSessionStore 保存列表（序号 → 作品）
  → WorkPresenter.send_list / send_card / send_cards / send_fetch
  → Delivery（event.send）
```

### 4.2 自动解析

`auto_parse` 监听所有消息：首个词是本插件指令时跳过；否则 `SourceManager.extract_refs` 按源站顺序提取引用（单条消息最多 3 个），逐个发送信息卡片，按“解析链接后的动作”决定是否继续获取文件。指令与自动解析可能同时命中同一条消息，`_claim` 以 `会话ID:消息ID` 去重。

### 4.3 获取作品（下载 → 打包 → 缓存）

```text
WorkService.fetch(work)
  ├─ 缓存目录不可用 → SourceUnavailable
  ├─ 同一作品加锁（并发请求排队）
  ├─ JobStore.find_outputs：保留期内且打包参数指纹一致 → 直接返回（from_cache）
  └─ 全局信号量（同时处理作品数）
       → JobStore.open_job（写归属标记与进行中过期时间）
       → source.fetch(work, FetchContext)
            ├─ 漫画：list_images → ImageDownloader（候选地址轮换、重试、文件头校验）
            ├─ JM：jmcomic.download_album（库内多线程与图片还原）
            └─ Hanime1：选清晰度 → VideoDownloader（大小上限）或仅返回链接
       → validate_images（Pillow 校验，损坏图片计入 missing）
       → Packager：流式 PDF（单页编码，按页数分片）/ ZIP（STORED）
       → JobStore.save_outputs（manifest：文件、指纹、过期时间）
       → 删除中间原图目录
```

文件名为 `标题 [源站前缀ID]`，标题部分截断而保留 ID 后缀。

### 4.4 自动推送

```text
PushManager.start → 每个任务一个 Scheduler（next_daily / next_interval）
  → run_task
       ├─ 排行榜：ranking(period, 1)
       ├─ 条件作品：search(query)
       └─ 随机：random_work × N（本轮去重）
       → PushHistory 过滤已推送（only_new）→ ensure_detail
       → 对每个目标会话：Delivery.for_session → send_cards（合并转发）→ 可选 send_fetch(quiet)
       → 至少一个会话成功才写入推送记录
```

推送目标：群号 / 用户 ID 与“推送平台 ID”拼接为 `平台ID:GroupMessage:群号` / `平台ID:FriendMessage:用户ID`，也可直接填写会话 ID。

## 五、缓存与清理

- 缓存根目录下 `jobs/{源站}-{作品ID}/`，含归属标记 `.astrbot_hentai_collector` 与 `manifest.json`。
- 进行中的任务写入 6 小时过期时间，防止周期清理误删；失败或仅链接的任务约 5 分钟后过期。
- `initialize` 启动的清理循环每 30 分钟删除过期目录；无 manifest 的遗留目录超过 72 小时删除。
- 管理员 `本子清理缓存` 强制删除全部带标记的目录。
- 搜索会话只保存在内存，插件重载后失效。

## 六、扩展：新增源站

1. 在 `core/source/platform/` 下新建 `<源站>.py`（需要多个模块时建立子包，入口为 `source.py`，辅助模块按职责命名）。
2. 继承 `BaseSource`，声明类属性并实现 `work_url`、`get_work`，按能力实现 `search` / `ranking` / `random_work` / `list_images` 或覆盖 `fetch`。
3. 在 `core/source/platform/__init__.py` 与 `core/source/__init__.py` 导出。
4. 在 `config_manager.py` 的 `SOURCE_KEYS`、`SOURCE_DISPLAY_NAMES`、默认开关表、`_create_source` 中登记，并添加配置 dataclass。
5. 在 `_conf_schema.json` 的 `sources`、`random.pool`、推送模板 `source` 选项与源站配置节点中同步添加。
6. 更新 README 支持表、[源站接口备忘](SOURCE_MEMO.md) 与测试。
