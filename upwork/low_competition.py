"""搜多个「Claude 可完成」关键词,抓提案数/时薪/国家/描述,按提案数升序排(最少=最低竞争)。"""
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import upwork as uw
from playwright.sync_api import sync_playwright

KEYWORDS = ["Claude", "web scraping", "data extraction", "n8n", "chatbot", "RAG"]


def parse_proposals(s):
    s = (s or "").lower()
    if not s:
        return 99
    if "fewer than 5" in s or "less than 5" in s:
        return 2
    if "50+" in s:
        return 60
    m = re.search(r"(\d+)\s*to\s*(\d+)", s)
    if m:
        return (int(m.group(1)) + int(m.group(2))) / 2
    m = re.search(r"(\d+)", s)
    if m:
        return int(m.group(1))
    return 99


def parse_card(a):
    href = (a.get_attribute("href") or "").strip()
    m = re.search(r"/jobs/[^/]*~([\d]+)", href)
    if not m:
        return None
    jid = m.group(1)
    title = (a.get_attribute("aria-label") or "").strip() or (a.inner_text() or "").strip()
    txt = a.evaluate("""e => {
        let el = e.closest('article') || e.closest('section');
        if (!el) { el = e; for (let i=0;i<5&&el.parentElement;i++) el = el.parentElement; }
        return el ? el.innerText : '';
    }""") or ""

    pm = re.search(r"Proposals:\s*([^\n]+)", txt)
    proposals = parse_proposals(pm.group(1) if pm else "")

    posted = ""
    pm = re.search(r"Posted\s+([^\n]+)", txt)
    if pm:
        posted = pm.group(1).strip()

    rate = ""
    if re.search(r"Hourly", txt):
        rm = re.search(r"Hourly:\s*\$([^\n]+)", txt)
        rate = "$" + rm.group(1).strip() if rm else "Hourly"
    elif re.search(r"Fixed", txt):
        rate = "Fixed"

    spent = ""
    sm = re.search(r"\$([\dK.,]+)\+?\s*spent", txt)
    if sm:
        spent = "$" + sm.group(1) + "+"

    # 国家:Location 后面第一段
    country = ""
    cm = re.search(r"Location\s*\n\s*([A-Z][A-Za-z ]{2,30})", txt)
    if cm:
        country = cm.group(1).strip()

    # 描述:Est. time 之后的片段
    desc = ""
    dm = re.search(r"hrs?/week\s*\n\s*\n(.+)", txt, re.S)
    if dm:
        desc = " ".join(dm.group(1).split())[:140]
    else:
        # fallback: 最后一段非空
        parts = [p.strip() for p in txt.split("\n") if p.strip()]
        if parts:
            desc = parts[-1][:140]

    return {
        "id": jid,
        "title": title,
        "url": "https://www.upwork.com" + href if href.startswith("/") else href,
        "proposals": proposals,
        "posted": posted,
        "rate": rate,
        "spent": spent,
        "country": country,
        "desc": desc,
    }


def main():
    p = sync_playwright().start()
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]

    seen = {}
    for kw in KEYWORDS:
        try:
            page = ctx.new_page()
            url = "https://www.upwork.com/nx/search/jobs/?q=" + urllib.parse.quote(kw) + "&sort=recency"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            uw.wait_ready(page)
            time.sleep(3)
            for _ in range(8):
                try:
                    page.mouse.wheel(0, 3000)
                except Exception:
                    pass
                time.sleep(1)
            for a in page.query_selector_all('a[href*="/jobs/"]'):
                try:
                    c = parse_card(a)
                except Exception:
                    continue
                if c and c["id"] not in seen:
                    seen[c["id"]] = c
            page.close()
        except Exception as e:
            print(f"[skip {kw}]: {str(e)[:60]}", file=sys.stderr)
        time.sleep(2)

    browser.close()
    p.stop()

    rows = sorted(seen.values(), key=lambda r: (r["proposals"], r["posted"]))
    print(f"\n共 {len(rows)} 个职位,按提案数升序(最少=最低竞争)\n")
    for r in rows:
        pn = r["proposals"]
        if pn <= 2:
            pn = "<5"
        elif pn % 1:
            pn = int(pn)
        else:
            pn = int(pn)
        line = f"[提案 {pn}] {r['title']}"
        meta = " · ".join(x for x in [r["rate"], r["country"], r["spent"], r["posted"]] if x)
        if meta:
            line += f"  ({meta})"
        print(line)
        print(f"    {r['url']}")
        if r["desc"]:
            print(f"    {r['desc']}")
        print()


if __name__ == "__main__":
    main()
