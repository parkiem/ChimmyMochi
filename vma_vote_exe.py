# vma_vote_exe.py — Multi-threaded voter (Jimin-only)
# Real-name emails only, smaller window, interactive prompts, start/end scheduling,
# total-vote aggregation, robust Submit modal, max threads = 6, and OPTIONAL color output.

import time, random, re, sys, argparse, threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- Color Output (optional) ----------
try:
    from colorama import Fore, Style, init as _cinit
    _cinit(autoreset=True)
except Exception:
    # Fallback: define no-op colors so the script runs even if colorama isn't present
    class _NoColor:
        def __getattr__(self, _): return ""
    Fore = Style = _NoColor()

# ---------- Selenium & Drivers ----------
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
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
END_TS_GLOBAL: Optional[float] = None

# ---------- CLI ----------
def parse_args():
    parser = argparse.ArgumentParser(description=f"VMA voter (Selenium, multi-thread, max threads = {MAX_THREADS})")
    parser.add_argument("--threads", type=int, default=1, help=f"Number of parallel threads (max {MAX_THREADS})")
    parser.add_argument("--loops", type=int, default=1, help="Loops per thread (0 = infinite)")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--edge", action="store_true", help="Use Edge instead of Chrome")
    return parser.parse_args()

# ---------- Time helpers ----------
def _parse_hhmm(s: Optional[str]):
    if not s: return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m: raise ValueError(f"Invalid time '{s}' (HH:MM)")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60): raise ValueError(f"Invalid time '{s}'")
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, now.tm_wday, now.tm_yday, now.tm_isdst))

def _wait_until(ts: Optional[float]):
    if not ts: return
    while True:
        now = time.time()
        if now >= ts: break
        remain = int(ts - now)
        print(Fore.CYAN + f"⏳ Waiting to start… {remain:>4}s", end="\r")
        time.sleep(1)
    print()

# ---------- Small utils ----------
def rdelay(a=0.08, b=0.16): time.sleep(random.uniform(a, b))

