import streamlit as st
from playwright.sync_api import sync_playwright
import pandas as pd
import time
import json
import re
from multiprocessing import Pool
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import datetime
import os
import json


# ─────────────────────────────────────────────────────────────
# GOOGLE SHEETS SETUP
# ─────────────────────────────────────────────────────────────
SHEET_ID = "1auy1Xas3wTACvJqh3X7wtbDlyYG0dLE9qGCg8EDKqkg"
SUBSHEET_NAME = "LiveClasses"
SERVICE_ACCOUNT_FILE = "high-electron-430706-h2-87e8e728dc0f.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Sheet column positions (1-indexed)
COL_VIDEO_LINK        = 9   # I
COL_PRODUCT_TAG       = 10   # J
COL_PRODUCT_TITLE     = 11  # K
COL_PRICE             = 12  # L
COL_PLATFORM          = 13  # M


def get_sheet():
    google_creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    
    if google_creds_json:
        # Load from environment variable (Render)
        creds_dict = json.loads(google_creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        # Fallback to local file for desktop testing
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).worksheet(SUBSHEET_NAME)
    return sheet


def fetch_urls_from_sheet():
    """Sheet se un rows ki video links fetch karo jahan Product_Tag_Status empty hai ya 'NO'/'ERROR' hai."""
    sheet = get_sheet()
    rows = sheet.get_all_values()
    total_rows = max(0, len(rows) - 1)  # -1 because of header
    
    urls_with_rows = []
    already_done_count = 0
    for i, row in enumerate(rows[1:], start=2):  # row 1 = header, skip
        try:
            video_link = row[COL_VIDEO_LINK - 1].strip() if len(row) >= COL_VIDEO_LINK else ""
            already_done = row[COL_PRODUCT_TAG - 1].strip() if len(row) >= COL_PRODUCT_TAG else ""
            
            if video_link:
                # Re-check if it's empty, explicitly marked as "NO", or marked as "ERROR"
                if not already_done or already_done.upper() in ("NO", "ERROR"):
                    urls_with_rows.append((i, video_link))
                else:
                    already_done_count += 1
        except Exception:
            continue
    
    return urls_with_rows, already_done_count, total_rows


