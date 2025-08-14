# vma_vote_exe.py — multi-thread voter with human-like timing
# - Login on VOTY page then navigate to hub
# - K-Pop accordion -> Jimin only
# - Vote to real page cap (10/20) via "X/Y votes remaining"
# - Faster clicks (debounce-safe)
# - Per-thread loop progress (x/y), comma-list input supported
# - Press ANY key to stop
# - Block notifications; silence logs

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
LOGIN_URL = "https://www.mtv.com/event/vma/vote/video-of-the-year"   # login here
HUB_URL   = "https://www.mtv.com/vma/vote"                           # then navigate here
CATEGORY_ID = "#accordion-button-best-k-pop"
ARTIST_X_H3 = "//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']"  # exact 'Jimin'

MAX_THREADS = 50
SAFETY_CEILING = 20  # absolute safety ceiling in case UI misbehaves

# ---------- Globals ----------
_global_submit_count = 0
_submit_lock = threading.Lock()
stop_event = threading.Event()

# ---------- Small utils (faster) ----------
# global pacing knobs
CLICK_MIN, CLICK_MAX = 0.030, 0.060   # 30–60 ms between clicks
CONFIRM_POLL_MS      = 30             # poll card counter every 30 ms
CONFIRM_POLL_STEPS   = 12             # up to ~360 ms for a visible increment
DISABLED_PATIENCE    = 40             # ~1.2–2.4 s max patience for debounce

def rdelay(a=0.02, b=0.05):
    time.sleep(random.uniform(a, b))

