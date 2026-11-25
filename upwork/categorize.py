"""抓取多个 agent 相关关键词的职位,按类别自动归类统计。"""
import collections
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import upwork as uw
from playwright.sync_api import sync_playwright

KEYWORDS = ["AI agent", "chatbot", "AI automation", "RAG", "n8n", "voice agent"]

CATEGORIES = [
    ("Chatbot/客服", ["chatbot", "support", "customer service", "customer support"]),
    ("Automation/工作流", ["automation", "n8n", "workflow", "zapier", "make.com", "go high level"]),
    ("RAG/文档问答", ["rag", "retrieval", "document", "knowledge", "manual"]),
    ("语音/电话", ["voice", "phone", "whatsapp", "call", "speech"]),
    ("Lead Gen/外联", ["lead", "outreach", "sales", "prospect", "marketing"]),
    ("通用 Agent 开发", ["agent", "agentic", "llm", "langchain", "ai engineer", "ai developer"]),
]


def categorize(title):
    t = title.lower()
    for cat, keys in CATEGORIES:
        if any(k in t for k in keys):
            return cat
    return "其他"


def main():
    p = sync_playwright().start()
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]

    all_titles = []
    for kw in KEYWORDS:
        try:
            page = ctx.new_page()
            url = "https://www.upwork.com/nx/search/jobs/?q=" + urllib.parse.quote(kw) + "&sort=recency"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            uw.wait_ready(page)
            time.sleep(3)
            titles = page.eval_on_selector_all(
                'a[href*="/jobs/"]',
                "els => els.map(e => e.textContent.trim()).filter(t => t.length > 5)",
            )
            for t in titles:
                if t and t not in all_titles:
                    all_titles.append(t)
            page.close()
        except Exception as e:
            print(f"  [跳过 {kw}]: {str(e)[:60]}")
        time.sleep(2)

    browser.close()
    p.stop()

    counter = collections.Counter(categorize(t) for t in all_titles)
    total = len(all_titles)
    print(f"\n共收集 {total} 个职位标题\n")
    for cat, n in counter.most_common():
        print(f"  {cat}: {n} 个 ({n / total * 100:.0f}%)" if total else "  无数据")

    for cat, n in counter.most_common():
        print(f"\n=== {cat} ({n} 个) ===\n")
        for t in all_titles:
            if categorize(t) == cat:
                print(f"  - {t}")


if __name__ == "__main__":
    main()
