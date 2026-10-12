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
   └── commands.py / types.py / errors.py / config_manager.py / constants.py / logger.py
```

依赖只允许自上而下：

- 只有 `main.py`、`message_adapter/`、`push/` 导入 AstrBot；`service/` 及以下不依赖 AstrBot 运行时，可在单元测试中直接调用。
- `commands.py` 只保存操作定义和配置校验，不导入 AstrBot；原生过滤器和 WebUI 设置的适配放在 `message_adapter/command_binding.py`。
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
    ├── commands.py                 # COMMAND_SPECS、CommandCatalog：操作目录与触发词校验
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
    ├── message_adapter/            # formatter.py / cover_grid.py / sender.py / presenter.py / command_binding.py
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
| `validate_query(query)` | 明确拒绝源站无法准确实现的条件组合 |
| `list_images(work, max_pages=0)` | 漫画源按原页码返回图片任务，发现前应用页数上限 |
| `fetch(work, ctx)` | 模板方法：默认 `list_images` + 共享下载器；JM（库内解码）与 Hanime1（视频）覆盖 |

`ImageTask.resolve` 支持延迟解析：E-Hentai 需先请求单页 HTML 才能拿到图片地址，下载器在每次重试时重新解析，以便换到新的图片服务器。

### 3.2 WorkInfo

列表接口返回的条目 `detailed=False`，展示卡片、推送与获取文件前由 `WorkService.ensure_detail` 补全；补全失败保留原条目，不中断流程。

### 3.3 异常

所有可预期失败都抛 `CollectorError` 子类，消息是面向用户的中文说明；`main.py::_guarded` 统一回复给用户。其他异常记录堆栈，用户收到稍后重试、仍失败时联系管理员的提示，不直接展示异常内部结构。

## 四、主流程

### 4.1 指令

插件没有默认源站。`core.commands.COMMAND_SPECS` 是指令的唯一目录，共 38 个固定操作：7 个通用操作、6 个聚合操作、5 个源站各 5 个操作。每个 `CommandSpec` 记录稳定的操作键、处理函数名称、默认触发词、业务动作、源站或媒体类型、参数说明和管理员限制。

| 类别 | 组成 | 示例 | 范围 |
|------|------|------|------|
| 单站指令 | 默认“动作 + 站点标识” | `搜eh`、`排行jm`、`随机nh`、`看anime`、`详情哔咔` | 只查该源站 |
| 聚合指令 | 默认“动作 + 媒体词” | `搜本子`、`排行本子`、`随机里番` | 该媒体类型的全部可用源站 |
| 通用指令 | 各操作独立配置 | `看本`、`本子详情`、`翻页`、`本子帮助` | 按序号 / 链接，与源站无关 |

`CommandCatalog` 将 `commands.<操作键>` 的字符串列表校验为有序触发词，第一项为首选。无效词被忽略，空项回退默认，跨操作冲突按相关操作回退并记录日志。目录、配置 schema、原生注册和用户提示共用操作键，不再分别维护指令名称。

入口根据目录注册原生 `filter.command`，处理函数使用稳定且唯一的名称，如 `cmd_search_ehentai`、`cmd_ranking_comic_all`。`CommandBindings.initialize` 绑定这些处理器与原生过滤器，先应用插件配置，再通过 AstrBot 官方指令管理入口载入已保存的 WebUI 改名、别名和禁用状态；新增自定义词与其他插件指令冲突时回退默认。帮助与 `WorkPresenter` 的操作提示实时读取绑定状态，禁用操作不再生成可执行提示。参数解析、业务服务和配置目录保持不依赖框架。

```text
AstrBot 事件 → 指令处理函数
  → _guarded：发送者是机器人自身？权限？同一消息已处理？ → stop_event()
      请求超过 3 秒发送一次等待通知；同用户、同聊天的列表请求串行
  → _site_action(动作, 源站键) / _aggregate_action(动作, 媒体类型)
  → split_command + parse_args（关键词 / 页码 / 排序 / 周期 / 数量 / 条件）
  → WorkService.search_many / ranking_many / random_works / random_many / get_work
  → _show_results：截断条数、拼接全局序号，构造候选 SearchSession（sources 可为多个）
  → WorkPresenter.send_list（单站） / send_aggregate_list、send_grouped_cards（聚合） / send_card / send_fetch
  → Delivery（event.send）
  → 列表投递成功后保存 SearchSession，失败时保留此前可见列表
