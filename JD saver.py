import os
import re
import uuid
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from dotenv import load_dotenv
from dateutil.relativedelta import relativedelta
from dateutil import parser as date_parser

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# =========================
# Config & constants
# =========================
load_dotenv()

APPLIED_URL = "https://www.seek.co.nz/my-activity/applied-jobs"
CHROME_BINARY = os.getenv("CHROME_BINARY", "")
CHROME_DRIVER_PATH = os.getenv("CHROMEDRIVER", "chromedriver")
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR")
CHROME_PROFILE_DIR = os.getenv("CHROME_PROFILE_DIR", "Default")
DB_PATH = os.getenv("DB_PATH", "seek_jobs_demo.db")

TITLE_BLOCK_XPATH = "//span[@role='button' and .//span[text()='Job Title ']]"
LIST_READY_CSS = "#tabs-saved-applied_2_panel > div:nth-child(2)"
JD_TITLE_CSS = "h1[data-automation='job-detail-title']"


# =========================
# Data model
# =========================
@dataclass
class JobRecord:
    id: str
    job_url: str
    job_title: str
    company: str
    address: str
    field: str
    job_type: str
    posted_date: str
    applied_date: str
    jd: str
    created_at: str


# =========================
# Database layer
# =========================
class JobDB:
    """SQLite wrapper with an upsert using job_url as unique key."""
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.cur = self.conn.cursor()
        self._ensure_schema()

    def _ensure_schema(self):
        self.cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            job_url TEXT UNIQUE,
            job_title TEXT,
            company TEXT,
            address TEXT,
            field TEXT,
            job_type TEXT,
            posted_date TEXT,
            applied_date TEXT,
            jd TEXT,
            created_at TEXT
        )
        """)
        self.conn.commit()

    def upsert(self, rec: JobRecord):
        """Insert or update by job_url."""
        self.cur.execute("SELECT id FROM jobs WHERE job_url = ?", (rec.job_url,))
        row = self.cur.fetchone()
        if row:
            existing_id = row[0]
            self.cur.execute("""
                UPDATE jobs
                SET job_title=?, company=?, address=?, field=?, job_type=?,
                    posted_date=?, applied_date=?, jd=?, created_at=?
                WHERE id=?
            """, (rec.job_title, rec.company, rec.address, rec.field, rec.job_type,
                  rec.posted_date, rec.applied_date, rec.jd, rec.created_at, existing_id))
            action = "Updated"
        else:
            self.cur.execute("""
                INSERT INTO jobs
                (id, job_url, job_title, company, address, field, job_type,
                 posted_date, applied_date, jd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rec.id, rec.job_url, rec.job_title, rec.company, rec.address,
                  rec.field, rec.job_type, rec.posted_date, rec.applied_date,
                  rec.jd, rec.created_at))
            action = "Inserted"
        self.conn.commit()
        print(f"[{action}] {rec.job_title} — {rec.company}")

    def close(self):
        self.conn.close()


