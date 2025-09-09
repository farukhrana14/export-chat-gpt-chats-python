# -*- coding: utf-8 -*-
"""
ChatGPT Team – Export chats to JSON.
Strategy:
  1) Primary: call in-page endpoints (/backend-api/conversations, /backend-api/conversation/:id)
     using your logged-in browser context (cookies carry over automatically).
  2) Fallback: robust DOM scraping if API is unavailable or returns 0.

Edit ONLY the CONFIG block below.
"""

import json
import os
import sys
import time
import platform
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ==== CONFIG (EDIT ME) =======================================================
USE_REAL_CHROME_PROFILE = True  # True recommended to avoid Cloudflare loops

# macOS:
MAC_CHROME_DIR = "~/Library/Application Support/Google/Chrome"
# Windows:
WIN_CHROME_DIR = r"C:\Users\resea\AppData\Local\Google\Chrome\User Data"
# Linux:
LINUX_CHROME_DIR = "~/.config/google-chrome"

# Common profile names: "Default", "Profile 1", "Profile 2"
CHROME_PROFILE_NAME = "Default"

OUTPUT = "chatgpt_export.json"
# ============================================================================

def _expand_profile_dir():
    sysname = platform.system()
    if sysname == "Darwin":
        base = MAC_CHROME_DIR
    elif sysname == "Windows":
        base = WIN_CHROME_DIR
    else:
        base = LINUX_CHROME_DIR
    return str(Path(os.path.expanduser(base)) / CHROME_PROFILE_NAME)

# ------------------ Fallback DOM selectors (only if API path fails) ---------
SELECTORS = {
    # Sidebar
    "sidebar_container_candidates": [
        '[data-testid="sidebar"]',
        'nav[role="navigation"]',
        'aside'
    ],
    # Conversation links (multiple variants for different rollouts)
    "chat_link_candidates": [
        '[data-testid="conversation-link"]',
        'a[href^="/c/"]',
        'a[href*="/c/"]',
        'a[data-radix-collection-item][href*="/c/"]'
    ],
    # Conversation page
    "conversation_title_candidates": [
        '[data-testid="conversation-title"]',
        'header :is(h1,h2)',
        'h1:below(:text("Share"))'
    ],
    # Messages / turns
    "turn_candidates": [
        '[data-message-author-role]',                 # most stable
        '[data-testid="conversation-turn"]',
        'article div:has([data-message-author-role])'
    ],
    "role_attr": "[data-message-author-role]",
    "content_candidates": [
        '[data-message-content]',
        '.markdown',
        '[data-testid="model-viewer"]',
        'article'
    ]
}

SCROLL_PAUSE = 0.8
MAX_SCROLL_PASSES = 80

def ensure_logged_in(page):
    page.goto("https://chat.openai.com/", wait_until="domcontentloaded")
    try:
        page.wait_for_timeout(1200)
        # Wait for either app shell or any nav; if none, give time to login
        page.wait_for_selector("main, nav, aside, [data-testid='sidebar']", timeout=20000)
    except PWTimeoutError:
        print("Log in if needed. Waiting for the app shell to load…")
        page.wait_for_selector("main, nav, aside, [data-testid='sidebar']", timeout=180000)

# ========================= Primary: In-page API ==============================

LIST_PAGE_SIZE = 50  # conversations per page

LIST_JS = """
async (limit) => {
  const res = [];
  let offset = 0;
  // Newer endpoints use /backend-api/conversations?offset=...&limit=...
  // Some rollouts also accept /api/conversations
  const urls = (o,l) => [
    `/backend-api/conversations?offset=${o}&limit=${l}&order=updated`,
    `/api/conversations?offset=${o}&limit=${l}&order=updated`
  ];
  while (true) {
    let ok = false, data = null;
    for (const u of urls(offset, limit)) {
      try {
        const r = await fetch(u, { credentials: 'include' });
        if (r.ok) {
          data = await r.json();
          ok = true;
          break;
        }
      } catch (e) {}
    }
    if (!ok || !data) break;
    if (Array.isArray(data.items)) {
      res.push(...data.items);
      if (data.has_more) {
        offset += data.limit ?? limit;
      } else {
        break;
      }
    } else if (Array.isArray(data.conversations)) { // older shape
      res.push(...data.conversations);
      if (data.has_more) {
        offset += data.limit ?? limit;
      } else {
        break;
      }
    } else {
      break;
    }
  }
  return res;
}
"""

