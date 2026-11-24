# 省局站点适配记录

省级药监局网站近几年普遍换成政务门户前端框架，**栏目列表不在页面源码里**。接一个省之前先按下面顺序分类，不要一上来就写解析器。

## 零、先过「完整度」这一关（最大的坑）

接任何栏目，顺序永远是：**枚举栏目树 → 找到列表 → 摸清翻页 → 抓全 → 才谈解析和过滤**。

翻页链接是 JS 生成时源码里根本没有「下一页」，`maxpage_links()` 只返回 1，程序安静地只抓第一页。**逐栏对账、反向枚举、页面数字全都能绿，只有“最早日期”能揭穿。**

自检命令（每次接完或改完栏目都跑）：

```python
import json, collections
rows = [json.loads(l) for l in open("data/cn_regs.jsonl", encoding="utf-8")]
by = collections.defaultdict(list)
for r in rows: by[r["col"]].append(r)
for c, rs in sorted(by.items()):
    ds = sorted(x["date"] for x in rs if x.get("date"))
    print(f"{c:<16} {len(rs):>5} 条  最早 {ds[0] if ds else '-'}  最晚 {ds[-1] if ds else '-'}")
```

判据：**最早日期还在近一两年内，而站点明显有十年历史 → 只抓了第一页。**

实测这套判定抓出的问题：

| 栏目 | 修复前 | 最早日期暴露的真相 | 修复后 |
|---|---|---|---|
| 安徽 公示公告 | 7 条 | 官方 `total:5357 / pageCount:358` | 483 条器械（全量 358 页）|
| 河南 公告通告 | 20 条 | 14 页，只抓了第 1 页 | 255 条 |
| 江苏 5 栏 | 320 条 | 最早 2000 年 ✓ 本来就到底了 | — |
| 北京 政策文件 | 32 条 | 最早 2016 年 ✓ | — |
| 广东 工作文件 | 54 条 | 最早 2020 年 ✓ | — |
| 上海 规范性文件 | 3 条 | 该栏确实只有 1 页 ✓，**但整站器械文件在别的栏目** | 234 条 |
| 浙江 工作文件 | 1 条 | jpaas 接口只返回最新 10 条，**取不到历史** | 13 条（另 5 栏合计） |

## 零之二、栏目名不可信，要枚举栏目树

**凭栏目名挑栏目是本技能吃过最大的亏**：名字最像“法规”的栏目往往只有一页，真正的文件堆在名字看不出来的栏里。

| 站点 | 名字最像的栏目 | 实际 |
|---|---|---|
| 安徽 | 「公示公告」 | 5,357 条 / 358 页 / 器械 483 —— 名字完全看不出器械 |
| 上海 | 「规范性文件」20 条 | 真货在「其他公文」**103 页**（器械 397）+「其他通知公告」11 页 |
| 浙江 | 「法规文件」98 条 / 器械 11 | 真货在「规范性文件」（`col1229136205`）和「通知公告」（`col1228989285`） |

枚举方法（半小时内出全景，比逐个试栏目名快）：

```python
# 1. 从首页源码抽所有栏目 ID
cols = sorted(set(re.findall(r"col(\d{6,})", home)))
# 2. 逐栏取标题 + 文章数 + 首条标题
for cid in cols:
    h = get(f"https://<站点>/col/col{cid}/index.html")
    title = re.search(r"<title>(.*?)</title>", h).group(1)
    arts = re.findall(r'<a[^>]+href="([^"]*art[^"]*)"[^>]*>(.*?)</a>', h, re.S)
    # 记一行：cid / 标题 / 文章数 / 首条
```

读表判据：
- 文章数只有 1～2 且首条是「举报热线」「个人登录」→ 这个栏是 JS 加载，要另找接口（或它的 `queryData`）
- 文章数 20 上下且首条日期很新 → 大概率还有历史页，去找 `index_N.html` 或 `?pageIndex=`
- 首条标题就能看出装的是什么（药品 / 器械 / 人事 / 财政）

## 一、先分类（决定用哪套取数）

| 看到什么 | 属于哪类 | 怎么办 |
|---|---|---|
| `<li><a title="完整标题">` + 日期节点 → 静态 HTML | 静态 | `parse_li` 或写专用解析 |
| 源码里有 `dataproxy.jsp` 和 `var param_xxx = {columnid, unitid, webid}` | 大汉 jpage | 走接口，按实际返回条数步进翻页 |
| 源码里有 `api-gateway/jpaas-publish-server/front/page/build/unit` 或 `<script queryData="{...}">` | 政务云 jpaas | 调接口，**返回 JSON 里嵌 HTML**，先 `json.loads` 取 `data.html` 再解析 |
| 源码里有 `Ls.pagination(...)` / `total:` / `pageCount:` | JS 分页（安徽型） | 从这段配置里读真实总数和翻页 URL 模板 |
| 源码里有 `totalPage: N`（上海型） | 前端分页 JS 变量 | 那就是真实页数，按 `index_N.html` 翻 |
| 源码里只有 `{{title}}` / `{{channel.showName}}` | 纯前端框架 | 先找 XHR，别再试静态 |
| 首页一个栏目链接都找不到 | 门户/子站 | 换栏目入口（站内导航、信息公开目录），别硬啃首页 |