def safe_click(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", el)
        time.sleep(0.005)
        el.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
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

def fmt_elapsed(s: float) -> str:
    if s >= 3600: return f"{s/3600:.2f} hr"
    if s >= 60:   return f"{s/60:.2f} min"
    return f"{s:.1f} sec"

def parse_loops_input(raw: str, threads: int):
    """Allow single number or comma-list (e.g., 10,8,12)."""
    raw = (raw or "").strip()
    if not raw:
        return [0] * threads  # 0 = infinite
    if "," in raw:
        parts = []
        for tok in raw.split(","):
            tok = tok.strip()
            try:
                parts.append(max(0, int(tok)))
            except:
                parts.append(0)
        if not parts:
            parts = [0]
        while len(parts) < threads:
            parts.append(parts[-1])
        return parts[:threads]
    else:
        try: v = max(0, int(raw))
        except: v = 0
        return [v] * threads

def start_hotkey_listener():
    """Press ANY key (Windows) or Enter (others) to stop gracefully."""
    try:
        import msvcrt
        def _listen():
            msvcrt.getch()
            stop_event.set()
            print("\n⛔ Stop requested — finishing current step…")
        threading.Thread(target=_listen, daemon=True).start()
    except Exception:
        def _listen():
            try: input()
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
LAST = ["smith","johnson","williams","brown","jones","miller","davis","garcia","rodriguez","martinez",
        "hernandez","lopez","gonzalez","wilson","anderson","thomas","taylor","moore","jackson","martin",
        "lee","thompson","white","harris","sanchez","clark","ramirez","lewis","robinson","walker"]
DOMAINS = ["gmail.com","outlook.com","yahoo.com","icloud.com","aol.com"]

def gen_email():
    fn = random.choice(FIRST); ln = random.choice(LAST); num = random.randint(1000, 99999)
    return f"{fn}{ln}{num}@{random.choice(DOMAINS)}".lower()

# ---------- Core flow (per thread) ----------
def worker(worker_id: int, loops_for_this_thread: int, use_edge: bool, win_size: str, win_pos: str):
    # Parse size/pos args
    try: w, h = [int(x) for x in win_size.split(",")]
    except Exception: w, h = 480, 360
    try: x, y = [int(x) for x in win_pos.split(",")]
    except Exception: x, y = 0, 0

    # Options: visible window, anti-throttle, block notifications, silence logs
    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument(f"--window-size={w},{h}")
    for a in [
        "--disable-logging", "--log-level=3",
        "--no-default-browser-check", "--disable-background-networking",
        "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding", "--disable-notifications", "--disable-speech-api",
        "--mute-audio", "--disable-crash-reporter"
    ]:
        opts.add_argument(a)
    try:
        opts.add_experimental_option("excludeSwitches", ["enable-logging","enable-automation"])
        opts.add_experimental_option('useAutomationExtension', False)
        opts.add_experimental_option("prefs", {
            "profile.default_content_setting_values.notifications": 2
        })
    except Exception:
        pass

    # Selenium 4 Service (silence service logs)
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

    # ---------- helpers ----------
    def login():
        """Login on the VOTY page, then go to the hub page."""
        if stop_event.is_set(): return False, None
        try: driver.get(LOGIN_URL)
        except WebDriverException as e:
            print(f"[T{worker_id}] nav error: {e}")
            return False, None

        rdelay(0.4, 0.8)
        # Nudge login modal by clicking any + if available
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Add Vote']")
            safe_click(driver, btn); rdelay(0.25, 0.45)
        except NoSuchElementException:
            pass

        # Email field present?
        try:
            email_input = driver.find_element(By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]")
        except NoSuchElementException:
            # already logged in; go to hub
            try: driver.get(HUB_URL)
            except Exception: pass
            return True, None

        addr = gen_email()
        try: email_input.clear(); email_input.send_keys(addr)
        except WebDriverException: return False, None

        # Click "Log in"
        logged = False
        for _ in range(50):
            if stop_event.is_set(): return False, None
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

        # Wait for email field to disappear
        try:
            WebDriverWait(driver, 8).until_not(
                EC.presence_of_element_located((By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]"))
            )
        except TimeoutException:
            print(f"[T{worker_id}] ⚠️ email form stuck")
            return False, None

        # Go to the hub page
        try: driver.get(HUB_URL)
        except Exception: pass
        return True, addr

    def open_section():
        if stop_event.is_set(): return False
        btn = wait_css(driver, CATEGORY_ID, 6)
        if not btn: return False
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
            if m: return int(m.group(1)), int(m.group(2))
        except NoSuchElementException:
            pass
        return None, None

    def vote_jimin_only_and_count() -> int:
        if stop_event.is_set(): return 0
        if not open_section(): return 0

        # Wait for the 'JIMIN' heading
        try:
            artist_node = WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.XPATH, ARTIST_X_H3))
            )
        except TimeoutException:
            return 0

        # Scroll into view
        try:
            driver.execute_script("arguments[0].scrollIntoView({behavior:'instant', block:'center'});", artist_node)
            time.sleep(0.12)
        except Exception:
            pass

        # Find the correct + button by walking from the heading (mirrors TM script)
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
            return 0  # never fall back to another card

        # Optional: nearby counter for stagnation
        try:
            counter_el = driver.find_element(By.XPATH, f"({ARTIST_X_H3}/following::p[contains(@class,'chakra-text')])[1]")
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

        # Use header as ground truth; stop at real cap
        remaining, header_cap = read_remaining_and_cap()
        dynamic_cap = min(SAFETY_CEILING, header_cap or SAFETY_CEILING)
        prev_remaining = remaining
        real_votes = 0
        disabled_streak = 0

        while True:
            if stop_event.is_set(): break

            # Already capped?
            remaining, hc = read_remaining_and_cap()
            if hc: dynamic_cap = min(dynamic_cap, hc)
            if remaining is not None and remaining <= 0:
                break

            # Modal up?
            vis, cap = submit_modal_visible_and_cap()
            if cap: dynamic_cap = min(dynamic_cap, cap)
            if vis: break

            # Button state
            aria = (add_btn.get_attribute("aria-disabled") or "").lower()
            if aria == "true" or add_btn.get_attribute("disabled") is not None:
                disabled_streak += 1
                if disabled_streak > 50:  # ~3s patience for debounce/queue
                    break
                time.sleep(0.05)
                continue
            disabled_streak = 0

            # CLICK (fast, but safe)
            if not safe_click(driver, add_btn):
                break
            time.sleep(random.uniform(0.06, 0.11))

            # Confirm only when HEADER decreases
            confirmed = False
            for _ in range(10):  # ~120–200 ms to see decrement
                time.sleep(0.05)
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

            # optional assist: reset stagnation if the local counter moves
            if not confirmed and counter_el:
                try:
                    _ = counter_el.text  # touching it keeps DOM ref fresh
                except StaleElementReferenceException:
                    pass

            # Guards
            if real_votes >= dynamic_cap: break
            if prev_remaining is not None and prev_remaining <= 0: break
            if real_votes >= SAFETY_CEILING: break

        # Click Submit (dialog only)
        def click_submit_modal():
            MODAL = "//*[@role='dialog' or contains(@id,'chakra-modal')]"
            for xp, to in [
                (MODAL + "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']", 6),
                (MODAL + "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]", 4)
            ]:
                try:
                    btn = quick_wait(driver, EC.element_to_be_clickable((By.XPATH, xp)), timeout=to, poll=0.05)
                    driver.execute_script("arguments[0].click();", btn)
                    return True
                except TimeoutException:
                    pass
                except WebDriverException:
                    pass
            return False

        click_submit_modal()
        return real_votes

    def logout_and_wait():
        if stop_event.is_set(): return
        time.sleep(random.uniform(1.0, 1.6))
        try:
            b = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out')]")
            safe_click(driver, b)
        except NoSuchElementException:
            try:
                driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                driver.get(HUB_URL)
            except Exception:
                pass
        time.sleep(random.uniform(2.2, 4.0))

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
            if stop_event.is_set(): break

            with _submit_lock:
                global _global_submit_count
                _global_submit_count += added

            loops_label = f"{current_loop}/{'∞' if loops_for_this_thread == 0 else loops_for_this_thread}"
            print(f"[T{worker_id}] ✅ Votes submitted — loops # {loops_label} | {email or 'N/A'}")

            logout_and_wait()

    finally:
        try: driver.quit()
        except Exception: pass

