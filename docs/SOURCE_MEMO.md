# 源站接口备忘

记录各源站当前使用的接口、参数映射与已知限制，供维护时对照。站点改版后优先更新本文件与对应解析器。

## E-Hentai / ExHentai（`core/source/platform/ehentai/`）

- **元数据**：`POST https://api.e-hentai.org/api.php`（里站 `https://exhentai.org/api.php`），`{"method":"gdata","gidlist":[[gid,token]],"namespace":1}`，每批最多 25 个。搜索、排行、随机都只从 HTML 中提取 `(gid, token)`，字段统一由 gdata 填充，避免依赖列表页布局。
- **搜索**：首页 `/?f_search=...`；`f_cats` 是“排除”位掩码；`advsearch=1&f_sr=on&f_srdd=N` 最低评分；`f_spf=N` 最少页数。
- **翻页**：列表只能用游标 `next=<gid>`，没有页码。源站内部按“查询参数 + 页码”缓存每页的 next 游标，跳页时从已知最大页逐页向后走，一次最多 20 页。总页数由 “Found about N results” 估算（每页 25）。
- **排行榜**：`https://e-hentai.org/toplist.php?tl=15|13|12|11&p=页码-1`，分别为昨日 / 近一月 / 近一年 / 总榜，站点没有周榜。
- **随机**：取最新页与 `prev=1` 最旧页的 gid 范围，随机游标跳转后随机挑选；gid 随时间递增，属于按时间近似均匀的抽样。
- **下载**：画廊页 `?p=N` 提取 `/s/{token}/{gid}-{页码}` 单页地址 → 单页 `<img id="img" src>`；`509.gif` 表示额度耗尽（抛 `BlockedError` 并中止整本）。
- **换节点**：单页中 `nl('xxxx-yyyy')` 是“Reload broken image”参数，带 `?nl=xxxx-yyyy` 重新请求单页会分配另一个 H@H 节点。图片下载失败后重新解析时带上上次的参数，避免重试反复落到同一个故障节点。
- **后端**：使用 curl_cffi。H@H 节点（`*.hath.network`）的大图经 aiohttp 下载时经常在接近结尾处断开（`ContentLengthError` + `SSLEOFError`），直连与走代理都会出现；同一批地址用 curl_cffi 下载全部完整（2026-10 在 NAS 实测）。
- **速度**：每张图都要先请求一次单页，单页请求受 `request_interval` 串行限速，因此整本耗时约为 `页数 × request_interval`；图片下载本身并行，通常不是瓶颈。实测 435 页（770 MB）约 219 秒，0 失败。
- Cookie `nw=1` 跳过内容警告页；页面含 “temporarily banned” 或里站返回空页面时抛 `BlockedError`。请求经 `RateLimiter` 限速。
- 认证 Cookie 仅限所选站点的 HTTPS 精确主机；H@H、其他子域与跨域重定向不接收凭据，fake-ip 不影响此规则。发现图片链接时先应用页数上限，目标页码缺失或不连续会明确抛出解析失败。

## nhentai（`core/source/platform/nhentai.py`）

- 使用官方 v2 匿名接口（旧版 `/api/galleries/search` 已返回 403 “Use new API”）：
  - `GET /api/v2/galleries/{id}`：详情（`title`、`tags[type,name]`、`pages[path]`、`cover`、`num_favorites`、`upload_date`）
  - `GET /api/v2/search?query=&sort=date|popular|popular-today|popular-week|popular-month&page=`：每页 25，`total` 上限 500
  - `GET /api/v2/galleries/random`：返回 `{"id": N}`
  - `GET /api/v2/cdn`：图片 / 缩略图服务器列表
- `query` 不能为空，无条件时使用 `*`；语言过滤追加 `language:xxx`，最少页数追加 `pages:>=N`。
- 图片地址为 `{image_server}/{page.path}`，多个服务器按页轮换作为候选。
- 使用 curl_cffi 后端模拟浏览器指纹（站点有 Cloudflare）。

## JMComic（`core/source/platform/jmcomic.py`）

