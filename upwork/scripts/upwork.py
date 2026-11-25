#!/usr/bin/env python3
"""Upwork job fetcher — fetch single jobs, the recent-jobs feed, and archive locally.

How it authenticates:
  * Uses Playwright + your real installed Chrome (`channel="chrome"`).
  * Uses a DEDICATED persistent Chrome profile under ./profile (NOT your daily one),
    so you log into Upwork ONCE (including 2FA) and the session is reused.
  * Cloudflare sees a real Chrome with a real fingerprint + valid cookies.

Commands:
  python upwork.py login                 # open a browser, log in once (auto-detects success)
  python upwork.py job "<job-url>"       # fetch + archive one job, print Markdown
  python upwork.py list [--details]      # fetch + archive the most-recent feed
  python upwork.py recent [keyword]      # show archived jobs from the local DB
  python upwork.py import-cookies "<c>"  # import a copied Cookie header (skip re-login)

Flags:
  --headless    run headless (faster, but Cloudflare may block; default: visible browser)
  --cdp <url>   connect to a running Chrome via CDP (e.g. http://127.0.0.1:9222) instead of launching
  --no-archive  do not write to the local DB
  --limit N     (recent) max rows
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

# Ensure UTF-8 output on Windows consoles (avoids GBK mojibake in job titles).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DATA_DIR = SKILL_DIR / "data"
PROFILE_DIR = SKILL_DIR / "profile"
DB_PATH = DATA_DIR / "jobs.db"

MOST_RECENT_URL = "https://www.upwork.com/nx/find-work/most-recent"
LOGIN_URL = "https://www.upwork.com/ab/account-security/login"

ARGS = None

# Lightweight anti-bot markers (helps avoid being flagged as automation).
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""


# --------------------------------------------------------------------------- #
# infra
# --------------------------------------------------------------------------- #
def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def init_db() -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            description TEXT,
            skills TEXT,
            budget TEXT,
            posted_at TEXT,
            fetched_at TEXT,
            raw_json TEXT
        )
        """
    )
    con.commit()
    return con


def launch(headless: bool):
    """Open a browser context. With --cdp, attach to the running debug Chrome instead."""
    pw = sync_playwright().start()
    if ARGS.cdp:
        browser = pw.chromium.connect_over_cdp(ARGS.cdp)
        return pw, browser.contexts[0], False
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=headless,
        viewport={"width": 1366, "height": 900},
        locale="en-US",
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx.add_init_script(STEALTH_JS)
    return pw, ctx, True


def shutdown(pw, ctx, close_ctx: bool):
    """Tear down. In CDP mode we only disconnect, leaving the user's Chrome running."""
    if close_ctx:
        try:
            ctx.close()
        except Exception:
            pass
    pw.stop()


BLOCK_MARKERS = (
    "just a moment",
    "challenge",
    "blocked",
    "attention required",
    "verify you are human",
    "access denied",
    "请稍候",
    "请稍后",
    "正在验证",
)


def _looks_blocked(title: str) -> bool:
    t = (title or "").lower()
    return any(m in t for m in BLOCK_MARKERS)