def safe_click(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", el)
        rdelay(0.05, 0.12)
        el.click()
        return True
    except (ElementClickInterceptedException, WebDriverException):
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except WebDriverException:
            return False

def wait_css(driver, sel, timeout=8):
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, sel))
        )
    except TimeoutException:
        return None

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
def worker(worker_id: int, loops: int, headless: bool, use_edge: bool=False):
    opts = EdgeOptions() if use_edge else ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=800,600")
    for a in ["--disable-logging","--log-level=3","--no-default-browser-check","--disable-background-networking"]:
        opts.add_argument(a)

    try:
        if use_edge:
            driver = webdriver.Edge(EdgeChromiumDriverManager().install(), options=opts)
        else:
            driver = webdriver.Chrome(ChromeDriverManager().install(), options=opts)
    except Exception as e:
        print(Fore.RED + f"[T{worker_id}] ❌ Unable to start browser: {e}")
        return

    def login():
        try:
            driver.get(VOTE_URL)
        except WebDriverException as e:
            print(Fore.RED + f"[T{worker_id}] nav error: {e}")
            return False, None
        print(Fore.CYAN + f"[T{worker_id}] ▶ started")

        rdelay(0.6, 1.0)
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Add Vote']")
            safe_click(driver, btn)
            rdelay(0.5, 0.9)
        except NoSuchElementException:
            pass

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

        logged = False
        for _ in range(40):
            try:
                btn = driver.find_element(By.XPATH, "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='log in']")
            except NoSuchElementException:
                try:
                    btn = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log in')]")
                except NoSuchElementException:
                    btn = None
            if btn and safe_click(driver, btn):
                logged = True
                break
            time.sleep(0.15)
        if not logged:
            print(Fore.YELLOW + f"[T{worker_id}] ⚠️ could not click Log in")
            return False, None

        try:
            WebDriverWait(driver, 8).until_not(
                EC.presence_of_element_located((By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]"))
            )
        except TimeoutException:
            print(Fore.YELLOW + f"[T{worker_id}] ⚠️ email form stuck")
            return False, None
        return True, addr

    def open_section():
        btn = wait_css(driver, CATEGORY_ID, 6)
        if not btn: return False
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", btn)
        rdelay(0.5, 0.9)
        if (btn.get_attribute("aria-expanded") or "").lower() != "true":
            safe_click(driver, btn)
            rdelay(0.8, 1.2)
        return True

    def vote_jimin_only():
        if not open_section(): return False
        try:
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, ARTIST_X_H3)))
        except TimeoutException:
            return False
        try:
            add_btn = driver.find_element(By.XPATH, f"({ARTIST_X_H3}/following::button[@aria-label='Add Vote'])[1]")
        except NoSuchElementException:
            return False

        counter_el = None
        for xp in [
            f"({ARTIST_X_H3}/following::p[contains(@class,'chakra-text')])[1]",
            f"({ARTIST_X_H3}/following::p)[1]"
        ]:
            try:
                counter_el = driver.find_element(By.XPATH, xp); break
            except NoSuchElementException:
                pass

        last, stagnant = -1, 0
        for _ in range(25):
            if not safe_click(driver, add_btn):
                rdelay(0.08, 0.16)
            rdelay(0.10, 0.16)
            if counter_el:
                try:
                    txt = counter_el.text or ""
                    m = re.search(r"\d+", txt)
                    if m:
                        n = int(m.group(0))
                        if n >= 10: break
                        if n == last: stagnant += 1
                        else: stagnant, last = 0, n
                        if stagnant >= 5: break
                except StaleElementReferenceException:
                    pass

        def click_submit_modal(tries=60, gap=0.15):
            MODAL_X = "//*[@role='dialog' or contains(@id,'chakra-modal')]"
            def text_ok(s: str) -> bool:
                s = s.lower()
                return ("distributed" in s) and ("10" in s) and ("votes" in s)
            for _ in range(tries):
                try:
                    dlgs = driver.find_elements(By.XPATH, MODAL_X)
                    target = None
                    for dlg in dlgs:
                        try:
                            tx = (dlg.text or "").strip()
                            if text_ok(tx):
                                target = dlg; break
                        except Exception:
                            pass
                    if target is not None:
                        try:
                            b = target.find_element(By.XPATH, ".//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']")
                            if safe_click(driver, b): return True
                        except NoSuchElementException:
                            pass
                        try:
                            b = target.find_element(By.XPATH, ".//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]")
                            if safe_click(driver, b): return True
                        except NoSuchElementException:
                            pass
                except Exception:
                    pass
                time.sleep(gap)
            for xp in [
                "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']",
                "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]",
            ]:
                try:
                    b = driver.find_element(By.XPATH, xp)
                    if safe_click(driver, b): return True
                except NoSuchElementException:
                    pass
            return False

        click_submit_modal()
        for _ in range(12):
            try:
                b = driver.find_element(By.XPATH, "//button[not(@disabled) and (contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'ok') or contains(.,'done') or contains(.,'close') or contains(.,'continue'))]")
                if safe_click(driver, b): break
            except NoSuchElementException:
                pass
            time.sleep(0.15)
        return True

    def logout_and_wait():
        rdelay(1.2, 2.0)
        try:
            b = driver.find_element(By.CSS_SELECTOR, "button.chakra-button.AuthNav__login-btn.css-ki1yvo")
            safe_click(driver, b)
        except NoSuchElementException:
            try:
                b = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out')]")
                safe_click(driver, b)
            except NoSuchElementException:
                try:
                    driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                    driver.get(VOTE_URL)
                except Exception:
                    pass
        time.sleep(random.uniform(3.0, 5.0))

    current_loop = 0
    try:
        while True:
            if END_TS_GLOBAL and time.time() >= END_TS_GLOBAL:
                print(Fore.CYAN + f"[T{worker_id}] ⏹ End time reached — stopping")
                break
            if loops != 0 and current_loop >= loops:
                break
            current_loop += 1

            ok, email = login()
            if not ok:
                print(Fore.YELLOW + f"[T{worker_id}] ⚠️ login failed"); break
            if not vote_jimin_only():
                print(Fore.YELLOW + f"[T{worker_id}] ⚠️ vote failed"); break

            with _submit_lock:
                global _global_submit_count
                _global_submit_count += 10

            vno = next_vote_no()
            print(Fore.GREEN + f"[T{worker_id}] ✅ Submitted 10 votes (#{vno}) | {email or 'N/A'}")

            logout_and_wait()

    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ---------- Main ----------
if __name__ == "__main__":
    args = parse_args()

    try:
        val = input(f"Threads (max {MAX_THREADS}) [{args.threads}]: ").strip()
        if val:
            args.threads = min(MAX_THREADS, max(1, int(val)))
    except Exception: pass

    try:
        val = input(f"Loops per thread (0 = infinite) [{args.loops}]: ").strip()
        if val: args.loops = max(0, int(val))
    except Exception: pass

    try:
        val = input("Start time HH:MM (enter = now): ").strip()
        start_time = val if val else None
    except Exception:
        start_time = None

    try:
        val = input("End time HH:MM (enter = none): ").strip()
        end_time = val if val else None
    except Exception:
        end_time = None

    threads  = min(MAX_THREADS, max(1, args.threads))
    loops    = max(0, args.loops)
    headless = bool(args.headless)
    use_edge = bool(args.edge)

    start_ts = _parse_hhmm(start_time)
    end_ts   = _parse_hhmm(end_time)
    if start_ts: _wait_until(start_ts)
    END_TS_GLOBAL = end_ts

    start_clock = time.time()
    print(Fore.CYAN + f"▶ Starting {threads} thread(s); loops per thread = {loops or '∞'}; headless={headless}; browser={'Edge' if use_edge else 'Chrome'}")
    if end_ts:
        print(Fore.CYAN + f"⏱ Will stop starting new loops after {time.strftime('%H:%M', time.localtime(end_ts))}")

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(worker, i+1, loops, headless, use_edge) for i in range(threads)]
        for _ in as_completed(futs):
            pass

    finish_clock = time.time()
    print(Fore.CYAN + f"🕒 Started : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_clock))}")
    print(Fore.CYAN + f"🕒 Finished: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finish_clock))}")
    print(Fore.GREEN + f"🧮 Total votes (all threads): {_global_submit_count}")
    print(Fore.CYAN + f"🏁 All threads finished in {finish_clock - start_clock:.1f}s")
    sys.exit(0)
