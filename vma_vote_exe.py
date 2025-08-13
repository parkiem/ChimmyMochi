# vma_vote_exe.py — Stable flow + human-like delays + anti-throttle (tiny visible window)
# - Visible browser (no headless). Small 480x360 window; moved to (0,0). Do NOT minimize.
# - Anti-throttling flags so it keeps running even if the window is covered/behind others.
# - Prompts only for threads & loops; starts immediately.
# - Real-name email generator; prints the email used every loop.
# - Add-Vote clicks slowed to 120–200 ms spacing (12 clicks to ensure 10 register).
# - Fast Submit detection (50 ms polling + tiny backoff).
# - Logout/reset: short 1.0–1.8 s + extra human pause 2.5–6.5 s between accounts.
# - Selenium 4 Service() API. Max threads = 6. Pause before exit.

import time, random, re, sys, argparse, threading
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
MAX_THREADS = 20  # hard cap

# ---------- Globals ----------
_global_submit_count = 0
_submit_lock = threading.Lock()

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
    # Optional: override window size/position without editing code
    parser.add_argument("--win", default="480,360", help="Window size WxH (default 480,360)")
    parser.add_argument("--pos", default="0,0", help="Window position X,Y (default 0,0)")
    return parser.parse_args()

# ---------- Small utils ----------
def rdelay(a=0.08, b=0.16): time.sleep(random.uniform(a, b))

def safe_click(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", el)
        time.sleep(0.02)
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

def quick_wait(driver, condition, timeout=6.0, poll=0.05):
    return WebDriverWait(driver, timeout, poll_frequency=poll).until(condition)

def detect_vote_limit(driver):
    """
    Best-effort read of the page's per-submit vote cap (10, 20, etc.).
    Returns an int; falls back to 10 if not found.
    """
    # 1) Look for a counter like "3/20", "0 / 10", etc.
    try:
        scopes = [
            "//*[@role='dialog' or contains(@id,'chakra-modal')]",
            "//*[contains(@class,'chakra')]",
            "//*",
        ]
        for scope in scopes:
            els = driver.find_elements(
                By.XPATH,
                f"{scope}[.//text()[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'vote') or contains(text(),'/')]]"
            )
            for el in els:
                t = (el.text or "").strip()
                m = re.search(r'(\d+)\s*/\s*(\d+)', t)
                if m:
                    cap = int(m.group(2))
                    if 1 <= cap <= 100:
                        return cap
    except Exception:
        pass

    # 2) Progressbar aria-valuemax fallback
    try:
        pbars = driver.find_elements(By.CSS_SELECTOR, '[role="progressbar"]')
        for pb in pbars:
            valmax = pb.get_attribute("aria-valuemax")
            if valmax and valmax.isdigit():
                cap = int(valmax)
                if 1 <= cap <= 100:
                    return cap
    except Exception:
        pass

    # Fallback
    return 10

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
    return f"{fn}{ln}{num}@{random.choice(DOMAINS)}".lower()

# ---------- Core flow (per thread) ----------
def worker(worker_id: int, loops: int, use_edge: bool, win_size: str, win_pos: str):
    # Parse size/pos args
    try:
        w, h = [int(x) for x in win_size.split(",")]
    except Exception:
        w, h = 480, 360
    try:
        x, y = [int(x) for x in win_pos.split(",")]
    except Exception:
        x, y = 0, 0

    # Options: visible small window, anti-throttle flags
    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument(f"--window-size={w},{h}")
    for a in [
        "--disable-logging",
        "--log-level=3",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]:
        opts.add_argument(a)

    # Selenium 4 Service
    try:
        if use_edge:
            service = EdgeService(EdgeChromiumDriverManager().install())
            driver = webdriver.Edge(service=service, options=opts)
        else:
            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        # keep it visible but tucked in a corner; do NOT minimize
        try:
            driver.set_window_position(x, y)
        except Exception:
            pass
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
            rdelay(0.4, 0.7)
        except NoSuchElementException:
            pass

        # email field for modal/full-page login
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

        # click "Log in" (handles both 'Log in' and 'Login' etc.)
        logged = False
        for _ in range(50):
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
            time.sleep(0.08)
        if not logged:
            print(f"[T{worker_id}] ⚠️ could not click Log in")
            return False, None

        # wait until the email form is gone
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
        time.sleep(random.uniform(0.30, 0.50))
        if (btn.get_attribute("aria-expanded") or "").lower() != "true":
            safe_click(driver, btn)
            time.sleep(random.uniform(0.50, 0.80))
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

        # --- HUMAN-LIKE ADD-VOTE CLICKS (adaptive to site limit) ---
         max_votes = detect_vote_limit(driver)   # e.g., 10 or 20
         safety_extra = 2                        # click a couple extra to ensure all register
         for _ in range(max_votes + safety_extra):
             try:
                 driver.execute_script("arguments[0].click();", add_btn)
             except WebDriverException:
                 pass
             time.sleep(random.uniform(0.12, 0.20))


        # --- FAST SUBMIT DETECTION & CLICK ---
        def click_submit_modal():
            MODAL_SCOPE = "//*[@role='dialog' or contains(@id,'chakra-modal')]"
            X_EQ  = MODAL_SCOPE + "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']"
            X_HAS = MODAL_SCOPE + "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]"

            btn = None
            # poll quickly (50ms) up to 6s in total
            for xp, to in [(X_EQ, 6), (X_HAS, 4),
                           ("//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']", 2),
                           ("//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]", 2)]:
                try:
                    btn = quick_wait(driver, EC.element_to_be_clickable((By.XPATH, xp)), timeout=to, poll=0.05)
                    break
                except TimeoutException:
                    btn = None
            if not btn:
                return False

            # small backoff if still animating, then JS click
            try:
                driver.execute_script("return arguments[0].offsetParent !== null", btn)
                time.sleep(0.03)
            except Exception:
                pass
            try:
                driver.execute_script("arguments[0].click();", btn)
                return True
            except WebDriverException:
                return safe_click(driver, btn)

        click_submit_modal()
        return True

       ok = click_submit_modal()
       awarded = get_awarded_votes_from_ui(driver, timeout=6.0)
       if awarded is None or not (1 <= awarded <= 100):
         awarded = max_votes
       return awarded if ok else 0


    def logout_and_wait():
        # Short cool-off to avoid hammering
        time.sleep(random.uniform(1.0, 1.8))
        # Try logout button if present; otherwise just clear & reload
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
        # Extra human-like pause before next account
        time.sleep(random.uniform(2.5, 6.5))

    # --------- Main per-thread loop ----------
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
    try:
        args = parse_args()

        # prompts
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

        # optional: pass window size/pos from CLI
        win_size = args.win
        win_pos  = args.pos

        start_clock = time.time()
        print(f"▶ Starting {threads} thread(s); loops per thread = {loops or '∞'}; browser={'Edge' if use_edge else 'Chrome'}; win={win_size}; pos={win_pos}")

        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(worker, i+1, loops, use_edge, win_size, win_pos) for i in range(threads)]
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


