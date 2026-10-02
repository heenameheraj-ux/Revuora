"""
scraper.py - Flipkart review scraper (Selenium)

Scrapes up to 500 reviews for ANY product name.
  One product : df = get_reviews("Samsung Galaxy M35")
  Many        : scrape_products(["Samsung Galaxy M35", "Redmi Note 13 Pro"])

How reviews are found (based on the real Flipkart HTML you sent):
  - Every review is inside a <div class="fWi7J_"> wrapper.
  - Those wrappers are also used for other page sections, so we keep only the ones
    that contain the text "Verified Purchase" or "Helpful for" (= real reviews).
  - Fields (rating, title, text, date, helpful) are read from the review's visible text.
"""
import os
import random
import re
import time
from urllib.parse import quote_plus

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------
MAX_REVIEWS = 500      # reviews per product (10 per page, so about 50 pages)
MAX_PAGES = 100        # safety limit
RAW_DIR = "data/raw"   # scraped CSVs are saved here (also works as a cache)
SEARCH_URL = "https://www.flipkart.com/search?q={query}"

# First product link on the search page (any link whose URL contains /p/)
SEARCH_FIRST_PRODUCT = "a[href*='/p/']"

# One review = innermost fWi7J_ wrapper that contains review-only text.
# If Flipkart renames fWi7J_ someday, change it here (only place).
_MARK = "(.//*[contains(text(),'Verified Purchase') or contains(text(),'Helpful for')])"
REVIEW_CARD_XPATH = (
    f"//div[contains(@class,'fWi7J_')][{_MARK}]"
    f"[not(.//div[contains(@class,'fWi7J_')][{_MARK}])]"
)

# Reads all review texts in ONE call (avoids "stale element" crashes)
GET_TEXTS_JS = """
const res = document.evaluate(arguments[0], document, null,
                              XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
const out = [];
for (let i = 0; i < res.snapshotLength; i++) { out.push(res.snapshotItem(i).innerText || ''); }
return out;
"""

# Optional sort order for the review URL. Confirm the parameter name by choosing
# "Latest" on the review page and reading the new URL. Leave None for default.
SORT_PARAM = None

MONTH_RE = r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*,?\s+\d{4}\b"


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------
def make_driver(headless=False):
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1366,900")
    options.add_argument("--lang=en-IN")
    return webdriver.Chrome(options=options)   # Selenium 4.6+ manages the driver


def pause(low=2, high=5):
    time.sleep(random.uniform(low, high))


def close_login_popup(driver):
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        pass


def safe_name(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def scroll_page(driver):
    """Scroll down in steps so lazy-loaded reviews appear."""
    for _ in range(6):
        driver.execute_script("window.scrollBy(0, document.body.scrollHeight / 6);")
        time.sleep(0.4)


# ---------------------------------------------------------------
# SEARCH
# ---------------------------------------------------------------
def search_product(driver, query):
    """Search Flipkart, return (product_name, product_url) of the top result."""
    driver.get(SEARCH_URL.format(query=quote_plus(query)))
    close_login_popup(driver)
    try:
        link = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_FIRST_PRODUCT))
        )
    except TimeoutException:
        raise RuntimeError(f"No product found for '{query}' (or Flipkart blocked the request).")
    url = link.get_attribute("href")
    skip = {"add to compare", "sponsored", "ad", "assured"}
    lines = [l.strip() for l in link.text.split("\n") if l.strip() and l.strip().lower() not in skip]
    name = lines[0] if lines else query
    return name, url


def build_reviews_url(product_url, page):
    """Product URL (/p/) -> reviews page URL (/product-reviews/) with &page=N."""
    url = product_url.replace("/p/", "/product-reviews/")
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}page={page}"
    if SORT_PARAM:
        url += f"&{SORT_PARAM}"
    return url


def load_review_page(driver, url, retries=2):
    """Open a review page and wait until reviews appear."""
    for attempt in range(retries + 1):
        try:
            driver.get(url)
            close_login_popup(driver)
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(text(),'Verified Purchase') or contains(text(),'Helpful for')]")
                )
            )
            scroll_page(driver)
            return True
        except (TimeoutException, WebDriverException):
            if attempt < retries:
                pause(4, 8)
    return False


# ---------------------------------------------------------------
# READ ONE REVIEW
# ---------------------------------------------------------------
def parse_count(s):
    """'38' -> 38, '1K' -> 1000, '1.2K' -> 1200 (Flipkart rounds big numbers)."""
    m = re.search(r"([\d.,]+)\s*([Kk])?", s or "")
    if not m:
        return ""
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return ""
    return int(n * 1000) if m.group(2) else int(n)


