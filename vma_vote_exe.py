# vma_vote_exe.py — Stable flow + human-like delays + anti-throttle (tiny visible window)

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
VOTE_URL = "https://www.mtv.com/event/vma/vote/best-k-pop"  # <<< changed: go straight to category page
CATEGORY_ID = "#accordion-button-best-k-pop"  # stays for backward compatibility (older layout)

# exact match for 'jimin' (case-insensitive), works across headings/spans
ARTIST_X = (
    "//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::p or self::span]"
    "[translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='jimin']"
)  # <<< changed

MAX_THREADS = 100
SAFETY_CEILING = 20  # never exceed this many clicks even if UI misbehaves

# ---------- Globals ----------
_global_submit_count = 0
_submit_lock = threading.Lock()

# stop-hotkey (optional – you can remove if not using)
try:
    import msvcrt
except Exception:
    msvcrt = None
stop_event = threading.Event()

# ---------- Small utils ----------
def rdelay(a=0.04, b=0.09):  # <<< faster default delays than before
    time.sleep(random.uniform(a, b))

def safe_click(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", el)
        time.sleep(0.01)
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

def start_hotkey_listener():
    def _listen():
        try:
            if msvcrt: msvcrt.getch()
            else: input()
        finally:
            stop_event.set()
            print("\n⛔ Stop requested — finishing current step…")
    threading.Thread(target=_listen, daemon=True).start()

# ---------- Real-name email generator ----------
FIRST = ["john","michael","sarah","emily","david","chris","anna","lisa","mark","paul","james","laura",
         "peter","susan","robert","nancy","kevin","mary","brian","julia","alex","joshua","olivia","matthew",
         "daniel","jennifer","thomas","andrew","stephanie","karen","tyler","nicole","heather","eric","amanda",
         "ryan","brandon","rachel","jason","patrick","victoria","kimberly","melissa","ashley","brittany","helen",
         "timothy","catherine","dennis","jacob","ethan","zoe","nathan","grace","henry","noah","ava","mia",
         "isabella","sophia"]
LAST = ["smith","john","wilams","brown","jones","miller","davis","garcia","rodri","martinez",
        "nandez","lopez","gonez","wilson","and","thomas","taylor","moore","jackson","martin",
        "lee","thompson","white","harris","sanchez","lark","ram","lewis","roson","walker"]
DOMAINS = ["gmail.com","outlook.com","yahoo.com","icloud.com","aol.com"]

def gen_email():
    fn = random.choice(FIRST)
    ln = random.choice(LAST)
    num = random.randint(100, 999)
    return f"{fn}{ln}{num}@{random.choice(DOMAINS)}".lower()

# ---------- Core flow (per thread) ----------
def worker(worker_id: int, loops_for_this_thread: int, use_edge: bool, win_size: str, win_pos: str):

    # Parse size/pos args
    try: w, h = [int(x) for x in win_size.split(",")]
    except Exception: w, h = 480, 360
    try: x, y = [int(x) for x in win_pos.split(",")]
    except Exception: x, y = 0, 0

    # Options: visible window, anti-throttle, silence logs
    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument(f"--window-size={w},{h}")
    for a in [
        "--disable-logging", "--log-level=3",
        "--no-default-browser-check", "--disable-background-networking",
        "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]: opts.add_argument(a)
    try:
        opts.add_experimental_option("excludeSwitches", ["enable-logging","enable-automation"])
        opts.add_experimental_option('useAutomationExtension', False)
    except Exception: pass

    # Selenium 4 Service
    try:
        if use_edge:
            service = EdgeService(EdgeChromiumDriverManager().install(), log_path='NUL')
            driver = webdriver.Edge(service=service, options=opts)
        else:
            service = ChromeService(ChromeDriverManager().install(), log_path='NUL')
            driver = webdriver.Chrome(service=service, options=opts)
        try: driver.set_window_position(x, y)
        except Exception: pass
    except Exception as e:
        print(f"[T{worker_id}] ❌ Unable to start browser: {e}")
        return

    # -------- helpers inside worker --------
    def login():
        try:
            driver.get(VOTE_URL)
        except WebDriverException as e:
            print(f"[T{worker_id}] nav error: {e}")
            return False, None

        rdelay(0.3, 0.6)
        if stop_event.is_set(): return False, None

        # poke any + button to surface login first time
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Add Vote']")
            safe_click(driver, btn); rdelay(0.2, 0.4)
        except NoSuchElementException:
            pass

        # email field for modal/full-page login
        try:
            email_input = driver.find_element(By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]")
        except NoSuchElementException:
            return True, None

        addr = gen_email()
        try:
            email_input.clear(); email_input.send_keys(addr)
        except WebDriverException:
            return False, None

        # click "Log in"
        logged = False
        for _ in range(40):
            try:
                b = driver.find_element(By.XPATH, "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='log in']")
            except NoSuchElementException:
                try:
                    b = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log in')]")
                except NoSuchElementException:
                    b = None
            if b and safe_click(driver, b):
                logged = True; break
            time.sleep(0.06)
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
        # If accordion exists (old layout), open; otherwise treat as already open
        btns = driver.find_elements(By.CSS_SELECTOR, CATEGORY_ID)
        if not btns: return True
        btn = btns[0]
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", btn)
        time.sleep(random.uniform(0.20, 0.35))
        if (btn.get_attribute("aria-expanded") or "").lower() != "true":
            safe_click(driver, btn); time.sleep(random.uniform(0.35, 0.55))
        return True

    def read_remaining_and_cap():
        """Parse 'X/Y votes remaining' near the category header."""
        try:
            hdr = driver.find_element(
                By.XPATH,
                "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'votes remaining')]"
            )
            txt = (hdr.text or "").lower()
            m = re.search(r'(\d+)\s*/\s*(\d+)\s+votes remaining', txt)
            if m:
                return int(m.group(1)), int(m.group(2))
        except NoSuchElementException:
            pass
        return None, None

    def vote_jimin_only_and_count() -> int:
        if stop_event.is_set(): return 0
        if not open_section(): return 0

        # Find the exact 'Jimin' node
        try:
            artist_node = WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.XPATH, ARTIST_X))
            )
        except TimeoutException:
            return 0

        # Scroll into view
        try:
            driver.execute_script("arguments[0].scrollIntoView({behavior:'instant', block:'center'});", artist_node)
            time.sleep(0.15)
        except Exception:
            pass

        # Find the Jimin + button by walking from the heading (mirrors your JS hops)
        find_btn_js = """
        const start = arguments[0];
        let node = start;
        for (let i = 0; i < 8; i++) {
          if (!node) break;
          if (node.querySelector) {
            const btn = node.querySelector('button[aria-label="Add Vote"]');
            if (btn) return btn;
          }
          node = node.nextElementSibling || node.parentElement;
        }
        return null;
        """
        add_btn = driver.execute_script(find_btn_js, artist_node)
        if not add_btn:
            return 0  # do NOT fall back to another artist

        # Optional: a nearby counter to detect stagnation (like your TM script)
        try:
            counter_el = driver.find_element(By.XPATH, f"({ARTIST_X}/following::p[contains(@class,'chakra-text')])[1]")
        except NoSuchElementException:
            counter_el = None

        # Strict modal detector (dialog only)
        def submit_modal_visible_and_cap():
            try:
                modals = driver.find_elements(By.XPATH, "//*[@role='dialog' or contains(@id,'chakra-modal')]")
                for m in modals:
                    txt = (m.text or "").lower()
                    if ("distributed" in txt or "you have" in txt) and "vote" in txt:
                        mm = re.search(r"(\d+)\s+vote", txt)
                        cap = int(mm.group(1)) if mm else None
                        for b in m.find_elements(By.XPATH, ".//button"):
                            t = (b.text or "").strip().lower()
                            if any(k in t for k in ("submit","confirm","continue")):
                                return True, cap
            except Exception:
                pass
            return False, None

        # Use the header to count *confirmed* votes and stop at real cap
        remaining, header_cap = read_remaining_and_cap()
        dynamic_cap = min(SAFETY_CEILING, header_cap or SAFETY_CEILING)
        prev_remaining = remaining
        real_votes = 0
        stagnant = 0
        disabled_streak = 0

        while True:
            if stop_event.is_set(): break

            # if already capped / no remaining, stop
            remaining, hc = read_remaining_and_cap()
            if hc: dynamic_cap = min(dynamic_cap, hc)
            if remaining is not None and remaining <= 0:
                break

            # modal up?
            vis, cap = submit_modal_visible_and_cap()
            if cap: dynamic_cap = min(dynamic_cap, cap)
            if vis: break

            # button state
            aria = (add_btn.get_attribute("aria-disabled") or "").lower()
            if aria == "true" or add_btn.get_attribute("disabled") is not None:
                disabled_streak += 1
                if disabled_streak > 60:  # ~3–4s total wait
                    break
                time.sleep(0.05)
                continue
            disabled_streak = 0

            # CLICK — faster spacing than before (60–110 ms)
            if not safe_click(driver, add_btn):
                break
            time.sleep(random.uniform(0.06, 0.11))  # <<< faster

            # Confirm via header change (don’t over-count)
            confirmed = False
            for _ in range(10):  # up to ~500ms to see the decrement
                rdelay(0.03, 0.06)
                r_now, hc2 = read_remaining_and_cap()
                if hc2: dynamic_cap = min(dynamic_cap, hc2)
                if r_now is not None:
                    if prev_remaining is None:
                        prev_remaining = r_now
                    elif r_now < prev_remaining:
                        real_votes += 1
                        prev_remaining = r_now
                        confirmed = True
                        break
            if not confirmed and counter_el:
                # fall back: if on-card counter increments, accept it
                try:
                    txt = counter_el.text or ""
                    m = re.search(r"\d+", txt)
                    if m:
                        stagnant = 0  # treat as moving
                except StaleElementReferenceException:
                    pass

            # stop points
            if real_votes >= dynamic_cap: break
            if prev_remaining is not None and prev_remaining <= 0: break
            if real_votes >= SAFETY_CEILING: break

        # Click Submit (dialog only)
        def click_submit_modal():
            MODAL = "//*[@role='dialog' or contains(@id,'chakra-modal')]"
            X_EQ  = MODAL + "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']"
            X_HAS = MODAL + "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]"
            btn = None
            for xp, to in [(X_EQ, 5), (X_HAS, 3)]:
                try:
                    btn = quick_wait(driver, EC.element_to_be_clickable((By.XPATH, xp)), timeout=to, poll=0.05)
                    break
                except TimeoutException:
                    btn = None
            if not btn: return False
            try:
                driver.execute_script("arguments[0].click();", btn)
                return True
            except WebDriverException:
                return safe_click(driver, btn)

        click_submit_modal()
        return real_votes

    def logout_and_wait():
        if stop_event.is_set(): return
        time.sleep(random.uniform(0.8, 1.4))  # shorter cool-off
        try:
            b = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out')]")
            safe_click(driver, b)
        except NoSuchElementException:
            try:
                driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                driver.get(VOTE_URL)
            except Exception:
                pass
        time.sleep(random.uniform(1.8, 3.5))  # shorter human pause

    # --------- Main per-thread loop ----------
    current_loop = 0
    try:
        while True:
            if stop_event.is_set(): break
            if loops_for_this_thread != 0 and current_loop >= loops_for_this_thread: break
            current_loop += 1

            ok, email = login()
            if not ok or stop_event.is_set():
                print(f"[T{worker_id}] ⚠️ login failed or stopped"); break

            added = vote_jimin_only_and_count()
            with _submit_lock:
                global _global_submit_count
                _global_submit_count += added

            loops_label = f"{current_loop}/{'∞' if loops_for_this_thread == 0 else loops_for_this_thread}"
            print(f"[T{worker_id}] ✅ Submitted {added} vote(s) — loop {loops_label} | {email or 'N/A'}")

            logout_and_wait()

    finally:
        try: driver.quit()
        except Exception: pass