def log_cron_run(start_time, end_time, total_rows, updated_with_product, already_have_product, have_no_product):
    """Log the cron job statistics to the CronLog subsheet."""
    try:
        google_creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if google_creds_json:
            creds_dict = json.loads(google_creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
            
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        try:
            log_sheet = spreadsheet.worksheet("CronLog")
        except gspread.exceptions.WorksheetNotFound:
            log_sheet = spreadsheet.add_worksheet(title="CronLog", rows="1000", cols="6")
            log_sheet.append_row(["Cron Start at", "Cron Stop at", "Total Rows", "Rows Updated with Product", "Rows Already have the product", "Rows have no Product"])

        start_time_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        end_time_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

        log_sheet.append_row([start_time_str, end_time_str, total_rows, updated_with_product, already_have_product, have_no_product])
    except Exception as e:
        print(f"[CRON LOG ERROR] Could not write to CronLog: {e}")


def update_sheet_row(row_num, product_tag_status, product_title, price, platform):
    """Ek row ke product tag columns update karo."""
    sheet = get_sheet()
    sheet.update_cell(row_num, COL_PRODUCT_TAG, product_tag_status)
    sheet.update_cell(row_num, COL_PRODUCT_TITLE, product_title)
    sheet.update_cell(row_num, COL_PRICE, price)
    sheet.update_cell(row_num, COL_PLATFORM, platform)


# ─────────────────────────────────────────────────────────────
# TOP-LEVEL WORKER — multiprocessing ke liye
# ─────────────────────────────────────────────────────────────
def _scrape_worker(args):
    video_url, cookies = args
    local_results = []
    local_logs = []
    tag = f"[...{video_url[-12:]}]"

    def log(msg):
        local_logs.append(f"[{time.strftime('%H:%M:%S')}] {tag} {msg}")

    def platform_for(text, link):
        t = (text or "").lower()
        l = (link or "").lower()
        return "Flipkart" if ("flipkart" in t or "flipkart" in l) else "Testbook"

    def card_link(el):
        try:
            return el.evaluate(
                "(node) => { const a = node.querySelector('a[href]'); return a ? a.href : ''; }"
            ) or ""
        except Exception:
            return ""

    def extract_title(text):
        lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
        skip = {"SHOP", "BUY NOW", "VIEW PRODUCTS", "VIEW PRODUCT", "SHOP NOW"}
        candidates = [
            l for l in lines
            if "₹" not in l
            and l.strip().upper() not in skip
            and not l.strip().upper().startswith("LEARN MORE")
            and len(l) >= 8
        ]
        return max(candidates, key=len) if candidates else "Unknown"

    def extract_price(text):
        m = re.search(r'(₹\s*[\d,.]+(?:\.\d+)?)', text or "")
        if m:
            return m.group(1)
        m2 = re.search(r'([$€£]\s?[\d,.]+)', text or "")
        return m2.group(1) if m2 else "N/A"

    def add_row(video_type, title, price, link, card_text):
        platform = platform_for(card_text, link)
        row = {
            "Source URL": video_url, "Video_Type": video_type,
            "Product_Tag_Status": "YES", "Title": title,
            "Price": price, "Platform": platform, "Link": link,
        }
        key = (video_url, title, price)
        if not any((r["Source URL"], r["Title"], r["Price"]) == key for r in local_results):
            local_results.append(row)

    def find_card_from_price(price_locator):
        try:
            ph = price_locator.element_handle(timeout=2000)
        except Exception:
            return None
        try:
            jh = ph.evaluate_handle("""
                (el) => {
                  function hasShop(node) {
                    return Array.from(node.querySelectorAll('button,a'))
                      .some(n => (n.innerText||'').trim().toUpperCase()==='SHOP');
                  }
                  let cur = el;
                  for (let i=0; i<12 && cur; i++) {
                    const txt = (cur.innerText||'').toUpperCase();
                    if (txt.includes('₹') && (txt.includes('SHOP') || hasShop(cur))) return cur;
                    if (hasShop(cur)) return cur;
                    cur = cur.parentElement;
                  }
                  return null;
                }
            """)
            return jh.as_element()
        except Exception:
            return None

    def scrape_cards(cards, video_type, limit=30):
        try:
            count = cards.count()
        except Exception:
            count = 0
        for i in range(min(count, limit)):
            try:
                item = cards.nth(i)
                if not item.is_visible():
                    continue
                txt = item.inner_text()
                price = extract_price(txt)
                if price == "N/A":
                    continue
                title = extract_title(txt)
                link = ""
                try:
                    el = item.element_handle(timeout=500)
                    if el:
                        link = card_link(el)
                except Exception:
                    pass
                add_row(video_type, title, price, link, txt)
            except Exception:
                continue

    def detect_type(page):
        page.wait_for_timeout(2000)
        try:
            if page.query_selector(
                "ytd-reel-video-renderer, ytd-shorts, ytd-shorts-player-renderer, "
                "ytd-reel-player-overlay-renderer, ytd-reel-item-renderer"
            ):
                return "Shorts"
            if page.query_selector("ytd-watch-flexy"):
                return "Normal"
        except Exception:
            pass
        try:
            box = page.locator("video").first.bounding_box()
            if box and box["width"] and box["height"]:
                return "Shorts" if box["height"] / max(box["width"], 1) >= 0.9 else "Normal"
        except Exception:
            pass
        try:
            is_shorts = page.evaluate("""
                () => {
                    const c = document.querySelector('link[rel="canonical"]');
                    if (c && c.href.includes('/shorts/')) return true;
                    const og = document.querySelector('meta[property="og:url"]');
                    if (og && og.content.includes('/shorts/')) return true;
                    const p = document.querySelector('#movie_player, ytd-player');
                    if (p) { const r = p.getBoundingClientRect(); return r.height >= r.width * 0.9; }
                    return false;
                }
            """)
            if is_shorts:
                return "Shorts"
        except Exception:
            pass
        return "Normal"

    def do_shorts(page):
        try:
            page.locator("video").first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        btn = None
        for sel in ["button:has-text('View product')", "button:has-text('View products')", "text=/View\\s+product/i"]:
            try:
                c = page.locator(sel)
                if c.count() > 0:
                    btn = c.first
                    log("'View Product' button found")
                    break
            except Exception:
                continue
        if btn is None:
            log("No 'View Product' button → NO")
            return
        try:
            btn.click(timeout=3000, force=True)
            page.wait_for_timeout(1800)
        except Exception as e:
            log(f"Click failed: {e}")

        panel = page.locator("ytd-engagement-panel-section-list-renderer[target-id='engagement-panel-shopping']")
        if panel.count() > 0 and panel.first.is_visible():
            cards = panel.first.locator("ytd-vertical-product-card-renderer, ytd-merch-item-renderer, ytd-grid-merch-item-renderer")
            scrape_cards(cards, "Shorts")
            try:
                panel.first.evaluate("(el) => el.scrollTop += 500")
                page.wait_for_timeout(800)
                scrape_cards(cards, "Shorts")
            except Exception:
                pass
            if any(r["Product_Tag_Status"] == "YES" for r in local_results):
                return

        scrape_cards(page.locator("ytd-vertical-product-card-renderer, ytd-merch-item-renderer, ytd-grid-merch-item-renderer"), "Shorts")

        if not any(r["Product_Tag_Status"] == "YES" for r in local_results):
            rp = page.locator("text=/₹\\s*[0-9]/")
            try:
                rc = rp.count()
            except Exception:
                rc = 0
            for idx in range(min(rc, 6)):
                try:
                    pn = rp.nth(idx)
                    if not pn.is_visible():
                        continue
                    el = find_card_from_price(pn)
                    if not el:
                        continue
                    txt = el.inner_text()
                    price = extract_price(txt)
                    if price == "N/A":
                        continue
                    add_row("Shorts", extract_title(txt), price, card_link(el), txt)
                except Exception:
                    continue

    def do_normal(page):
        try:
            page.evaluate("""
                () => {
                    const t = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) * 0.3;
                    window.scrollTo({ top: t, behavior: 'smooth' });
                }
            """)
            page.wait_for_timeout(2500)
        except Exception:
            pass

        panel = page.locator("ytd-engagement-panel-section-list-renderer[target-id='engagement-panel-shopping']")
        if panel.count() > 0 and panel.first.is_visible():
            scrape_cards(panel.first.locator("ytd-vertical-product-card-renderer, ytd-merch-item-renderer, ytd-grid-merch-item-renderer"), "Normal")

        scrape_cards(page.locator("ytd-merch-item-renderer, ytd-vertical-product-card-renderer, ytd-grid-merch-item-renderer"), "Normal")

        if any(r["Product_Tag_Status"] == "YES" for r in local_results):
            return

        rp = page.locator("text=/₹\\s*[0-9]/")
        try:
            rc = rp.count()
        except Exception:
            rc = 0
        for idx in range(min(rc, 8)):
            try:
                pn = rp.nth(idx)
                if not pn.is_visible():
                    continue
                pn.scroll_into_view_if_needed()
                el = find_card_from_price(pn)
                if not el:
                    continue
                try:
                    el.click(timeout=2000, force=True)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                txt = el.inner_text()
                price = extract_price(txt)
                if price == "N/A":
                    continue
                add_row("Normal", extract_title(txt), price, card_link(el), txt)
            except Exception:
                continue

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        if cookies:
            try:
                cleaned = []
                for c in cookies:
                    ck = c.copy()
                    if ck.get('sameSite') not in ['Strict', 'Lax', 'None']:
                        ck.pop('sameSite', None)
                    for k in ('hostOnly', 'session', 'storeId'):
                        ck.pop(k, None)
                    cleaned.append(ck)
                context.add_cookies(cleaned)
            except Exception as e:
                log(f"Cookie error: {e}")

        max_retries = 2
        for attempt in range(1, max_retries + 1):
            local_results.clear()
            page = context.new_page()
            page.route("**/*", lambda route: route.abort()
                if route.request.resource_type in ["image", "media", "font"]
                else route.continue_()
            )

            try:
                log(f"Navigating (Attempt {attempt}/{max_retries})...")
                page.goto(video_url, wait_until="networkidle", timeout=60000)
                
                # Dynamic wait: wait for the engagement panel to attach to DOM, or fallback
                try:
                    page.wait_for_selector("ytd-engagement-panel-section-list-renderer, ytd-merch-shelf-renderer", timeout=5000)
                except Exception:
                    # Fallback wait if it truly is a page without shopping
                    page.wait_for_timeout(3000)

                vtype = detect_type(page)
                log(f"Type: {vtype}")

                if vtype == "Shorts":
                    do_shorts(page)
                else:
                    do_normal(page)

                if local_results:
                    log(f"→ {len(local_results)} product(s) ✓")
                    page.close()
                    break # Success, exit retry loop
                else:
                    log("→ NO products found this attempt")
                    if attempt == max_retries:
                        local_results.append({
                            "Source URL": video_url, "Video_Type": vtype,
                            "Product_Tag_Status": "NO",
                            "Title": "", "Price": "", "Platform": "", "Link": "",
                        })

            except Exception as e:
                log(f"Error on attempt {attempt}: {e}")
                if attempt == max_retries:
                    local_results.append({
                        "Source URL": video_url, "Video_Type": "Unknown",
                        "Product_Tag_Status": "ERROR",
                        "Title": str(e), "Price": "", "Platform": "", "Link": "",
                    })
            finally:
                page.close()

        context.close()
        browser.close()

    return local_results, local_logs


# ─────────────────────────────────────────────────────────────
# MANUAL SCRAPER (UI se trigger)
# ─────────────────────────────────────────────────────────────
def scrape_youtube_products(video_urls, log_placeholder, cookies=None, max_workers=5):
    all_products = []
    all_logs = []
    clean_urls = [u.strip() for u in video_urls if u.strip()]

    def refresh_log():
        log_placeholder.code("\n".join(all_logs[-80:]), language="text")

    all_logs.append(f"[{time.strftime('%H:%M:%S')}] Starting: {len(clean_urls)} URLs | {max_workers} workers")
    refresh_log()

    args = [(url, cookies) for url in clean_urls]
    with Pool(processes=max_workers) as pool:
        for results, logs in pool.imap_unordered(_scrape_worker, args):
            all_products.extend(results)
            all_logs.extend(logs)
            refresh_log()

    all_logs.append(f"[{time.strftime('%H:%M:%S')}] Done. Total rows: {len(all_products)}")
    refresh_log()
    return all_products


# ─────────────────────────────────────────────────────────────
# CRON JOB — daily 11 AM automatic Google Sheet run
# ─────────────────────────────────────────────────────────────
def run_cron_job(progress_bar=None, log_container=None):
    """Sheet se URLs fetch karo, scrape karo, results wapas sheet mein likho."""
    start_time = datetime.datetime.now()
    start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[CRON] Started at {start_str}")
    
    # Save running state to file for UI to read later
    try:
        with open("cron_status.txt", "w") as f:
            f.write(f"Started collecting URLs at {start_str}...")
    except Exception:
        pass

    try:
        urls_with_rows, already_done_count, total_rows = fetch_urls_from_sheet()
    except Exception as e:
        print(f"[CRON] Sheet fetch error: {e}")
        return

    if not urls_with_rows:
        msg = f"Completed at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (0 pending URLs)"
        print(f"[CRON] {msg}")
        try:
            with open("cron_status.txt", "w") as f:
                f.write(msg)
        except Exception:
            pass
        
        end_time = datetime.datetime.now()
        log_cron_run(start_time, end_time, total_rows, 0, already_done_count, 0)
        return

    msg = f"Started processing {len(urls_with_rows)} URLs at {start_str}"
    print(f"[CRON] {msg}")
    try:
        with open("cron_status.txt", "w") as f:
            f.write(msg)
    except Exception:
        pass

    urls_only = [url for _, url in urls_with_rows]
    row_map = {url: row_num for row_num, url in urls_with_rows}

    args = [(url, None) for url in urls_only]

    # Scrape karo (Sequentially on Render to avoid OOM crashes)
    total_to_process = len(args)
    updated_with_product = 0
    have_no_product = 0
    
    # UI Live Logs Tracker
    live_logs = []
    
    for idx, arg in enumerate(args):
        url = arg[0]
        row_num = row_map[url]
        try:
            results, logs = _scrape_worker(arg)
            for log_line in logs:
                print(f"[CRON] {log_line}")
                if log_container:
                    live_logs.append(log_line)
                    # Show only last 20 lines to prevent UI freezing
                    log_container.code("\n".join(live_logs[-20:]), language="text")

            # --- INSTANT GOOGLE SHEET UPDATE ---
            if not results:
                update_sheet_row(row_num, "NO", "", "", "")
                have_no_product += 1
            else:
                first = results[0]
                status = first["Product_Tag_Status"]
                update_sheet_row(
                    row_num,
                    status,
                    first.get("Title", ""),
                    first.get("Price", ""),
                    first.get("Platform", ""),
                )
                if status == "YES":
                    updated_with_product += 1
                else:
                    have_no_product += 1
            print(f"[CRON] Updated row {row_num} for {url[-30:]}")

        except Exception as e:
            err_msg = f"[CRON] Error processing {url}: {e}"
            print(err_msg)
            if log_container:
                live_logs.append(err_msg)
                log_container.code("\n".join(live_logs[-20:]), language="text")
                
        # Update progress bar
        if progress_bar:
            progress = (idx + 1) / total_to_process
            progress_bar.progress(progress)
            
        # Update text file to act as a heartbeat
        try:
            with open("cron_status.txt", "w") as f:
                f.write(f"Running ({idx + 1}/{total_to_process} URLs processed) - Started {start_str}")
        except Exception:
            pass

    end_time = datetime.datetime.now()
    log_cron_run(start_time, end_time, total_rows, updated_with_product, already_done_count, have_no_product)
    
    end_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[CRON] Finished at {end_str}")
    
    try:
        with open("cron_status.txt", "w") as f:
            f.write(f"Completed processing {len(args)} URLs at {end_str}")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# SCHEDULER INIT — app start hone pe background mein chal ta hai
# ─────────────────────────────────────────────────────────────
if "scheduler_started" not in st.session_state:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_cron_job,
        trigger="cron",
        hour="6,18",
        minute=0,
        timezone="Asia/Kolkata",   # IST
    )
    scheduler.start()
    st.session_state.scheduler_started = True
    print("[SCHEDULER] Cron job scheduled for 6:00 AM and 6:00 PM IST daily")