def extract_review_fields(card_text):
    """
    card_text is the visible text of one review, roughly:
        5.0 / Simply awesome / Review for: Color ... / <review text> /
        <Name, City> / Helpful for 38 / 6 / Verified Purchase / Nov, 2024
    """
    lines = [l.strip() for l in card_text.split("\n") if l.strip()]
    lines = [l for l in lines if l not in {"•", "·", "|", "more", "...more", "Read more"}]
    if not lines:
        return None

    rating = lines[0] if re.fullmatch(r"\d(\.\d)?", lines[0]) else ""
    title = lines[1] if len(lines) > 1 else ""

    start = 2
    if start < len(lines) and lines[start].startswith("Review for"):
        start += 1

    # where the "Helpful for N" line is (fallback: Verified Purchase line)
    idx = next((i for i, l in enumerate(lines) if l.startswith("Helpful")), None)
    if idx is None:
        idx = next((i for i, l in enumerate(lines) if "Verified Purchase" in l), len(lines))

    body = lines[start:idx]
    if body:
        last = body.pop()                       # last line before Helpful = reviewer name/city
        # Name and city can be split into two lines, e.g. "Vaibhav Patel" + ", Bhopal"
        # or "Sandeep Kumar," + "Ghumarwin". In both cases remove the name line too.
        if body and (last.startswith(",") or
                     (body[-1].endswith(",") and len(body[-1].split()) <= 4)):
            body.pop()
    text = " ".join(body).strip()

    joined = " ".join(lines)
    d = re.search(MONTH_RE, joined) or re.search(r"\d+\s+(?:day|week|month|year)s?\s+ago", joined)
    date = d.group(0) if d else ""

    helpful_line = lines[idx] if idx < len(lines) and lines[idx].startswith("Helpful") else ""

    return {
        "rating": rating,
        "title": title,
        "text": text,
        "date": date,
        "helpful_votes": parse_count(helpful_line),
    }


# ---------------------------------------------------------------
# SCRAPE ONE PRODUCT
# ---------------------------------------------------------------
def scrape_reviews(driver, product_name, product_url,
                   max_reviews=MAX_REVIEWS, max_pages=MAX_PAGES,
                   progress=None, debug=True):
    rows, seen = [], set()
    empty_pages = 0

    for page in range(1, max_pages + 1):
        if not load_review_page(driver, build_reviews_url(product_url, page)):
            print(f"  Page {page}: could not load, stopping.")
            break

        card_texts = driver.execute_script(GET_TEXTS_JS, REVIEW_CARD_XPATH)

        if debug and page == 1:
            print(f"\n  Review blocks found on page 1: {len(card_texts)} (expected about 10)")
            if card_texts:
                print("--- RAW TEXT of first review block ---")
                print(card_texts[0])
                print("--- end ---\n")
            debug = False

        added = 0
        for t in card_texts:
            if t.count("Verified Purchase") > 1:      # a wrapper holding many reviews, skip
                continue
            fields = extract_review_fields(t)
            if not fields or not fields["text"]:
                continue
            key = (fields["title"] + "|" + fields["text"]).lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append({"product_name": product_name, **fields})
            added += 1
            if len(rows) >= max_reviews:
                break

        print(f"  Page {page}: +{added} (total {len(rows)}/{max_reviews})")
        if progress:
            progress(f"Scraping reviews: {len(rows)}/{max_reviews}")

        if len(rows) >= max_reviews:
            break
        empty_pages = empty_pages + 1 if added == 0 else 0
        if empty_pages >= 2:
            print("  No new reviews on 2 pages in a row, stopping.")
            break
        pause()

    return pd.DataFrame(rows)


def get_reviews(query, max_reviews=MAX_REVIEWS, headless=False,
                force=False, driver=None, progress=None):
    """
    Search + scrape one product. Saves data/raw/<product>.csv.
    If that CSV already exists it is loaded instead (cache), unless force=True.
    """
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, safe_name(query) + ".csv")

    if os.path.exists(path) and not force:
        print(f"Loaded cached reviews for '{query}' from {path}")
        return pd.read_csv(path)

    own_driver = driver is None
    driver = driver or make_driver(headless)
    try:
        if progress:
            progress("Searching Flipkart...")
        name, url = search_product(driver, query)
        print(f"Found: {name}\n  {url}")
        df = scrape_reviews(driver, name, url, max_reviews, progress=progress)
        if df.empty:
            raise RuntimeError("0 reviews scraped. Send the printed output to get the selectors fixed.")
        df["product_url"] = url
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"Saved {len(df)} reviews to {path}")
        return df
    finally:
        if own_driver:
            driver.quit()


def scrape_products(queries, max_reviews=MAX_REVIEWS, headless=False, force=False):
    """Scrape several products one by one. A failed product is skipped."""
    results = {}
    driver = make_driver(headless)
    try:
        for i, q in enumerate(queries, 1):
            print(f"\n[{i}/{len(queries)}] {q}")
            try:
                results[q] = get_reviews(q, max_reviews, driver=driver, force=force)
            except Exception as e:
                print(f"  FAILED: {e}")
            if i < len(queries):
                pause(10, 20)
    finally:
        driver.quit()
    return results


def dump_review_page_html(query=
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          
                          "Samsung Galaxy M35"):
    """Debug helper: saves the raw HTML of page 1 of the reviews to review_page_dump.html."""
    driver = make_driver(headless=False)
    try:
        name, url = search_product(driver, query)
        load_review_page(driver, build_reviews_url(url, 1))
        with open("review_page_dump.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print("Saved review_page_dump.html")
    finally:
        driver.quit()


if __name__ == "__main__":
    # Type any product name when you run this file. Works for any product,
    # not just the ones used while testing.
    query = input("Enter the product name to scrape: ").strip()
    if not query:
        print("You didn't type anything. Run the script again and enter a product name.")
    else:
        df = get_reviews(query, max_reviews=500)
        print(df.head(10).to_string())
        print(len(df), "reviews scraped for:", query)

    # To scrape several products in one run instead of typing one at a time,
    # comment out the input() block above and use this instead:
    # products = ["Samsung Galaxy M35", "Redmi Note 13 Pro", "boAt Airdopes 141"]
    # scrape_products(products)