# ---------- Helper to pause before exit ----------
def _pause_exit():
    try: input("\nPress Enter to close...")
    except Exception: pass

# ---------- CLI ----------
def parse_args():
    parser = argparse.ArgumentParser(description=f"VMA voter (Selenium, multi-thread, max threads = {MAX_THREADS})")
    parser.add_argument("--threads", type=int, default=1, help=f"Number of parallel threads (max {MAX_THREADS})")
    parser.add_argument("--loops", type=int, default=1, help="Loops per thread (0 = infinite)")
    parser.add_argument("--edge", action="store_true", help="Use Edge instead of Chrome")
    parser.add_argument("--win", default="480,360", help="Window size WxH (default 480,360)")
    parser.add_argument("--pos", default="0,0", help="Window position X,Y (default 0,0)")
    return parser.parse_args()

# ---------- Main ----------
if __name__ == "__main__":
    try:
        args = parse_args()

        # prompts
        try:
            val = input(f"Threads (max {MAX_THREADS}) [{args.threads}]: ").strip()
            if val: args.threads = min(MAX_THREADS, max(1, int(val)))
        except Exception: pass

        try:
            val = input(f"Loops per thread (0 = infinite) [{args.loops}]: ").strip()
            if val: args.loops = max(0, int(val))
        except Exception: pass

        threads  = min(MAX_THREADS, max(1, args.threads))
        loops    = max(0, args.loops)
        use_edge = bool(args.edge)

        win_size = args.win
        win_pos  = args.pos

        # start hotkey AFTER prompts so it doesn't eat your input
        start_hotkey_listener()

        start_clock = time.time()
        print(f"▶ Starting {threads} thread(s); loops per thread = {loops or '∞'}; browser={'Edge' if use_edge else 'Chrome'}; win={win_size}; pos={win_pos}")
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(worker, i+1, loops, use_edge, win_size, win_pos) for i in range(threads)]
            for _ in as_completed(futs): pass

        finish_clock = time.time()
        print(f"🕒 Started : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_clock))}")
        print(f"🕒 Finished: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finish_clock))}")
        print(f"🧮 Total votes (all threads): {_global_submit_count}")
        elapsed = finish_clock - start_clock
        if elapsed >= 3600:
            print(f"🏁 All threads finished in {elapsed/3600:.2f} hr")
        elif elapsed >= 60:
            print(f"🏁 All threads finished in {elapsed/60:.2f} min")
        else:
            print(f"🏁 All threads finished in {elapsed:.1f} sec")

    except Exception:
        import traceback
        print("\n=== FATAL ERROR ===")
        traceback.print_exc()
    finally:
        _pause_exit()
        sys.exit(0)