def wait_ready(page, timeout_ms: int = 60000):
    """Wait out a Cloudflare interstitial (managed challenge / 'Just a moment')."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            title = (page.title() or "").strip()
            if title and not _looks_blocked(title):
                break
        except Exception:
            pass
        time.sleep(2)
    return page


def goto_ready(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    wait_ready(page)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    return page


def is_logged_in(page) -> bool:
    url = (page.url or "").lower()
    return "login" not in url and "account-security" not in url


def job_id_from_url(url: str) -> str:
    m = re.search(r"/details/~([\w-]+)", url)
    if m:
        return m.group(1)
    return hashlib.sha1(url.encode()).hexdigest()[:20]


# --------------------------------------------------------------------------- #
# parsing helpers (multiple fallbacks; Upwork is a Next.js SPA)
# --------------------------------------------------------------------------- #
def _inner_text(el):
    try:
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def extract_jsonld(page):
    out = []
    for el in page.query_selector_all('script[type="application/ld+json"]'):
        txt = _inner_text(el)
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and "JobPosting" in str(it.get("@type", "")):
                out.append(it)
    return out


def extract_next_data(page):
    el = page.query_selector("#__NEXT_DATA__")
    if not el:
        return None
    try:
        return json.loads(_inner_text(el))
    except Exception:
        return None


def find_job_objects(obj, found=None, depth=0, _max=14):
    if found is None:
        found = []
    if depth > _max or obj is None:
        return found
    if isinstance(obj, dict):
        if isinstance(obj.get("title"), str) and any(
            k in obj for k in ("description", "skills", "budget", "publishedOnDateTime", "postedOn")
        ):
            found.append(obj)
        for v in obj.values():
            find_job_objects(v, found, depth + 1, _max)
    elif isinstance(obj, list):
        for v in obj:
            find_job_objects(v, found, depth + 1, _max)
    return found


def score_job(o) -> int:
    s = 0
    if isinstance(o.get("description"), str):
        s += min(len(o["description"]), 2000)
    if o.get("skills"):
        s += 50
    if o.get("budget") or o.get("amount") or o.get("hourlyBudget"):
        s += 30
    return s


def norm_skills(v):
    out = []
    if isinstance(v, list):
        for x in v:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                for key in ("name", "prefLabel", "title"):
                    if isinstance(x.get(key), str):
                        out.append(x[key])
                        break
    return out


def pick_str(o, *keys):
    for k in keys:
        v = o.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def fmt_budget(v):
    if v in (None, "", []):
        return ""
    if isinstance(v, dict):
        parts = []
        for k in ("value", "amount"):
            if v.get(k) not in (None, ""):
                parts.append(str(v[k]))
        if v.get("currency"):
            parts.append(str(v["currency"]))
        if v.get("unitText"):
            parts.append(str(v["unitText"]))
        return " ".join(parts) or json.dumps(v, ensure_ascii=False)
    return str(v)


def job_from_obj(o, url):
    return {
        "id": job_id_from_url(url),
        "url": url,
        "title": pick_str(o, "title", "jobTitle", "name"),
        "description": pick_str(o, "description", "jobDescription", "excerpt"),
        "skills": norm_skills(o.get("skills")),
        "budget": fmt_budget(o.get("budget") or o.get("amount") or o.get("hourlyBudget")),
        "posted_at": pick_str(o, "publishedOnDateTime", "postedAt", "createdAt", "datePosted"),
    }


def extract_from_dom(page):
    """Last-resort DOM fallback. Selectors are best-effort and may need tuning."""
    title = ""
    for sel in ("h1", "[data-test='job-title']", "h2[data-test='jobTitle']"):
        el = page.query_selector(sel)
        if el:
            title = _inner_text(el)
            if title:
                break
    if not title:
        title = (page.title() or "").replace(" - Upwork", "").strip()

    desc = ""
    for sel in (
        "[data-test='JobDescription']",
        "[data-ev-sublocation='job_detail_description']",
        "section[class*='description']",
    ):
        el = page.query_selector(sel)
        if el:
            desc = _inner_text(el)
            if desc:
                break

    skills = []
    for el in page.query_selector_all("section[class*='skills'] a, [data-test='skill']"):
        t = _inner_text(el)
        if t:
            skills.append(t)

    budget = ""
    for el in page.query_selector_all("[data-test='budget'], [data-test='job-type'], [class*='budget']"):
        t = _inner_text(el)
        if t:
            budget = t
            break

    return {"title": title, "description": desc, "skills": skills, "budget": budget, "posted_at": ""}


NUXT_TYPE_MARKERS = {"ShallowReactive", "Reactive", "Ref", "ShallowRef", "LazyRef", "ShallowReadonly", "Readonly"}


def _resolve_nuxt_payload(arr):
    """Resolve Nuxt 3's flat `__NUXT_DATA__` array (integer refs + type markers) into nested data."""
    memo = {}

    def r(i):
        if isinstance(i, int):
            if i in memo:
                return memo[i]
            if i < 0 or i >= len(arr):
                return i
            memo[i] = None
            memo[i] = _v(arr[i])
            return memo[i]
        return _v(i)

    def _v(node):
        if isinstance(node, list):
            if len(node) == 2 and isinstance(node[0], str) and node[0] in NUXT_TYPE_MARKERS and isinstance(node[1], int):
                return r(node[1])
            return [r(x) if isinstance(x, int) else _v(x) for x in node]
        if isinstance(node, dict):
            return {
                (r(k) if isinstance(k, int) else k): (r(v) if isinstance(v, int) else _v(v))
                for k, v in node.items()
            }
        return node

    return r(0)


def extract_activity(page):
    """提取 'Activity on this job' 段(Proposals/Hires/Interviewing/Invites),用 DOM 查询而非正则。"""
    try:
        return page.evaluate(
            """
            () => {
                const out = {};
                document.querySelectorAll('li.ca-item').forEach(li => {
                    const t = li.querySelector('.title');
                    const v = li.querySelector('.value');
                    if (t && v) out[t.textContent.trim()] = v.textContent.trim();
                });
                return out;
            }
            """
        )
    except Exception:
        return {}