**找 XHR / 找翻页最快的办法**：浏览器里渲染完执行
`performance.getEntriesByType('resource').map(r=>r.name).filter(n=>/json|api|list|data|query/i.test(n))`
——里面常能直接看到 `.../build/unit?...&pageId=<栏目ID>` 这种取数地址。
另一个同源技巧：`document.querySelector('script[queryData]')?.getAttribute('queryData')` 直接拿到 `tplSetId` / `tagId` / `pageId` 三件套。

## 二、已接通的省（含各站的翻页姿势）

| 省 | key | 栏目 URL | 取数 | 分页要点 |
|---|---|---|---|---|
| 广东 | `gd_` | `https://mpa.gd.gov.cn/zwgk/gzwj/index.html` | urllib（**必须 https**，http 建连后无响应） | `index_N.html` 正常递增 |
| 江苏 | `js_` | `https://da.jiangsu.gov.cn/col/col{cid}/index.html` | 大汉 jpage `dataproxy.jsp` | 一次返约 60 条，**按实际返回条数步进**；`totalRecord` 到 1000 就封顶，不能当总数 |
| 河南 | `hn_` | `https://yjj.henan.gov.cn/ylqx/fgwj/`、`/ylqx/ggtg/` | **urllib**（scrapling 拉回来是空，别用） | `index_N.html`，**`index_1` = 第 2 页**；源码里没有“下一页”，必须栏目配置 `pages=14`；`page_url()` 要能处理斜杠结尾 URL |
| 北京 | `bj_` | `https://yjj.beijing.gov.cn/yjj/zwgk20/zcwj91/index.html` | urllib（easysite 模板） | `parse_beijing`：`li.easysite-article-li`，**完整标题在 a 的 ` title=` 属性里**；分页 `<6位十六进制>-N.html` |
| 安徽 | `ah_` | `https://mpa.ah.gov.cn/zwgz/gsgg/index.html` | urllib | 列表 `li.even/li.odd` + `<span class="right date">`；分页 `/content/column/<栏目ID>?pageIndex=N`，每页 15 条，共 358 页。列表解析和翻页是**两套不同 URL**，别混 |
| 上海 | `sh_` | `/gfxwj/`（1 页）、`/qttzgg/`（11 页）、`/qtgzwj/`（**103 页**） | urllib | 三栏结构相同：`li.list-ban` + `<small class="listTime">2026.11.20</small>`；页数从页面 JS `totalPage: N` 读，翻 `index_N.html`（`index_1` 起） |
| 浙江 | `zj_` / `zj2_` / `zj3_` | `col1229136205`（规范性文件）、`col1228989285`（通知公告）、`col1229215895`（法规文件）、`col1229893251`（产品召回） | jpaas 接口为主，`col1229215895` 是服务端渲染 | 见下方参数表；**`pageNum` 参数无效**，这些栏目就是当前一页 |

### 浙江 jpaas 接口（可直接复用）

```
https://mpa.zj.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit
  ?parseType=bulidstatic&webId=3396&tplSetId=PpFTeEhA5DVkxKPoRDIzx
  &pageType=column&tagId=<URL编码：列表通用list>&editType=null&pageId=<栏目ID>
```

返回 `{"success":true,"data":{"html":"<ul>…<li><a href title>…</a><em>2026-08-04</em></li>…"}}`。

四个坑：
1. **必须先 `json.loads` 再取 `data.html`**。直接对 JSON 字符串正则 `<li>` 会因转义引号（`\"`）匹配不上，解析结果是 0 条，而且看起来像“站点没数据”。
2. **`tagId` 每个栏目不一样**，从栏目页的 `<script queryData="{...}">` 里抄，不要沿用上一个栏目的。实测：`列表通用list`（工作文件/通知公告/产品召回）、`规章列表`（上级文件）、`法律_法规列表`（国家法律法规）、`规范性文件列表`（规范性文件）。
3. **`pageNum` 参数不起作用**，接口返回的总页数是 `0`，每个栏目就是当前这一页。补 `pageNum=2..N` 拿到的还是第 1 页。
4. **这些栏目没有 `index_N.html` 历史页**：`index_1` 到 `index_30` 全部 404 回落到同一个首页（返回长度完全相同是最明显的信号）。看到“每个 index_N 返回一样大”就不要重试了，这站就这一页。

浙江的器械文件分布（供判断该接哪几个栏）：`col1229136205` 规范性文件 20 条/器械 5（都是核心程序）；`col1229215895` 法规文件 98 条/器械 11；`col1229893251` 产品召回 10 条/器械 5；`col1228989285` 通知公告 14 条/器械 0（全是疫苗、执业药师、中药炮制）。