# ── STREAMLIT UI ──
st.set_page_config(page_title="YouTube Product Scraper", page_icon="🛍️", layout="wide")
st.title("YouTube Product Scraper 🛍️")
st.markdown("Extract tagged product metadata from YouTube videos.")

# ── Cron Status + Manual Trigger ──
st.subheader("🕐 Cron Job")

# Local Status Persistency Check
if os.path.exists("cron_status.txt"):
    try:
        with open("cron_status.txt", "r") as f:
            status_text = f.read().strip()
        if status_text:
            st.info(f"**Last Status:** {status_text}")
    except Exception:
        pass

col_a, col_b = st.columns([3, 1])
with col_a:
    st.markdown("Auto-runs daily at **6:00 AM** and **6:00 PM IST** — fetches pending URLs from Google Sheet and updates results.")
with col_b:
    if st.button("▶ Run Now (Manual)"):
        pb = st.progress(0)
        lc = st.empty()
        with st.spinner("Running cron job manually..."):
            run_cron_job(progress_bar=pb, log_container=lc)
        st.success("Cron job complete! Check your Google Sheet.")
        st.rerun()

st.divider()

# ── Manual URL Scraper ──
st.subheader("🔍 Manual Scrape")
urls_input = st.text_area(
    "Enter YouTube Video URLs (one per line)",
    placeholder="https://www.youtube.com/watch?v=...",
    height=150
)

