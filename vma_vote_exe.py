# vma_vote_exe.py — Multi-threaded voter (Jimin-only), SPEED TUNED
# - Real-name emails only
# - Prompts only for threads & loops; starts immediately, ends when done
# - Smaller visible window
# - Fast 10-clicks + fast Submit
# - Fast reset between loops (no slow logout)
# - Step timing logs so you can see where time goes
# - Total-vote aggregation, max threads = 6
# - No color dependencies
# - Selenium 4 Service() usage

import time, random, re, sys, argparse, threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- Selenium & Drivers ----------
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, ElementClickInterceptedException,
    StaleElementReferenceException, WebDriverException
)
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

# ---------- Config ----------
VOTE_URL = "https://www.mtv.com/vma/vote"
CATEGORY_ID = "#accordion-button-best-k-pop"
ARTIST_X_H3 = "//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']"
MAX_THREADS = 6  # hard cap

# ---------- Globals ----------
_global_submit_count = 0
_submit_lock = threading.Lock()
_counter_lock = threading.Lock()
_global_vote_no = 0

# ---------- Helper to pause before exit ----------
def _pause_exit():
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass

# ---------- CLI ----------
def parse_args():
    parser = argparse.ArgumentParser(description=f"VMA voter (Selenium, multi-thread, max threads = {MAX_THREADS})")
    parser.add_argument("--threads", type=int, default=1, help=f"Number of parallel threads (max {MAX_THREADS})")
    parser.add_argument("--loops", type=int, default=1, help="Loops per thread (0 = infinite)")
    parser.add_argument("--edge", action="store_true", help="Use Edge instead of Chrome")
    return parser.parse_args()

# ---------- Small utils ----------
def rdelay(a=0.02, b=0.05): time.sleep(random.uniform(a, b))