FETCH_ONE_JS = """
async (cid) => {
  const tryUrls = [
    `/backend-api/conversation/${cid}`,
    `/api/conversation/${cid}`
  ];
  for (const u of tryUrls) {
    try {
      const r = await fetch(u, { credentials: 'include' });
      if (r.ok) return await r.json();
    } catch (e) {}
  }
  return null;
}
"""

def list_conversations_via_api(page):
    try:
        items = page.evaluate(LIST_JS, LIST_PAGE_SIZE)
        return items or []
    except Exception:
        return []

def fetch_conversation_via_api(page, cid):
    try:
        data = page.evaluate(FETCH_ONE_JS, cid)
        return data
    except Exception:
        return None

def convert_api_conversation(api_obj):
    """Normalize different shapes to {id, title, messages:[{role,text,html?}]}"""
    cid = api_obj.get("id") or api_obj.get("conversation_id")
    title = api_obj.get("title") or api_obj.get("current_node") or "Untitled"

    # Different payloads:
    # - Full conversation fetch returns "mapping" of nodes.
    msgs = []
    mapping = api_obj.get("mapping") or {}
    if mapping:
        # nodes keyed by id -> { message: { author: { role }, content: { parts / text } } }
        # Keep chronological by created time if present
        nodes = list(mapping.values())
        nodes.sort(key=lambda n: (n.get("message", {}).get("create_time") or 0))
        for n in nodes:
            m = n.get("message") or {}
            author = (m.get("author") or {}).get("role") or "assistant"
            content = m.get("content") or {}
            text = ""
            # parts could be list of strings or objects
            parts = content.get("parts")
            if isinstance(parts, list) and parts:
                # Join string parts; if objects, try 'text'
                chunk = []
                for p in parts:
                    if isinstance(p, str):
                        chunk.append(p)
                    elif isinstance(p, dict) and "text" in p:
                        chunk.append(p["text"])
                text = "\n".join(chunk).strip()
            elif isinstance(content.get("text"), str):
                text = content["text"].strip()
            elif isinstance(content, str):
                text = content.strip()
            msgs.append({"role": author, "text": text, "html": None})
    else:
        # Conversation list item (no messages) — just return header
        msgs = []

    return {"id": cid, "title": title, "messages": msgs}

# ========================= Fallback: DOM scraping ============================

def _first_locator(page, selectors):
    for s in selectors:
        loc = page.locator(s)
        if loc.count() > 0:
            return loc
    return None

def load_all_chats_dom(page):
    # Try opening sidebar if it’s collapsed
    for label in ["Open sidebar", "History", "Show sidebar"]:
        btn = page.locator(f'button[aria-label="{label}"], button:has-text("{label}")')
        if btn.count() > 0:
            try:
                btn.first.click(timeout=1000)
                page.wait_for_timeout(400)
            except Exception:
                pass

    sidebar = None
    for s in SELECTORS["sidebar_container_candidates"]:
        loc = page.locator(s)
        if loc.count() > 0:
            sidebar = loc.first
            break
    if sidebar is None:
        return []

    try:
        sidebar.click(timeout=1500)
    except Exception:
        pass

    seen = 0
    stable = 0
    for _ in range(MAX_SCROLL_PASSES):
        try:
            page.evaluate("(el)=>{el.scrollTop=el.scrollHeight;}", sidebar.element_handle())
        except Exception:
            break
        page.wait_for_timeout(int(SCROLL_PAUSE * 1000))
        total = 0
        for c in SELECTORS["chat_link_candidates"]:
            total += page.locator(c).count()
        if total == seen:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            seen = total

    links = []
    for c in SELECTORS["chat_link_candidates"]:
        loc = page.locator(c)
        n = loc.count()
        for i in range(n):
            a = loc.nth(i)
            try:
                href = a.get_attribute("href")
                title = (a.get_attribute("title") or a.inner_text(timeout=800) or "").strip()
                if href and "/c/" in href:
                    if not href.startswith("http"):
                        href = "https://chat.openai.com" + href
                    links.append({"href": href, "title": title})
            except Exception:
                continue
    # de-dup by id
    seen_ids = set()
    uniq = []
    for L in links:
        cid = L["href"].rsplit("/", 1)[-1]
        if cid not in seen_ids:
            uniq.append(L)
            seen_ids.add(cid)
    return uniq

