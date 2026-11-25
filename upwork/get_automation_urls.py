"""抓 Automation 方向(AI automation / n8n / GoHighLevel)的职位 {title, url}。"""
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import upwork as uw
from playwright.sync_api import sync_playwright

KEYWORDS = ["AI automation", "n8n", "GoHighLevel"]


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
            items = page.eval_on_selector_all(
                'a[href*="/jobs/"]',
                "els => els.map(e => ({t: e.textContent.trim(), h: e.href})).filter(x => x.t.length > 5)",
            )
            for it in items:
                m = re.search(r"/jobs/[^/]*~([\d]+)", it["h"])
                if m:
                    jid = m.group(1)
                    if jid not in seen:
                        seen[jid] = (it["t"], it["h"])
            page.close()
        except Exception as e:
            print(f"[skip {kw}]: {str(e)[:60]}")
        time.sleep(2)

    browser.close()
    p.stop()

    for jid, (t, h) in seen.items():
        print(f"{t} || {h}")


if __name__ == "__main__":
    main()