```

`split_command` 支持成对引号的多词参数值并保留源站标签语法。`parse_args` 严格校验页码、数量、评分和排序；排行只接受榜单与页码，入口拒绝搜索操作中的数量参数并引导翻页。源站不支持的条件以说明随结果传递到展示层，不静默忽略用户输入。

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
- 翻页对会话中的全部源站请求同一页码，使用新会话候选对象，成功展示后再替换原页。翻页空结果、查询全部失败或列表投递失败时保留原页和序号；新查询得到空结果时先发送重搜提示，通知成功后再清除旧会话。
- 搜索会话按“聊天会话 ID + 发送者 ID”隔离，默认有效 30 分钟。同一键的列表请求串行执行，按序号操作也使用同一锁等待展示提交完成；取到所选作品后立即释放锁，后续详情加载和下载不持锁。结果提示说明归属、有效期、详情、下载以及可用的上下页。
- 聚合结果的 `has_next` 在任一源站失败或仍有下一页时保持为真；只有全部参与源站都确认无下一页时才标注最后一页，入口会话状态与展示提示使用相同规则。
- 聚合随机：“混合随机”每次由 `SourceManager.pick_random_source` 挑一个候选源站；“每站随机”对每个候选源站调用 `random_works`，以 `send_grouped_cards` 分组发送。候选为“随机候选源站”中对应媒体类型的可用源站，没有时回退该类型全部可用源站。
- 随机结果保留请求数量、实际数量以及部分失败或去重导致不足的说明。随机卡片不保存搜索会话；获取提示优先使用当前可用的站点指令与完整作品 ID，站点获取停用时回退到通用获取指令加链接或明确的源站与 ID，全部停用时不生成获取指令。
- 封面按源站分别限流下载，聚合列表中各站并行，整批总等待预算为 10 秒；超时取消并等待请求收尾，保留已经取得的封面。
- 封面在展示前完整解码，透明部分按白底合成；各颜色通道的像素范围均不超过 2 时视为空白。拼图跳过缺失、损坏和空白图片，按原作品序号紧凑排列；全部无效时只保留文字列表。判空在缩放与模糊前执行，单张卡片共用该检查。
- 拼图逐张解码、判空、缩小后释放原尺寸像素，再解码下一张，避免同时持有整批大图。
- 单站与聚合列表共用文本分块与消息组装逻辑，普通消息优先按作品段落分片；封面拒发仍继续正文，关键正文失败向上传播。

### 4.3 自动解析

`auto_parse` 监听所有消息：使用 `CommandBindings.is_command` 识别当前触发词，包含被禁用的指令，避免自动解析绕过禁用；否则 `SourceManager.extract_refs` 按源站顺序提取引用（单条消息最多 3 个），逐个发送信息卡片，按“解析链接后的动作”决定是否继续获取文件。每条链接独立处理，已解析作品先加入获取列表，某张卡片拒发不阻断后续链接或这些作品的文件获取。指令与自动解析可能同时命中同一条消息，`_claim` 以 `会话ID:消息ID` 去重。解析卡片不建立列表序号，获取文件应复制卡片中的获取指令或使用原链接。

### 4.4 获取作品（下载 → 打包 → 缓存）

`看本` 与站点下载指令在加载作品后直接进入获取流程，详情指令调用 `send_card`。`send_fetch` 的详细进度默认关闭；入口的单次等待通知、视频链接、缺页与页数限制提醒不受详细进度开关影响。随机、解析等批量获取按整次请求检查一次冷却，逐作品处理失败，最终汇总失败原因与重试指令；取消必须继续向上传播。

下载缺页与配置截断分别记录为 `missing`、`truncated`。`missing > 0` 的文件本次可以发送，但下次获取跳过该缓存并重建；`truncated > 0` 且没有下载缺页的文件可复用，提高 `max_pages` 后参数指纹改变，旧缓存自动失效。

```text
WorkService.fetch(work)
  ├─ 视频仅链接 → 同作品锁与并发槽内重新解析，不检查缓存目录或读取视频缓存
  ├─ 缓存目录不可用 → SourceUnavailable
  ├─ 同一作品加锁（并发请求排队）
  ├─ JobStore.find_outputs：保留期内、参数指纹一致且无下载缺页 → 返回缓存（from_cache）
  └─ 全局信号量（同时处理作品数）
       → JobStore.open_job（写归属标记与目录清理期限，本次写入使用独立子目录）
       → source.fetch(work, FetchContext)
            ├─ 漫画：list_images → ImageDownloader（候选地址轮换、重试、文件头校验）
            ├─ JM：jmcomic.download_album（下载器预先限制章节与图片，库内多线程与图片还原）
            └─ Hanime1：选清晰度 → VideoDownloader（大小上限）或仅返回链接
       → validate_images（Pillow 校验并完整解码，损坏图片计入 missing）
       → Packager：流式 PDF（单页编码，按页数分片）/ ZIP（STORED）
       → JobStore.save_outputs（manifest：文件、指纹、过期时间）
       → 删除中间原图目录
