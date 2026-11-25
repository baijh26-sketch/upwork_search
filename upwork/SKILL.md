---
name: upwork
description: Fetch, search, and archive Upwork job postings. Extracts job details (title/budget/description/skills/posted) AND employer credit signals (country, payment-verified, total paid, hires, rating, proposal count). Use whenever the user wants to view, fetch, track, search, or vet Upwork jobs.
---

# Upwork 职位抓取

通过 CDP 驱动一个**已登录 Upwork 的真实 Chrome**,抓取职位、解析 Nuxt payload,输出 Markdown 并存档到本地 SQLite。

## 前置:一个带调试端口的 Chrome(已登录)

```bash
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="<你的调试 profile 路径>"
```

> 这个 profile 已登录 Upwork(路径每台机器自定,勿写死)。登录过期就在打开的 Chrome 里重新登录。

## 命令(都加 `--cdp http://127.0.0.1:9222`)

```bash
# 抓单个职位详情(含雇主信号)
python scripts/upwork.py --cdp http://127.0.0.1:9222 job "<职位URL>"

# 搜职位(按最新排序)
python scripts/upwork.py --cdp http://127.0.0.1:9222 search "web scraping"

# 抓「最新职位」feed
python scripts/upwork.py --cdp http://127.0.0.1:9222 list

# 读本地存档(可按关键词过滤)
python scripts/upwork.py recent
python scripts/upwork.py recent "python"

# 从浏览器复制 Cookie 导入(跳过重新登录)
python scripts/upwork.py import-cookies "<Cookie header>"
```

## `job` 命令提取的雇主信号

除标题/预算/描述/技能/发布时间外,还提取:
- `buyer_country` 雇主国籍 · `payment_verified` 支付是否认证 · `total_paid` 历史支付总额
- `client_hires` 雇佣次数 · `client_score` 评分 · `proposals` 提案数 · `interviewing` 面试中

> 判断单值不值得投:`Hires≥1` 警惕已有人占位;`评分 0 + 未认证` = 新客户风险;`$3/hr` 之类极低价直接跳过。

## 用法约定(Claude 遵循)

1. 用户给职位链接 → `job "<url>"`,用返回的雇主信号 + 描述判断是否值得投、写 proposal。
2. 用户想看/找职位 → `search "<关键词>"`(比 `list` 更多结果)。
3. 用户提到「之前存的职位」→ `recent [关键词]`。
4. 报「未登录」→ 提示用户在调试 Chrome 重新登录;CDP 连不上 → 提示先启动上面的调试 Chrome。
5. 报「被 Cloudflare 拦截」→ 请求太密集被限流,等 15-30 分钟冷却,或重启调试 Chrome。
6. `connect_over_cdp` 报 `Browser.setDownloadBehavior ... not supported` → Chrome 自动升级导致 Playwright 协议不匹配,`pip install --upgrade playwright`(需 ≥1.65.2 配 Chrome 157.0.8103.80)。
7. 解析失败(标题/描述为空)→ Upwork 改了页面结构,优先检查 `extract_nuxt_job`。

## 关键事实

- 详情页是 **Nuxt 3**,数据在 `<script type="application/json" id="__NUXT_DATA__">` 的扁平数组里(整数引用 + `ShallowReactive` 等标记),解析器 `_resolve_nuxt_payload`。
- 职位在 `vuex.jobDetails.job`;技能名在 `vuex.jobDetails.sands.additionalSkills`;雇主信息在 `vuex.jobDetails.buyer`。
- 详情页重定向到 `/jobs/~<ciphertext>`;列表/搜索页职位链接是 `/jobs/<slug>_~<ciphertext>`。
- Cloudflare 拦截无头浏览器,必须用真实可见 Chrome;Chrome 157 忽略默认 profile 上的 `--remote-debugging-port`,需用非默认 `--user-data-dir`。

## 存档位置

- DB:`data/jobs.db`(表 `jobs`,主键职位 id,重复抓取覆盖更新,`raw_json` 存完整数据)。
- 调试 profile:一个已登录 Upwork 的 Chrome profile(路径自定,勿删)。
