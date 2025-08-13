# vma_vote_exe.py — Multi-threaded voter (Jimin-only), robust login + fast submit
# - Visible browser (no headless)
# - Prompts only for threads & loops; starts immediately, ends when done
# - Smaller window (800x600)
# - Real-name email generator; prints email every loop
# - Fast 10-clicks + fast Submit
# - Strong reset between loops (cookies + storage cleared; reload to vote page)
# - Total-vote aggregation, max threads = 6
# - Selenium 4 Service() usage
# - Pause before exit so logs are visible

import time, random, re, sys, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- Selenium & Drivers ----------
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
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
    # Options: speed & lightness
    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument("--window-size=800,600")
    opts.add_argument("--disable-logging")
    opts.add_argument("--log-level=3")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-background-networking")
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)
    # Disable images, notifications, etc. to speed things up
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.default_content_setting_values.notifications": 2,
        "profile.default_content_setting_values.geolocation": 2,
    }
    opts.add_experimental_option("prefs", prefs)
    # Faster page loads (don't wait for all subresources)
    opts.page_load_strategy = "eager"

    # Launch browser (Selenium 4 Service API)
    try:
        if use_edge:
            service = EdgeService(EdgeChromiumDriverManager().install())
            driver = webdriver.Edge(service=service, options=opts)
        else:
            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        driver.implicitly_wait(0)
        driver.set_page_load_timeout(12)
        driver.set_script_timeout(8)
    except Exception as e:
        print(f"[T{worker_id}] ❌ Unable to start browser: {e}")
        return

    def between_loops_fast():
        """Strong reset so each loop starts clean and shows email gate."""
        try:
            driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
        except Exception:
            pass
        try:
            driver.delete_all_cookies()
        except Exception:
            pass
        try:
            driver.get(VOTE_URL)
        except Exception:
            try:
                driver.execute_script("location.href = arguments[0];", VOTE_URL)
            except Exception:
                pass
        time.sleep(0.3)  # give SPA a moment to mount

    def login():
        """Handles full-page auth or modal. Returns (True, email) or (True, None) if already logged in."""
        # Ensure we're on the vote page (or navigate there)
        try:
            if not driver.current_url.startswith(VOTE_URL):
                try:
                    driver.get(VOTE_URL)
                except TimeoutException:
                    pass
        except Exception:
            try:
                driver.get(VOTE_URL)
            except Exception as e:
                print(f"[T{worker_id}] nav error: {e}")
                return False, None
        print(f"[T{worker_id}] ▶ started")

        # Poke "Add Vote" to trigger inline gate (if present)
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Add Vote']")
            driver.execute_script("arguments[0].click();", btn)
        except NoSuchElementException:
            pass

        # Helper: find an email input (modal or full page)
        def find_email_input():
            Xs = [
                "//input[@type='email']",
                "//input[contains(@id,'field-') and contains(@id,':')]",  # Chakra random ids
                "//input[@inputmode='email']",
                "//input[contains(translate(@placeholder,'EMAIL','email'),'email') or contains(translate(@aria-label,'EMAIL','email'),'email') or contains(translate(@name,'EMAIL','email'),'email')]",
            ]
            for xp in Xs:
                try:
                    el = driver.find_element(By.XPATH, xp)
                    if el.is_displayed():
                        return el
                except NoSuchElementException:
                    continue
            return None

        # Helper: click a login/continue/submit-like button
        def click_login_like():
            Xs = [
                "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='log in']",
                "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='login']",
                "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'continue')]",
                "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'next')]",
                "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]",
            ]
            for xp in Xs:
                try:
                    b = driver.find_element(By.XPATH, xp)
                    if b.is_displayed():
                        try:
                            b.click(); return True
                        except WebDriverException:
                            try:
                                driver.execute_script("arguments[0].click();", b); return True
                            except WebDriverException:
                                pass
                except NoSuchElementException:
                    continue
            return False

        # If category accordion is visible and no email input, assume we’re logged in already
        try:
            if driver.find_elements(By.CSS_SELECTOR, CATEGORY_ID) and not find_email_input():
                return True, None
        except Exception:
            pass

        # Wait for email input to appear (up to ~8s)
        email_input = None
        for _ in range(80):
            email_input = find_email_input()
            if email_input:
                break
            time.sleep(0.1)

        if not email_input:
            # Might already be logged in if section is present
            try:
                if driver.find_elements(By.CSS_SELECTOR, CATEGORY_ID):
                    return True, None
            except Exception:
                pass
            print(f"[T{worker_id}] ⚠️ no email field found")
            return False, None

        # Type email + submit (ENTER + click fallback)
        addr = gen_email()
        try:
            driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", email_input)
            email_input.clear()
            email_input.send_keys(addr)
            email_input.send_keys(Keys.ENTER)
        except WebDriverException as e:
            print(f"[T{worker_id}] email typing error: {e}")
            return False, None

        # Also click a login-like button to be safe
        click_login_like()

        # Wait until the email field disappears or category appears (~6s)
        ok = False
        for _ in range(120):
            if not find_email_input():
                ok = True; break
            try:
                if driver.find_elements(By.CSS_SELECTOR, CATEGORY_ID):
                    ok = True; break
            except Exception:
                pass
            time.sleep(0.05)

        if not ok:
            print(f"[T{worker_id}] ⚠️ login didn’t complete in time")
            return False, None

        print(f"[T{worker_id}] ...login with {addr}")
        return True, addr

    def open_section():
        try:
            btn = quick_wait(driver, EC.presence_of_element_located((By.CSS_SELECTOR, CATEGORY_ID)), timeout=6, poll=0.06)
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
            quick_wait(driver, EC.presence_of_element_located((By.XPATH, ARTIST_X_H3)), timeout=6, poll=0.06)
        except TimeoutException:
            return False
        try:
            add_btn = driver.find_element(By.XPATH, f"({ARTIST_X_H3}/following::button[@aria-label='Add Vote'])[1]")
        except NoSuchElementException:
            return False

        # Fast 10 clicks (a couple extra; site caps at 10)
        for _ in range(12):
            try:
                driver.execute_script("arguments[0].click();", add_btn)
            except WebDriverException:
                pass
            time.sleep(0.05)

        # Find and click Submit quickly
        SUBMIT_EQ  = "//*[@role='dialog' or contains(@id,'chakra-modal')]//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']"
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
            # tiny backoff if animating
            try:
                driver.execute_script("return arguments[0].offsetParent !== null", btn)
                time.sleep(0.03)
            except Exception:
                pass
            try:
                driver.execute_script("arguments[0].click();", btn)
            except WebDriverException:
                safe_click(driver, btn)

        print(f"[T{worker_id}] ...vote+submit in {time.time()-t:.2f}s")
        return True

    # --------- Main per-thread loop ----------
    current_loop = 0
    try:
        while True:
            if loops != 0 and current_loop >= loops:
                break
            current_loop += 1

            ok, email = login()
            if not ok:
                print(f"[T{worker_id}] ⚠️ login failed")
                between_loops_fast()
                continue

            if not vote_jimin_only():
                print(f"[T{worker_id}] ⚠️ vote failed")
                between_loops_fast()
                continue

            with _submit_lock:
                global _global_submit_count
                _global_submit_count += 10

            vno = next_vote_no()
            print(f"[T{worker_id}] ✅ Submitted 10 votes (#{vno}) | {email or 'N/A'}")

            # Prepare for the next loop quickly
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