def extract_nuxt_job(page):
    """Extract the full job object from Upwork's Nuxt `__NUXT_DATA__` payload (job detail page)."""
    # Activity 段(Proposals/Hires/Interviewing)是客户端异步加载的,等它出现
    time.sleep(7)
    raw = page.content()
    m = re.search(r'<script type="application/json"[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', raw, re.DOTALL)
    if not m:
        return None
    try:
        arr = json.loads(m.group(1).strip())
    except Exception:
        return None
    root = _resolve_nuxt_payload(arr)
    if not isinstance(root, dict):
        return None
    jd = root.get("vuex", {}).get("jobDetails")
    if not isinstance(jd, dict):
        return None
    job = jd.get("job")
    if not isinstance(job, dict) or not job.get("title"):
        return None

    skills = []
    sands = jd.get("sands")
    if isinstance(sands, dict):
        for s in sands.get("additionalSkills") or []:
            if isinstance(s, dict) and s.get("name"):
                skills.append(s["name"])

    ext = job.get("extendedBudgetInfo") or {}
    hmin = ext.get("hourlyBudgetMin")
    hmax = ext.get("hourlyBudgetMax")
    if hmin is not None and (hmin or hmax):
        budget = f"Hourly ${hmin}-${hmax}/hr"
        job_type = "Hourly"
    elif (job.get("budget") or {}).get("amount"):
        budget = f"Fixed ${job['budget']['amount']}"
        job_type = "Fixed-price"
    else:
        budget = ""
        job_type = ""

    buyer = jd.get("buyer") or {}
    stats = buyer.get("stats") or {}

    act = extract_activity(page)
    a = lambda *keys: next((act.get(k, "") for k in keys if act.get(k)), "")

    return {
        "title": job.get("title") or "",
        "description": job.get("description") or "",
        "skills": skills,
        "budget": budget,
        "job_type": job_type,
        "posted_at": job.get("postedOn") or "",
        "workload": job.get("workload") or "",
        "duration": job.get("durationLabel") or ((job.get("engagementDuration") or {}).get("label")) or "",
        "category": (job.get("category") or {}).get("name") or "",
        "questions": [q.get("question") for q in (job.get("questions") or []) if isinstance(q, dict) and q.get("question")],
        "buyer_country": (buyer.get("location") or {}).get("country") or "",
        "payment_verified": bool(buyer.get("isPaymentMethodVerified")),
        "total_paid": (stats.get("totalCharges") or {}).get("amount"),
        "client_hires": stats.get("totalJobsWithHires"),
        "client_score": stats.get("score"),
        "proposals": a("Proposals:", "Proposals"),
        "interviewing": a("Interviewing:", "Interviewing"),
        "hires": a("Hires:", "Hires"),
        "invites_sent": a("Invites sent:", "Invites sent"),
    }


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def fetch_job(url: str):
    ensure_dirs()
    pw, ctx, close_ctx = launch(headless=ARGS.headless)
    page = None
    try:
        page = ctx.new_page()
        goto_ready(page, url)
        if _looks_blocked(page.title() or ""):
            print("❌ 被 Cloudflare 拦截(挑战未通过)。请稍后重试,或加 --headed 用可见浏览器。", file=sys.stderr)
            return None
        if not is_logged_in(page):
            print("❌ 未登录或会话已失效。请先运行: python scripts/upwork.py login", file=sys.stderr)
            return None

        data = extract_nuxt_job(page)

        if not data:
            for jl in extract_jsonld(page):
                data = {
                    "title": pick_str(jl, "title", "name"),
                    "description": pick_str(jl, "description"),
                    "skills": norm_skills(jl.get("skills")),
                    "budget": fmt_budget(jl.get("estimatedSalary") or jl.get("baseSalary")),
                    "posted_at": pick_str(jl, "datePosted"),
                }
                break

        if not data:
            nd = extract_next_data(page)
            if nd:
                cands = find_job_objects(nd)
                if cands:
                    cands.sort(key=score_job, reverse=True)
                    data = job_from_obj(cands[0], url)

        if not data:
            data = extract_from_dom(page)

        data = data or {}
        data.setdefault("id", job_id_from_url(url))
        data.setdefault("url", url)
        return data
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        shutdown(pw, ctx, close_ctx)