```

文件名为 `标题 [源站前缀ID]`，标题部分截断而保留 ID 后缀。

单页 `SourceError` / `DownloadError` 在重试耗尽后计为缺页；封禁与额度耗尽立即中止。全部下载子任务退出后才返回或抛出异常。curl 下载通过 `content_callback` 分块写盘，请求协程直接持有传输，响应头到达前或正文下载时取消均会移除本请求句柄，不关闭共享会话或其他并发请求。大小超限与写盘失败在回调中中止，异常在请求协程传播，所有退出路径清理临时文件。打包与 JM 同步工作通过 `run_sync` 执行，取消时等待实际线程结束后再删除输入、释放作品锁和并发槽。视频链接使用关键文本发送，平台拒发会作为交付失败反馈给调用方。

### 4.5 自动推送

```text
PushManager.start → 每个任务一个 Scheduler（next_daily / next_interval）
  → run_task（同任务锁覆盖选取、投递和历史更新）
       ├─ 排行榜：ranking(period, 1)
       ├─ 条件作品：search(query)
       └─ 随机：random_work × N（本轮去重）
       → 按每个目标历史过滤（only_new）并独立应用数量名额 → 合并引用补 ensure_detail
       → 对每个目标会话：Delivery.for_session → send_cards（合并转发）→ 可选 send_fetch(quiet)
       → 成功目标立即记录作品，失败目标单独保留重试资格
```

推送目标：群号 / 用户 ID 与“推送平台 ID”拼接为 `平台ID:GroupMessage:群号` / `平台ID:FriendMessage:用户ID`，也可直接填写会话 ID。

随机选择全部请求失败时返回“获取作品失败”，与“没有新作品”和源站空结果区分；数量不足原因同时进入推送标题和管理员结果。单部文件的下载异常或平台拒发在作品边界处理，继续其他作品，接收者收到一次失败汇总。`WorkPresenter.retry_hint` 统一生成重新获取指令，获取操作全部禁用时生成联系管理员的说明。管理员结果分别统计卡片成功会话数、文件成功、文件失败以及因卡片失败未发送的次数；`FetchResult.missing > 0` 计入文件失败，不写入完成历史，保留后续重新入选资格。

`PushTask` 解析与任务说明共用忽略原因，`describe` 展示可执行编号和被忽略配置的原位置。同类型同名配置拒绝后出现项，日内间隔只接受 1–1440 分钟。历史以 `tasks[task_id][umo]` 保存完成引用，每个目标独立去重、选取和应用数量名额，失败目标的旧作品不占用正常目标的新作品名额；随机选取在有限重试内填充各目标名额。无文件任务收到卡片即完成，有文件任务还要求文件或链接成功且 `missing == 0`，配置截断不影响完成。旧任务级记录只证明至少一个目标成功，完整旧字典一次性归档在 `migrated_records` 中用于追溯，不为任何目标推断完成状态；未知完成状态允许升级后重新推送，之后以真实目标级成功去重。

## 五、缓存与清理

- 缓存根目录下 `jobs/{源站}-{作品ID}/`，含归属标记 `.astrbot_hentai_collector` 与 `manifest.json`。
- `expires_at` 是产物有效期，`cleanup_at` 是目录清理期限；进行中目录保留 6 小时，失败后至少保留 5 分钟且不早于已有产物有效期。失败重取不改变旧产物有效期，仅链接不创建任务目录。
- `initialize` 启动的清理循环每 30 分钟删除过期目录；无 manifest 的遗留目录超过 72 小时删除。
- 管理员 `本子清理缓存` 强制删除全部带标记的目录。
- 搜索会话只保存在内存，按本人当前聊天隔离，默认 30 分钟过期；插件重载后失效。

## 六、扩展：新增源站

1. 在 `core/source/platform/` 下新建 `<源站>.py`（需要多个模块时建立子包，入口为 `source.py`，辅助模块按职责命名）。
2. 继承 `BaseSource`，声明类属性并实现 `work_url`、`get_work`，按能力实现 `search` / `ranking` / `random_work` / `list_images` 或覆盖 `fetch`。
3. 在 `core/source/platform/__init__.py` 与 `core/source/__init__.py` 导出。
4. 在 `config_manager.py` 的 `SOURCE_KEYS`、`SOURCE_DISPLAY_NAMES`、默认开关表、`_create_source` 中登记，并添加配置 dataclass。
5. 在 `_conf_schema.json` 的 `sources`、`random.pool`、推送模板 `source` 选项与源站配置节点中同步添加。
6. 在 `core/commands.py` 的统一目录中登记源站标识与五个操作，并同步 `_conf_schema.json` 的 `commands` 项、默认触发词和配置说明；入口按固定操作定义注册处理器，帮助与卡片从实时绑定读取名称。新源站的媒体类型决定它参与 `本子` 还是 `里番` 聚合指令。
7. 更新 README 支持表与单站指令表、[源站接口备忘](SOURCE_MEMO.md) 与测试。
