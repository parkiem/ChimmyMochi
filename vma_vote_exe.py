# vma_vote_exe.py — Multi-threaded voter (Jimin-only)
# Real-name emails only, smaller window, prompts only for threads & loops,
# starts immediately and ends when loops complete, total-vote aggregation,
# robust Submit modal, max threads = 6, no colorama.

import time, random, re, sys, argparse, threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# ---------- CLI ----------
def parse_args():
    parser = argparse.ArgumentParser(description=f"VMA voter (Selenium, multi-thread, max threads = {MAX_THREADS})")
    parser.add_argument("--threads", type=int, default=1, help=f"Number of parallel threads (max {MAX_THREADS})")
    parser.add_argument("--loops", type=int, default=1, help="Loops per thread (0 = infinite)")
    parser.add_argument("--edge", action="store_true", help="Use Edge instead of Chrome")
    return parser.parse_args()

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
def worker(worker_id: int, loops: int, use_edge: bool=False):
    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument("--window-size=800,600")  # visible, smaller window
    for a in ["--disable-logging","--log-level=3","--no-default-browser-check","--disable-background-networking"]:
        opts.add_argument(a)

    try:
        if use_edge:
            driver = webdriver.Edge(EdgeChromiumDriverManager().install(), options=opts)
        else:
            driver = webdriver.Chrome(ChromeDriverManager().install(), options=opts)
    except Exception as e:
        print(f"[T{worker_id}] ❌ Unable to start browser: {e}")
        return

    def login():
        try:
            driver.get(VOTE_URL)
        except WebDriverException as e:
            print(f"[T{worker_id}] nav error: {e}")
            return False, None
        print(f"[T{worker_id}] ▶ started")

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
            print(f"[T{worker_id}] ⚠️ could not click Log in")
            return False, None

        try:
            WebDriverWait(driver, 8).until_not(
                EC.presence_of_element_located((By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]"))
            )
        except TimeoutException:
            print(f"[T{worker_id}] ⚠️ email form stuck")
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

        # Submit modal
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
    sys.exit(0)