def safe_click(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", el)
        rdelay(0.01, 0.03)
        el.click()
        return True
    except (ElementClickInterceptedException, WebDriverException):
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except WebDriverException:
            return False

def quick_wait(driver, condition, timeout=5.0, poll=0.05):
    return WebDriverWait(driver, timeout, poll_frequency=poll).until(condition)

def next_vote_no() -> int:
    global _global_vote_no
    with _counter_lock:
        _global_vote_no += 1
        return _global_vote_no

# ---------- Real-name email generator ----------
FIRST = ["john","michael","sarah","emily","david","chris","anna","lisa","mark","paul","james","laura",
         "peter","susan","robert","nancy","kevin","mary","brian","julia","alex","joshua","olivia","matthew",
         "daniel","jennifer","thomas","andrew","stephanie","karen","tyler","nicole","heather","eric","amanda",
         "ryan","brandon","rachel","jason","patrick","victoria","kimberly","melissa","ashley","brittany","helen",
         "timothy","catherine","dennis","jacob","ethan","zoe","nathan","grace","henry","noah","ava","mia",
         "isabella","sophia"]
LAST = ["smith","johnson","williams","brown","jones","miller","davis","garcia","rodriguez","martinez",
        "hernandez","lopez","gonzalez","wilson","anderson","thomas","taylor","moore","jackson","martin",
        "lee","thompson","white","harris","sanchez","clark","ramirez","lewis","robinson","walker"]
DOMAINS = ["gmail.com","outlook.com","yahoo.com","icloud.com","aol.com"]

def gen_email():
    fn = random.choice(FIRST)
    ln = random.choice(LAST)
    num = random.randint(1000, 99999)
    return f"{fn}.{ln}.{num}@{random.choice(DOMAINS)}".lower()

# ---------- Core flow (per thread) ----------
def worker(worker_id: int, loops: int, use_edge: bool=False):
    t0 = time.time()

    # Options: speed & lightness
    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument("--window-size=800,600")
    opts.add_argument("--disable-logging")
    opts.add_argument("--log-level=3")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-background-networking")
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)
    # Disable images, notifications, etc.
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
    }
    opts.add_experimental_option("prefs", prefs)
    # Load faster (don't wait for all subresources)
    opts.page_load_strategy = "eager"

    try:
        if use_edge:
            service = EdgeService(EdgeChromiumDriverManager().install())
            driver = webdriver.Edge(service=service, options=opts)
        else:
            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        driver.implicitly_wait(0)  # rely on explicit waits only
        driver.set_page_load_timeout(10)
        driver.set_script_timeout(8)
    except Exception as e:
        print(f"[T{worker_id}] ❌ Unable to start browser: {e}")
        return

    def fast_reset():
        """Clear auth and reload page quickly (faster than slow logout flows)."""
        try:
            driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
        except Exception:
            pass
        try:
            driver.delete_all_cookies()
        except Exception:
            pass
        try:
            driver.execute_script("location.reload()")
        except WebDriverException:
            try:
                driver.get(VOTE_URL)
            except Exception:
                pass

    def login():
        t = time.time()
        try:
            # If not already on the vote page, go there quickly
            if not driver.current_url.startswith(VOTE_URL):
                try:
                    driver.get(VOTE_URL)
                except TimeoutException:
                    pass
        except Exception:
            try:
                driver.get(VOTE_URL)
            except Exception:
                return False, None
        print(f"[T{worker_id}] ▶ started")

        # Light poke to trigger email gate if needed
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Add Vote']")
            driver.execute_script("arguments[0].click();", btn)
        except NoSuchElementException:
            pass

        # Email gate?
        try:
            email_input = driver.find_element(By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]")
        except NoSuchElementException:
            return True, None

        addr = gen_email()
        try:
            email_input.clear()
            email_input.send_keys(addr)
        except WebDriverException:
            return False, None

        # Click "Log in" – fast polling
        for _ in range(60):
            try:
                b = driver.find_element(By.XPATH, "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='log in']")
            except NoSuchElementException:
                try:
                    b = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log in')]")
                except NoSuchElementException:
                    b = None
            if b and safe_click(driver, b):
                break
            time.sleep(0.05)

        # Wait for email field to disappear (fast poll)
        try:
            quick_wait(driver,
                EC.invisibility_of_element_located((By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]")),
                timeout=6, poll=0.05
            )
        except TimeoutException:
            print(f"[T{worker_id}] ⚠️ email form stuck")
            return False, None
        print(f"[T{worker_id}] ...login done in {time.time()-t:.2f}s")
        return True, addr

    def open_section():
        try:
            btn = quick_wait(driver, EC.presence_of_element_located((By.CSS_SELECTOR, CATEGORY_ID)), timeout=5, poll=0.05)
        except TimeoutException:
            return False
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", btn)
        if (btn.get_attribute("aria-expanded") or "").lower() != "true":
            safe_click(driver, btn)
        return True

    def vote_jimin_only():
        t = time.time()
        if not open_section(): return False
        try:
            quick_wait(driver, EC.presence_of_element_located((By.XPATH, ARTIST_X_H3)), timeout=5, poll=0.05)
        except TimeoutException:
            return False
        try:
            add_btn = driver.find_element(By.XPATH, f"({ARTIST_X_H3}/following::button[@aria-label='Add Vote'])[1]")
        except NoSuchElementException:
            return False

        # 🔥 FAST 10 CLICKS: JS clicks with tight gaps (no counter polling)
        for _ in range(12):  # a couple extra; site caps at 10
            try:
                driver.execute_script("arguments[0].click();", add_btn)
            except WebDriverException:
                pass
            time.sleep(0.05)  # ~50ms between clicks

        # ⚡ Fast wait for Submit button in the modal, then click via JS
        SUBMIT_EQ = "//*[@role='dialog' or contains(@id,'chakra-modal')]//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']"
        SUBMIT_HAS = "//*[@role='dialog' or contains(@id,'chakra-modal')]//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]"
        btn = None
        for xp, to in [(SUBMIT_EQ, 5), (SUBMIT_HAS, 3),
                       ("//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']", 2),
                       ("//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]", 2)]:
            try:
                btn = quick_wait(driver, EC.element_to_be_clickable((By.XPATH, xp)), timeout=to, poll=0.05)
                break
            except TimeoutException:
                btn = None
        if btn:
            try:
                driver.execute_script("arguments[0].click();", btn)
            except WebDriverException:
                safe_click(driver, btn)

        print(f"[T{worker_id}] ...vote+submit in {time.time()-t:.2f}s")
        return True

    def between_loops_fast():
        """Fast reset to get back to email gate for next loop."""
        t = time.time()
        fast_reset()
        # Wait until either email field is visible OR the page fully reloaded with the accordion present
        try:
            quick_wait(driver, EC.any_of(
                EC.presence_of_element_located((By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]")),
                EC.presence_of_element_located((By.CSS_SELECTOR, CATEGORY_ID))
            ), timeout=6, poll=0.05)
        except TimeoutException:
            pass
        print(f"[T{worker_id}] ...reset in {time.time()-t:.2f}s")

    current_loop = 0
    try:
        while True:
            if loops != 0 and current_loop >= loops:
                break
            current_loop += 1

            ok, email = login()
            if not ok:
                print(f"[T{worker_id}] ⚠️ login failed"); break
            if not vote_jimin_only():
                print(f"[T{worker_id}] ⚠️ vote failed"); break

            with _submit_lock:
                global _global_submit_count
                _global_submit_count += 10

            vno = next_vote_no()
            print(f"[T{worker_id}] ✅ Submitted 10 votes (#{vno}) | {email or 'N/A'}")

            between_loops_fast()

    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ---------- Main ----------
if __name__ == "__main__":
    try:
        args = parse_args()

        # prompt only threads & loops
        try:
            val = input(f"Threads (max {MAX_THREADS}) [{args.threads}]: ").strip()
            if val:
                args.threads = min(MAX_THREADS, max(1, int(val)))
        except Exception:
            pass

        try:
            val = input(f"Loops per thread (0 = infinite) [{args.loops}]: ").strip()
            if val: args.loops = max(0, int(val))
        except Exception:
            pass

        threads  = min(MAX_THREADS, max(1, args.threads))
        loops    = max(0, args.loops)
        use_edge = bool(args.edge)

        start_clock = time.time()
        print(f"▶ Starting {threads} thread(s); loops per thread = {loops or '∞'}; browser={'Edge' if use_edge else 'Chrome'}")

        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(worker, i+1, loops, use_edge) for i in range(threads)]
            for _ in as_completed(futs):
                pass

        finish_clock = time.time()
        print(f"🕒 Started : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_clock))}")
        print(f"🕒 Finished: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finish_clock))}")
        print(f"🧮 Total votes (all threads): {_global_submit_count}")
        print(f"🏁 All threads finished in {finish_clock - start_clock:.1f}s")

    except Exception:
        import traceback
        print("\n=== FATAL ERROR ===")
        traceback.print_exc()

    finally:
        _pause_exit()
        sys.exit(0)
