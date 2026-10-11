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
    │   ├── manager.py              # SourceManager：别名、链接路由、引用解析、聚合候选、随机源
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

插件没有默认源站，指令分三类，由 `main.py` 顶部的指令表生成：

| 类别 | 组成 | 示例 | 范围 |
|------|------|------|------|
| 单站指令 | 动作 + 站点标识（`SITE_TAGS`） | `搜eh`、`排行jm`、`随机nh`、`看anime`、`详情哔咔` | 只查该源站 |
| 聚合指令 | 动作 + 媒体词（`MEDIA_WORDS`） | `搜本子`、`排行本子`、`随机里番` | 该媒体类型的全部可用源站 |
| 通用指令 | 固定名称 | `看本`、`本子详情`、`翻页`、`本子帮助` | 按序号 / 链接，与源站无关 |

单站与聚合指令的处理函数由 `_register_generated_commands` 在模块导入时生成：AstrBot 按“模块名_函数名”登记处理函数、加载插件时再绑定实例，所以每个生成函数都设置了唯一的 `__name__`（如 `cmd_search_ehentai`、`cmd_ranking_comic_all`），`__doc__` 显示在 WebUI 指令管理中。参数中不再识别源站名，源站只由指令决定。

```text
AstrBot 事件 → 指令处理函数
  → _guarded：发送者是机器人自身？权限？同一消息已处理？ → stop_event()
  → _site_action(动作, 源站键) / _aggregate_action(动作, 媒体类型)
  → split_command + parse_args（关键词 / 页码 / 排序 / 周期 / 数量 / 条件）
  → WorkService.search_many / ranking_many / random_works / random_many / get_work
  → _show_results：截断条数、拼接全局序号、SearchSessionStore 保存（sources 可为多个）
  → WorkPresenter.send_list（单站） / send_aggregate_list、send_grouped_cards（聚合） / send_card / send_fetch
  → Delivery（event.send）
```

### 4.2 聚合

```text
WorkService._gather(keys, call)
  → 各源站并发执行，单站超时 AGGREGATE_TIMEOUT（90 秒）
  → 每站得到 SiteResult(source, page | error)；失败只记录在该站，不中断其他站
_show_results
  → 每站截取“聚合列表每站最多显示条数”条，按源站顺序拼接，序号全局连续
  → 全部为空时回复各站失败原因；只有一个源站时按单站列表展示
WorkPresenter.send_aggregate_list
  → 总览（各站序号范围 / 无结果 / 失败原因 + 操作提示）
  → 每站一组：标题 + 从该站起始序号编号的封面拼图 + 每部作品独立的摘要节点
  → Delivery.forward_sections：嵌套合并转发（外层每个节点内是该站的内层节点）；
    未开启嵌套或协议端拒绝时展开为单层合并转发
```

- 聚合排行写了榜单时只请求支持该榜单的源站，并在标题中注明跳过的站；不写榜单时各站使用 `default_period`（周榜，没有周榜用第一个榜单）。
- 翻页对会话中的全部源站请求同一页码。
- 聚合随机：“混合随机”每次由 `SourceManager.pick_random_source` 挑一个候选源站；“每站随机”对每个候选源站调用 `random_works`，以 `send_grouped_cards` 分组发送。候选为“随机候选源站”中对应媒体类型的可用源站，没有时回退该类型全部可用源站。
- 封面按源站分别限流下载，聚合列表中各站并行。
- 单站与聚合列表共用文本分块与消息组装逻辑，普通消息将作品块以空行连接；序号与封面拼图一致。

### 4.3 自动解析

`auto_parse` 监听所有消息：首个词是本插件指令时跳过；否则 `SourceManager.extract_refs` 按源站顺序提取引用（单条消息最多 3 个），逐个发送信息卡片，按“解析链接后的动作”决定是否继续获取文件。指令与自动解析可能同时命中同一条消息，`_claim` 以 `会话ID:消息ID` 去重。

### 4.4 获取作品（下载 → 打包 → 缓存）

`看本` 与站点下载指令在加载作品后直接进入获取流程，详情指令调用 `send_card`。`send_fetch` 的进度提示默认关闭，视频链接、缺页与页数限制提醒不受进度开关影响；不完整结果的页数随获取结果和缓存记录保存。

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

### 4.5 自动推送

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
6. 在 `main.py` 的 `SITE_TAGS` 中登记站点标识，单站指令（搜 / 排行 / 随机 / 看 / 详情 + 标识）会自动生成；新源站的媒体类型决定它参与 `本子` 还是 `里番` 聚合指令。
7. 更新 README 支持表与单站指令表、[源站接口备忘](SOURCE_MEMO.md) 与测试。