def fetch_list(url=None):
    ensure_dirs()
    pw, ctx, close_ctx = launch(headless=ARGS.headless)
    page = None
    try:
        page = ctx.new_page()
        goto_ready(page, url or MOST_RECENT_URL)
        if _looks_blocked(page.title() or ""):
            print("❌ 被 Cloudflare 拦截(挑战未通过)。请稍后重试,或加 --headed 用可见浏览器。", file=sys.stderr)
            return []
        if not is_logged_in(page):
            print("❌ 未登录或会话已失效。请先运行: python scripts/upwork.py login", file=sys.stderr)
            return []

        # scroll to trigger lazy-loaded cards
        for _ in range(10):
            try:
                page.mouse.wheel(0, 3000)
            except Exception:
                pass
            time.sleep(1)

        items, seen = [], set()
        for a in page.query_selector_all('a[href*="/jobs/"]'):
            try:
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            m = re.search(r"/jobs/[^/]*~([\d]+)", href)
            if not m:
                continue
            jid = m.group(1)
            if jid in seen:
                continue
            seen.add(jid)
            url = "https://www.upwork.com/jobs/~" + jid
            title = _inner_text(a) or (a.get_attribute("aria-label") or "")
            items.append({"id": jid, "url": url, "title": title})
        return items
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        shutdown(pw, ctx, close_ctx)


# --------------------------------------------------------------------------- #
# archive / read-back
# --------------------------------------------------------------------------- #
def archive(con: sqlite3.Connection, data: dict):
    con.execute(
        """
        INSERT INTO jobs (id, url, title, description, skills, budget, posted_at, fetched_at, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url, title=excluded.title, description=excluded.description,
            skills=excluded.skills, budget=excluded.budget, posted_at=excluded.posted_at,
            fetched_at=excluded.fetched_at, raw_json=excluded.raw_json
        """,
        (
            data.get("id"),
            data.get("url"),
            data.get("title", ""),
            data.get("description", ""),
            json.dumps(data.get("skills", []), ensure_ascii=False),
            data.get("budget", ""),
            data.get("posted_at", ""),
            datetime.now(timezone.utc).isoformat(),
            json.dumps(data, ensure_ascii=False),
        ),
    )
    con.commit()


def parse_cookie_header(header: str, domain=".upwork.com"):
    cookies = []
    for part in header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
    return cookies