def extract_turns_dom(page):
    # load all content by scrolling
    page.evaluate("window.scrollTo(0, 0)")
    last_h = 0
    for _ in range(60):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(250)
        h = page.evaluate("document.body.scrollHeight")
        if h == last_h:
            break
        last_h = h

    # message nodes
    turns = []
    turn_loc = _first_locator(page, SELECTORS["turn_candidates"])
    if not turn_loc:
        # fallback to any article blocks
        turn_loc = page.locator("article")
    count = turn_loc.count() if turn_loc else 0
    for i in range(count):
        node = turn_loc.nth(i)
        role = "assistant"
        try:
            role_el = node.locator(SELECTORS["role_attr"]).first
            if role_el and role_el.count() > 0:
                role = role_el.get_attribute("data-message-author-role") or role
        except Exception:
            pass

        text, html = "", ""
        content_el = _first_locator(node, SELECTORS["content_candidates"])
        try:
            if content_el and content_el.count() > 0:
                text = content_el.inner_text(timeout=1500).strip()
                html = content_el.inner_html(timeout=1500)
            else:
                text = node.inner_text(timeout=1500).strip()
                html = node.inner_html(timeout=1500)
        except Exception:
            try:
                text = node.inner_text(timeout=800).strip()
                html = node.inner_html(timeout=800)
            except Exception:
                pass
        turns.append({"role": role, "text": text, "html": html})
    return turns

# ================================ Main =======================================

def main():
    export = {
        "exported_at": int(time.time()),
        "source": "chat.openai.com (API-first exporter)",
        "conversations": []
    }

    with sync_playwright() as p:
        if USE_REAL_CHROME_PROFILE:
            user_data_dir = _expand_profile_dir()
        else:
            user_data_dir = str(Path.home() / ".chatgpt_playwright_profile")

        browser = p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            slow_mo=100
        )
        browser.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = browser.new_page()

        ensure_logged_in(page)

        # ---------- Try API path ----------
        print("Listing conversations via in-page API…")
        try:
            items = list_conversations_via_api(page)
        except Exception as e:
            items = []
        print(f"API listed {len(items)} conversations.")

        conversations = []

        if items:
            # Fetch each conversation detail via API
            for idx, it in enumerate(items, 1):
                cid = it.get("id") or it.get("conversation_id")
                title = it.get("title") or "Untitled"
                if not cid:
                    continue
                print(f"[API {idx}/{len(items)}] Fetching {cid} …")
                data = fetch_conversation_via_api(page, cid)
                if data:
                    conv = convert_api_conversation(data)
                else:
                    # if detail fetch failed, at least include header
                    conv = {"id": cid, "title": title, "messages": []}
                conv["url"] = f"https://chat.openai.com/c/{cid}"
                conversations.append(conv)

        # ---------- Fallback to DOM if API found nothing ----------
        if not conversations:
            print("Falling back to DOM scraping…")
            page.goto("https://chat.openai.com/", wait_until="domcontentloaded")
            links = load_all_chats_dom(page)
            print(f"Found {len(links)} conversation links in sidebar (DOM).")

            for idx, link in enumerate(links, 1):
                url = link["href"]
                cid = url.rsplit("/", 1)[-1]
                print(f"[DOM {idx}/{len(links)}] Opening {cid}")
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(1000)

                # Title
                title = link.get("title") or "Untitled"
                for s in SELECTORS["conversation_title_candidates"]:
                    loc = page.locator(s)
                    if loc.count() > 0:
                        try:
                            title = loc.first.inner_text(timeout=1200).strip() or title
                            break
                        except Exception:
                            pass

                # Messages
                turns = extract_turns_dom(page)

                conversations.append({
                    "id": cid,
                    "url": url,
                    "title": title,
                    "messages": turns
                })

        export["conversations"] = conversations
        Path(OUTPUT).write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ Saved {len(export['conversations'])} conversations to {OUTPUT}")

        browser.close()

if __name__ == "__main__":
    main()