with st.expander("Advanced Settings"):
    cookies_json = st.text_area("Paste Cookies (JSON format)", height=100)
    max_workers = st.slider(
        "Parallel Workers (zyada = fast, RAM zyada lagegi)",
        min_value=1, max_value=10, value=2
    )

if st.button("Start Scraping"):
    urls = [u.strip() for u in urls_input.split('\n') if u.strip()]
    cookies = None
    if cookies_json:
        try:
            cookies = json.loads(cookies_json)
        except Exception as e:
            st.error(f"Invalid JSON: {e}")
            st.stop()

    if not urls:
        st.warning("Please enter at least one URL.")
    else:
        st.subheader("Live Log")
        log_spot = st.empty()
        with st.spinner(f"Scraping {len(urls)} URLs with {max_workers} workers..."):
            data = scrape_youtube_products(urls, log_spot, cookies=cookies, max_workers=max_workers)

        if data:
            st.success(f"Done! Found {len(data)} row(s).")
            df = pd.DataFrame(data)
            st.dataframe(df, use_container_width=True)
            csv = df.to_csv(index=False).encode('utf-8')
            st.download_button("Download CSV", csv, "youtube_products.csv", "text/csv")
        else:
            st.warning("No products extracted.")

st.markdown("---")
st.caption("Cron: 11 AM IST daily | Manual scrape also available above.")