# =========================
# Scraper layer
# =========================
class SeekScraper:
    """Encapsulates Selenium interactions with Seek."""
    def __init__(self, driver: webdriver.Chrome, wait: WebDriverWait):
        self.driver = driver
        self.wait = wait

    @staticmethod
    def create_driver_from_env() -> "SeekScraper":
        """Factory to build driver reusing local Chrome profile (already logged in)."""
        chrome_opts = Options()
        chrome_opts.add_argument("--start-maximized")
        chrome_opts.add_argument(f"--user-data-dir={CHROME_USER_DATA_DIR}")
        chrome_opts.add_argument(f"--profile-directory={CHROME_PROFILE_DIR}")
        if CHROME_BINARY:
            chrome_opts.binary_location = CHROME_BINARY

        service = Service(CHROME_DRIVER_PATH)
        driver = webdriver.Chrome(service=service, options=chrome_opts)
        wait = WebDriverWait(driver, 15)
        return SeekScraper(driver, wait)

    # ---------- generic utilities ----------
    def wait_present(self, locator):
        return self.wait.until(EC.presence_of_element_located(locator))

    def safe_text(self, by, selector) -> str:
        try:
            return self.driver.find_element(by, selector).text.strip()
        except Exception:
            return ""

    def scroll_into_view(self, el):
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)

    def close_drawer_if_open(self):
        """Try to close the right-side drawer after scraping."""
        try:
            close_btn = self.driver.find_element(
                By.XPATH, "//button[@aria-label='Close' or @aria-label='Close dialog']"
            )
            self.driver.execute_script("arguments[0].click();", close_btn)
            time.sleep(0.2)
        except Exception:
            # best-effort ESC
            try:
                from selenium.webdriver.common.keys import Keys
                self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
                time.sleep(0.1)
            except Exception:
                pass

    # ---------- page-level actions ----------
    def open_applied_list(self):
        self.driver.get(APPLIED_URL)
        self.wait_present((By.CSS_SELECTOR, LIST_READY_CSS))
        time.sleep(1)

    def lazy_scroll(self):
        last_height = 0
        for _ in range(5):
            self.driver.execute_script("window.scrollBy(0, 1200);")
            time.sleep(0.8)
            new_height = self.driver.execute_script("return document.body.scrollHeight;")
            if new_height == last_height:
                break
            last_height = new_height

    def find_title_blocks(self):
        return self.driver.find_elements(By.XPATH, TITLE_BLOCK_XPATH)

    def next_page(self) -> bool:
        """Click 'Next' and wait for list to change; return False on last page."""
        try:
            before_text = ""
            blocks = self.find_title_blocks()
            if blocks:
                before_text = blocks[0].text

            next_btn = self.driver.find_element(By.XPATH, "//span[.='Next']/parent::span")
            self.scroll_into_view(next_btn)
            time.sleep(0.2)
            self.driver.execute_script("arguments[0].click();", next_btn)
        except Exception:
            return False

        try:
            WebDriverWait(self.driver, 8).until(
                lambda d: (self.find_title_blocks()
                           and self.find_title_blocks()[0].text != before_text)
            )
            return True
        except Exception:
            return False

    # ---------- scraping routines ----------
    def get_view_job_url_from_drawer(self) -> str:
        link = self.wait_present(
            (By.XPATH, "//a[contains(@href, 'job/') and contains(text(),'View job')]")
        )
        href = link.get_attribute("href")
        return f"https://www.seek.co.nz{href}" if href.startswith("/") else href

    def open_drawer_for(self, el):
        self.scroll_into_view(el)
        time.sleep(0.3)
        # JS click to avoid interception
        self.driver.execute_script("arguments[0].click();", el)
        # ensure drawer loaded (by View job)
        self.wait_present((By.XPATH, "//a[contains(@href, 'job/') and contains(text(),'View job')]"))

    def scrape_jd_page(self, job_url: str) -> JobRecord:
        """Open JD in a new tab, scrape fields, close the tab, return JobRecord."""
        main = self.driver.current_window_handle
        self.driver.execute_script("window.open(arguments[0], '_blank');", job_url)
        self.driver.switch_to.window(self.driver.window_handles[-1])

        try:
            self.wait_present((By.CSS_SELECTOR, JD_TITLE_CSS))
        except Exception:
            print(f"[Warn] JD page didn't load properly: {job_url}")

        # core fields
        job_title = self.safe_text(By.CSS_SELECTOR, JD_TITLE_CSS)
        company = self.safe_text(By.CSS_SELECTOR, "span[data-automation='advertiser-name']")
        address = self.safe_text(By.CSS_SELECTOR, "span[data-automation='job-detail-location']")
        field = self.safe_text(By.CSS_SELECTOR, "span[data-automation='job-detail-classifications']")
        job_type = self.safe_text(By.CSS_SELECTOR, "span[data-automation='job-detail-work-type']")
        jd_text = self.safe_text(By.CSS_SELECTOR, "div[data-automation='jobAdDetails']")

        # Posted ... ago -> absolute date
        posted_date = ""
        try:
            posted_text = self.driver.find_element(
                By.XPATH, "//span[starts-with(text(), 'Posted ')]"
            ).text
            m = re.search(r"Posted\s+(\d+)([dwmy])\s+ago", posted_text)
            if m:
                num = int(m.group(1)); unit = m.group(2)
                today = datetime.today()
                if unit == "d":
                    date_obj = today - timedelta(days=num)
                elif unit == "w":
                    date_obj = today - timedelta(weeks=num)
                elif unit == "m":
                    date_obj = today - relativedelta(months=num)
                elif unit == "y":
                    date_obj = today - relativedelta(years=num)
                else:
                    date_obj = today
                posted_date = date_obj.strftime("%Y-%m-%d")
        except Exception:
            posted_date = ""

        # You applied on ...
        applied_date = ""
        try:
            applied_text = self.driver.find_element(
                By.XPATH, "//span[starts-with(text(), 'You applied on')]"
            ).text
            m = re.search(r"You applied on (.+)", applied_text)
            if m:
                dt = date_parser.parse(m.group(1))
                applied_date = dt.strftime("%Y-%m-%d")
        except Exception:
            pass

        # close JD tab, back to list
        self.driver.close()
        self.driver.switch_to.window(main)

        # pack record
        return JobRecord(
            id=str(uuid.uuid4()),
            job_url=job_url,
            job_title=job_title,
            company=company,
            address=address,
            field=field,
            job_type=job_type,
            posted_date=posted_date,
            applied_date=applied_date,
            jd=jd_text,
            created_at=datetime.utcnow().isoformat()
        )

    # ---------- high-level orchestration ----------
    def scrape_all_pages(self, db: JobDB):
        self.open_applied_list()
        page = 1
        while True:
            self.lazy_scroll()
            blocks = self.find_title_blocks()
            print(f"[Page {page}] Found {len(blocks)} job entries.")

            for i in range(len(blocks)):
                # re-find each iteration to avoid stale after drawer open/close
                blocks = self.find_title_blocks()
                if i >= len(blocks):
                    break
                el = blocks[i]

                title_text = el.text.replace("Job Title", "").strip()
                print(f"[{i+1}] {title_text}")

                try:
                    self.open_drawer_for(el)
                except Exception as e:
                    print(f"[Click failed] {title_text}: {e}")
                    continue

                # get JD url from drawer
                try:
                    job_url = self.get_view_job_url_from_drawer()
                    print(f"[View job] {job_url}")
                except Exception as e:
                    print(f"[Skip] No 'View job' link for: {title_text} — {e}")
                    self.close_drawer_if_open()
                    continue

                # scrape JD page & upsert
                rec = self.scrape_jd_page(job_url)
                db.upsert(rec)

                # close drawer and continue
                self.close_drawer_if_open()
                time.sleep(0.2)

            # pagination
            if self.next_page():
                page += 1
                self.wait_present((By.CSS_SELECTOR, LIST_READY_CSS))
                time.sleep(0.6)
            else:
                print("[Done] No more pages.")
                break

    def quit(self):
        self.driver.quit()


# =========================
# Entrypoint
# =========================
def main():
    db = JobDB(DB_PATH)
    scraper = SeekScraper.create_driver_from_env()
    try:
        scraper.scrape_all_pages(db)
    finally:
        scraper.quit()
        db.close()
        print("All done.")

if __name__ == "__main__":
    main()