- 通过 `jmcomic` 库的移动端 API 客户端（`impl: api`），所有同步调用在线程中执行。
- 详情：`get_album_detail`；关键词搜索：`search_site(query, page, order_by)`，每页 80；分类查询：`categories_filter(page, time='a', category, order_by)`。移动端 `search_site` 丢弃分类参数，关键词与分类组合明确拒绝；分类列表的排除词在本页标题、作者和标签上过滤。排行：`day_ranking` / `week_ranking` / `month_ranking`，总榜为 `categories_filter(time='a', order_by='mv')`。
- 排序映射：最新 `mr`、观看 `mv`、点赞 `tf`；分类映射见源码 `CATEGORY_PARAMS`。
- 随机（无条件）：`categories_filter` 取总页数后随机翻页（最多前 200 页）。
- 下载：`jmcomic.download_album` 自带多线程下载与切割图片还原，下载完成后按自然序收集图片；有页数上限时使用本次下载器的 `do_filter` 扩展点，顺序发现所需章节并限制实际图片任务，不修改库的全局下载器。取消同步调用时等待真实线程和库内线程收尾再释放任务资源。
- 封面：`JmcomicText.get_album_cover_url`。

## 哔咔（`core/source/platform/pica/`）

- App 接口 `https://picaapi.picacomic.com/`，每个请求带 HMAC-SHA256 签名（路径 + 时间 + nonce + 方法 + api-key，转小写）；请求头需与 App 一致，否则登录会误报 1023。
- 登录 `POST auth/sign-in` 换 Token（约 7 天），401 时自动重新登录一次；登录失败冷却 10 分钟。Token 与账号密码哈希绑定保存。
- 详情 `comics/{id}`；搜索 `POST comics/advanced-search?page=N`（`keyword`、`sort`、`categories`），只有分类时走 `comics?page=N&c=分类&s=排序`；排序：最新 `dd`、点赞 `ld`、观看 `vd`。
- 两个查询分支统一按本页标题和标签应用排除词；图片发现达到页数上限后停止请求后续分页与章节。
- 排行 `comics/leaderboard?tt=H24|D7|D30&ct=VC`；随机 `comics/random`。
- 图片：按话 `comics/{id}/order/{第几话}/pages?page=N` 分页获取，地址为 `{fileServer}/static/{path}`。
- 签名按原样路径计算，请求使用 `raw_url` 禁止重新编码。

## Hanime1（`core/source/platform/hanime1/`）

- 搜索：`/search?query=&genre=&sort=&page=`；`genre` 取值 全部（不传）/ 裏番 / 泡麵番 / Motion Anime / 3DCG / 2.5D / 2D動畫 / AI生成 / MMD / Cosplay；`sort` 取值 最新上市 / 最新上傳 / 本日排行 / 本週排行 / 本月排行 / 觀看次數 / 讚好比例 / 時長最長。
- 排行映射：日 `本日排行`、周 `本週排行`、月 `本月排行`、总 `觀看次數`。
- 卡片解析：按 `watch?v=ID` 链接切块，在块内找封面 `<img src>` 与标题（`home-rows-videos-title` / `card-mobile-title`），广告卡片没有 watch 链接自然跳过；最大页码取分页链接中的 `page=`（注意 HTML 中是 `&amp;page=`）。
- 播放页：标题 `#shareBtn-title`（回退 `og:title`）、作者 `#video-artist-name`、“觀看次數：X萬次 日期”、简介 `.video-caption-text`、标签 `.single-video-tag`、视频源 `<source src size>`。
- 获取内容时重新请求播放页详情，不使用搜索会话保存的旧视频地址；下载页 `/download?v=ID` 补充更多清晰度（文件名形如 `-1080p.mp4`），同清晰度使用本次下载页的新地址。未登录通常只有 480P。
- 视频直链带 `secure` 时效参数，“仅链接”模式发送的地址会过期。
- 使用 curl_cffi 后端（站点有 Cloudflare）。
- 配置 Cookie 仅发送到 `hanime1.me` 的 HTTPS 精确主机；仅链接模式每次重新解析，不复用先前下载的视频，也不依赖缓存目录。