### 上海：器械文件不在「规范性文件」栏

- `/gfxwj/` 规范性文件：`totalPage: 1`，只有 20 条，器械 3 条 —— 名字最像，内容最少
- `/qttzgg/` 其他通知公告：11 页，器械 164（批准注册通告、注销注册证通告）
- `/qtgzwj/` 其他公文：**103 页、1,917 条**，器械 397（大量转发国家局文件）

解析（三栏同一套）：
`<li class="list-ban"><a href=… title="完整标题">…</a><small class="listTime">2026.11.20</small></li>`
日期是 `2026.11.20` 点分隔，**用 `[-/]` 的正则会全部漏掉**（抓到 20 条却全是空日期）。分隔符写成 `[.\-/]`，再补零成 `YYYY-MM-DD`。

过滤：`/qtgzwj/` 的器械 397 条里 215 条是 `^国家药监局` 开头的转发、33 条是「批准注册 N 项产品的通告」，剔完入库约 311 条。

### 安徽：列表和翻页是两套 URL

列表：`https://mpa.ah.gov.cn/zwgz/gsgg/index.html`（`li.even` / `li.odd`，日期在 `<span class="right date">`）

翻页：页面里 `Ls.pagination("#page_xxx", …, {isTotal:true, total:5357, pageCount:358, param:"pageSize=15"})`，真地址是
`https://mpa.ah.gov.cn/content/column/<栏目ID>?pageIndex=N`（每页 15 条）。

全量 358 页用 0.25s 间隔约 2-3 分钟；**会被限速**（约 1/3 的页返回 HTTPError），必须重试（3 次 + 递增 sleep），只跑一遍会少一半数据。漏掉的页要记下来补（`failed_pages`），补完再合并——器械条目分散在各页，不能拿“抓到的部分”当全量。

### 省级过滤要多两道（除了转发+器械+结果）

省局站混进的杂质不止办理结果名单，实测还要剔：

- **许可/备案结果公示**：`《医疗器械经营许可证》开办[企业名]`、`变更（需现场检查）`、`注销[...]`、`首次注册产品公告`、`备案情况` —— 是办事结果，不是规定
- **新闻动态**：`实训|培训|调研|座谈|会议|考察|赴|助力|赋能|走访|交流` —— 省局网站把工作动态和法规发在一起，标题里带这些词的基本都是新闻

两条都要在入库前做（`merge_*.py` 那一层），不要留到 `build.py` 兜。

## 三、探测过但没接的

- **山东、四川、湖北、湖南、重庆、山西、天津、江西、贵州**：站点能连，但列表都是 JS 加载；湖北/重庆渲染后拿到的仍是导航链接（“其他主动公开文件”、区县市场局列表），江西是 `{{title}}` 占位。要接得逐个找接口。
- **12 个连不上（000）**：内蒙古、辽宁、吉林、黑龙江、福建、广西、海南、云南、陕西、青海、宁夏、新疆。**000 不等于这个省没网站**——多半是猜的域名过期了，真要接得重新找入口，别直接判断“这省没数据”。
- **甘肃** 返回 412，和 NMPA 一样是反爬，要 scrapling。
- **CMDE 审评论坛** `https://www.cmde.org.cn/xwdt/shpgzgg/index.html` 612 条，内容绝大多数是“优先审批/创新审查结果公示”，属办理结果，**不进库**。
- **CMDE 通知公告** `https://www.cmde.org.cn/xwdt/tzgg/index.html` 是转载政府网新闻（时政），**不是法规栏目，不要抓**。
- **中检院** `www.nifdc.org.cn` NXDOMAIN，域名已不存在。库里的「中检院」记录是 CMDE 分类界定栏目来的。

## 四、边际价值（修正过两次，以这版为准）

第一次结论「省局主体业务是药品，器械发文量天然小」—— 错，错因是多数栏目只抓了第一页。

第二次结论「省局能挖出几百条」—— 对上海、安徽成立，**对浙江不成立**。浙江全站栏目挖了三轮，器械合计约 22 条：法规文件 11、规范性文件 5、产品召回 5、工作文件 1，通知公告 0。

现行结论：

- 安徽「公示公告」5,357 条（358 页）→ 器械 483 条
- 上海三栏 2,142 条 → 器械 561 条（剔转发和结果后入 311）
- 河南「公告通告」14 页 → 255 条
- 江苏 320、广东 54、北京 32 —— 真到底了
- **浙江是真的少**：省局不转发国家局文件（上海转，所以上海多），自己的器械文件也就二十来条；它的器械监管重心在 `浙械eRPS` 电子申报系统、政务服务网办事指南、审评中心问答，那些不是法规

所以：**看到某省只有个位数，先查翻页和栏目名（90% 是这两样）；两样都排除了才承认这个省真的少，且要说清“少”的原因是什么（转不转发、业务重心在不在发文件）**。浙江这种结论要带着证据给用户（栏目清单、每栏条数、index_N 全 404），他才会接受。