def do_import_cookies(header):
    ensure_dirs()
    pw, ctx, close_ctx = launch(headless=True)
    try:
        cookies = parse_cookie_header(header)
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        try:
            page.goto("https://www.upwork.com/", wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        stored = [c for c in ctx.cookies() if "upwork" in (c.get("domain") or "")]
        print(f"✅ 已导入 {len(cookies)} 个 cookie,当前 upwork cookie 共 {len(stored)} 个。")
    finally:
        shutdown(pw, ctx, close_ctx)


def recent(keyword=None, limit=20):
    con = init_db()
    q = "SELECT id, url, title, skills, budget, posted_at, fetched_at FROM jobs"
    args = []
    if keyword:
        q += " WHERE title LIKE ? OR description LIKE ? OR skills LIKE ?"
        args = [f"%{keyword}%"] * 3
    q += " ORDER BY fetched_at DESC LIMIT ?"
    args.append(limit)
    rows = con.execute(q, args).fetchall()
    con.close()
    return rows


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def print_job(data: dict):
    print(f"# {data.get('title') or '(无标题)'}")
    if data.get("category"):
        print(f"**分类:** {data['category']}")
    if data.get("budget"):
        print(f"**预算:** {data['budget']}")
    if data.get("job_type"):
        print(f"**类型:** {data['job_type']}")
    if data.get("posted_at"):
        print(f"**发布时间:** {data['posted_at']}")
    client_bits = []
    if data.get("buyer_country"):
        client_bits.append(data["buyer_country"])
    if data.get("payment_verified") is not None:
        client_bits.append("支付认证" if data["payment_verified"] else "未认证支付")
    if data.get("total_paid") is not None:
        client_bits.append(f"历史支付 ${data['total_paid']}")
    if data.get("client_hires") is not None:
        client_bits.append(f"雇佣 {data['client_hires']} 次")
    if data.get("client_score") is not None:
        client_bits.append(f"评分 {data['client_score']}")
    if client_bits:
        print(f"**雇主:** {', '.join(client_bits)}")
    if data.get("proposals"):
        print(f"**提案数:** {data['proposals']} | 面试中: {data.get('interviewing') or 0} | 已雇: {data.get('hires') or 0} | 邀请: {data.get('invites_sent') or 0}")
    if data.get("workload"):
        print(f"**工作量:** {data['workload']}")
    if data.get("duration"):
        print(f"**时长:** {data['duration']}")
    skills = data.get("skills") or []
    if skills:
        print(f"**技能:** {', '.join(skills)}")
    print()
    if data.get("description"):
        print(data["description"])
    questions = data.get("questions") or []
    if questions:
        print()
        print("**筛选问题:**")
        for q in questions:
            print(f"- {q}")
    print()
    print(f"🔗 {data.get('url')}")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def do_login():
    ensure_dirs()
    print("正在打开浏览器… 请在弹出的窗口里完成登录(含两步验证)。")
    print("登录成功后脚本会自动检测并退出。")
    pw, ctx, close_ctx = launch(headless=False)
    try:
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        wait_ready(page)
        logged = False
        for _ in range(180):  # up to ~15 min
            time.sleep(5)
            try:
                u = (page.url or "").lower()
                # After a real login Upwork redirects off the account-security/login path.
                if "upwork.com" in u and "login" not in u and "account-security" not in u:
                    logged = True
                    break
            except Exception:
                pass
        if logged:
            print("✅ 登录成功,会话已保存到 profile 目录,后续抓取会自动复用。")
        else:
            print("⚠️ 超时未检测到登录成功,请重试。")
    finally:
        shutdown(pw, ctx, close_ctx)


def run_job(url):
    data = fetch_job(url)
    if not data:
        return
    print_job(data)
    if not ARGS.no_archive:
        con = init_db()
        archive(con, data)
        con.close()
        print("\n(已存档到本地)")


def run_list(url=None):
    items = fetch_list(url)
    if not items:
        print("未抓取到职位。")
        return
    con = None if ARGS.no_archive else init_db()
    for it in items:
        print(f"- {it['title'] or '(无标题)'}  {it['url']}")
        if ARGS.details:
            d = fetch_job(it["url"])
            if d:
                print_job(d)
            time.sleep(2)
        if con:
            archive(
                con,
                {
                    "id": it["id"], "url": it["url"], "title": it["title"],
                    "description": "", "skills": [], "budget": "", "posted_at": "",
                },
            )
    if con:
        con.close()
    print(f"\n共 {len(items)} 个职位")


def run_search():
    kw = ARGS.arg
    if not kw:
        print("search 需要一个关键词")
        return
    import urllib.parse
    run_list("https://www.upwork.com/nx/search/jobs/?q=" + urllib.parse.quote(kw) + "&sort=recency")


def run_recent():
    rows = recent(ARGS.arg, ARGS.limit)
    if not rows:
        print("本地存档为空。先运行 `job <url>` 或 `list` 抓取。")
        return
    print(f"# 已存档职位 ({len(rows)})\n")
    for jid, url, title, skills, budget, posted, fetched in rows:
        print(f"- **{title or '(无标题)'}**")
        if budget:
            print(f"  - 预算: {budget}")
        if posted:
            print(f"  - 发布: {posted}")
        if skills:
            try:
                sk = ", ".join(json.loads(skills))
            except Exception:
                sk = skills
            if sk:
                print(f"  - 技能: {sk}")
        print(f"  - {url}")


def main():
    parser = argparse.ArgumentParser(description="Upwork job fetcher")
    parser.add_argument("command", choices=["login", "job", "list", "search", "recent", "import-cookies"])
    parser.add_argument("arg", nargs="?", help="job URL (for 'job'), keyword (for 'recent')")
    parser.add_argument("--details", action="store_true", help="(list) fetch full details for each job")
    parser.add_argument("--limit", type=int, default=20, help="(recent) max rows")
    parser.add_argument("--headless", action="store_true", help="run headless (faster, but Cloudflare may block)")
    parser.add_argument("--cdp", help="connect to a running Chrome via CDP (e.g. http://127.0.0.1:9222) instead of launching")
    parser.add_argument("--no-archive", action="store_true", help="do not write to the local DB")
    global ARGS
    ARGS = parser.parse_args()

    if ARGS.command == "login":
        do_login()
    elif ARGS.command == "job":
        if not ARGS.arg:
            parser.error("job 需要一个职位 URL")
        run_job(ARGS.arg)
    elif ARGS.command == "list":
        run_list()
    elif ARGS.command == "search":
        run_search()
    elif ARGS.command == "recent":
        run_recent()
    elif ARGS.command == "import-cookies":
        if not ARGS.arg:
            parser.error("import-cookies 需要从浏览器复制的 Cookie 字符串")
        do_import_cookies(ARGS.arg)


if __name__ == "__main__":
    main()