# ---------- CLI / Main ----------
def _pause_exit():
    try: input("\nPress Enter to close...")
    except Exception: pass

def parse_args():
    p = argparse.ArgumentParser(description=f"VMA voter (Selenium, multi-thread, max {MAX_THREADS})")
    p.add_argument("--threads", type=int, default=1, help=f"Parallel threads (max {MAX_THREADS})")
    p.add_argument("--loops",   type=int, default=1, help="Loops per thread (0 = infinite). You can override interactively.")
    p.add_argument("--edge", action="store_true", help="Use Edge instead of Chrome")
    p.add_argument("--win",  default="480,360", help="Window size WxH")
    p.add_argument("--pos",  default="0,0",    help="Window position X,Y")
    return p.parse_args()

if __name__ == "__main__":
    try:
        args = parse_args()

        # Prompts
        try:
            v = input(f"Threads (max {MAX_THREADS}) [{args.threads}]: ").strip()
            if v: args.threads = min(MAX_THREADS, max(1, int(v)))
        except Exception: pass

        try:
            v = input(f"Loops per thread (0 = infinite). Single number OR comma-list for each thread [{args.loops}]: ").strip()
            loops_list = parse_loops_input(v if v else str(args.loops), args.threads)
        except Exception:
            loops_list = [0] * args.threads

        threads  = min(MAX_THREADS, max(1, args.threads))
        use_edge = bool(args.edge)
        win_size = args.win
        win_pos  = args.pos

        # Start the ANY-key stop listener AFTER prompts (to avoid stealing input)
        start_hotkey_listener()
        print("▶ Press ANY key to stop…")

        start_clock = time.time()
        print(f"▶ Starting {threads} thread(s); loops per thread = {loops_list}; browser={'Edge' if use_edge else 'Chrome'}; win={win_size}; pos={win_pos}")

        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(worker, i+1, loops_list[i], use_edge, win_size, win_pos) for i in range(threads)]
            for _ in as_completed(futs): pass

        finish_clock = time.time()
        print(f"🕒 Started : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_clock))}")
        print(f"🕒 Finished: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finish_clock))}")
        print(f"🏁 All threads finished in {fmt_elapsed(finish_clock - start_clock)}")

    except Exception:
        import traceback
        print("\n=== FATAL ERROR ===")
        traceback.print_exc()
    finally:
        _pause_exit()
        sys.exit(0)
