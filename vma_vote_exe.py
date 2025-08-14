# vma_vote_exe.py — faster + resilient
# - Login on VOTY -> hub
# - K-Pop accordion -> Jimin only
# - Clicks confirmed by the Jimin card number (fast)
# - Shorter waits for submit/logout; per-thread loop x/y; any-key stop
# - Watchdog: re-find button/counter, reopen accordion if progress stalls
# - Site notifications & noisy logs disabled

import time, random, re, sys, argparse, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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
LOGIN_URL   = "https://www.mtv.com/event/vma/vote/video-of-the-year"
HUB_URL     = "https://www.mtv.com/vma/vote"
CATEGORY_ID = "#accordion-button-best-k-pop"
ARTIST_X_H3 = "//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']"  # exact Jimin

MAX_THREADS = 50
SAFETY_CEILING = 20  # absolute guard

# ---------- Timing knobs (tuned for speed) ----------
CLICK_MIN, CLICK_MAX = 0.030, 0.060      # 30–60 ms between clicks
CONFIRM_POLL_MS      = 30                # poll the card every 30 ms
CONFIRM_POLL_STEPS   = 12                # ~360 ms to see the number tick up
DISABLED_PATIENCE    = 30                # tolerate ~1s of disabled button before moving on
STALL_REQUERY_LIMIT  = 4                 # if this many clicks don’t confirm, re-find elements
STALL_REOPEN_LIMIT   = 2                 # after 2 re-queries still stuck -> reopen accordion

# Logout & human pauses (trimmed)
PAUSE_AFTER_SUBMIT   = (0.4, 0.8)
HUMAN_PAUSE_BETWEEN  = (0.7, 1.4)

# ---------- Globals ----------
_global_submit_count = 0
_submit_lock = threading.Lock()
stop_event = threading.Event()

# ---------- Small utils ----------
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
    raw = (raw or "").strip()
    if not raw: return [0]*threads
    if "," in raw:
        out = []
        for tok in raw.split(","):
            try: out.append(max(0,int(tok.strip())))
            except: out.append(0)
        while len(out)<threads: out.append(out[-1])
        return out[:threads]
    try: v = max(0,int(raw))
    except: v = 0
    return [v]*threads

def start_hotkey_listener():
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

# ---------- Email generator ----------
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

# ---------- Worker ----------
def worker(worker_id: int, loops_for_this_thread: int, use_edge: bool, win_size: str, win_pos: str):
    try: w,h = [int(x) for x in win_size.split(",")]
    except: w,h = 480,360
    try: x,y = [int(x) for x in win_pos.split(",")]
    except: x,y = 0,0

    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument(f"--window-size={w},{h}")
    for a in [
        "--disable-logging","--log-level=3","--no-default-browser-check",
        "--disable-background-networking","--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows","--disable-renderer-backgrounding",
        "--disable-notifications","--disable-speech-api","--mute-audio"
    ]: opts.add_argument(a)
    try:
        opts.add_experimental_option("excludeSwitches", ["enable-logging","enable-automation"])
        opts.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})
        opts.add_experimental_option('useAutomationExtension', False)
    except Exception: pass

    # Start driver (silence service logs); add timeouts to avoid stalls
    try:
        if use_edge:
            service = EdgeService(EdgeChromiumDriverManager().install(), log_path='NUL')
            driver  = webdriver.Edge(service=service, options=opts)
        else:
            service = ChromeService(ChromeDriverManager().install(), log_path='NUL')
            driver  = webdriver.Chrome(service=service, options=opts)
        try:
            driver.set_window_position(x,y)
            driver.set_page_load_timeout(20)
            driver.set_script_timeout(10)
        except Exception: pass
    except Exception as e:
        print(f"[T{worker_id}] ❌ Unable to start browser: {e}")
        return

    def login():
        if stop_event.is_set(): return False, None
        try:
            driver.get(LOGIN_URL)
        except WebDriverException as e:
            print(f"[T{worker_id}] nav error: {e}")
            return False, None
        rdelay(0.25,0.45)
        # poke any + to surface login
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Add Vote']")
            safe_click(driver, btn); rdelay(0.15,0.30)
        except NoSuchElementException:
            pass

        # email field?
        try:
            email_input = driver.find_element(By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]")
        except NoSuchElementException:
            try: driver.get(HUB_URL)
            except Exception: pass
            return True, None

        addr = gen_email()
        try: email_input.clear(); email_input.send_keys(addr)
        except WebDriverException: return False, None

        # click Log in
        logged = False
        for _ in range(40):
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
            time.sleep(0.05)
        if not logged:
            print(f"[T{worker_id}] ⚠️ could not click Log in"); return False, None

        # wait for email form to disappear
        try:
            WebDriverWait(driver, 8).until_not(
                EC.presence_of_element_located((By.XPATH, "//input[@type='email' or starts-with(@id,'field-:')]"))
            )
        except TimeoutException:
            print(f"[T{worker_id}] ⚠️ email form stuck"); return False, None

        try: driver.get(HUB_URL)
        except Exception: pass
        return True, addr

    def open_section():
        if stop_event.is_set(): return False
        btn = wait_css(driver, CATEGORY_ID, 6)
        if not btn: return False
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", btn)
        time.sleep(random.uniform(0.15,0.25))
        if (btn.get_attribute("aria-expanded") or "").lower() != "true":
            safe_click(driver, btn); time.sleep(random.uniform(0.20,0.35))
        return True

    # ---- voting core (fast + watchdog) ----
    def vote_jimin_only_and_count() -> int:
        if stop_event.is_set(): return 0
        if not open_section(): return 0
        try:
            artist_node = WebDriverWait(driver,6).until(
                EC.presence_of_element_located((By.XPATH, ARTIST_X_H3))
            )
        except TimeoutException:
            return 0
        try:
            driver.execute_script("arguments[0].scrollIntoView({behavior:'instant', block:'center'});", artist_node)
            time.sleep(0.08)
        except Exception: pass

        # get + button and counter from the card
        def find_btn_and_counter():
            js = """
            const start = arguments[0];
            let node = start;
            for (let i = 0; i < 8 && node; i++) {
              const plus = node.querySelector ? node.querySelector('button[aria-label="Add Vote"]') : null;
              if (plus) {
                // try to find the big numeric counter in the same card
                const scope = node.closest('section,article,div') || node;
                const cand = scope.querySelectorAll('span,div,p,strong');
                let counter = null;
                for (const el of cand) {
                  const t = (el.textContent || '').trim();
                  if (/^\\d+$/.test(t)) { counter = el; break; }
                }
                return [plus, counter];
              }
              node = node.nextElementSibling || node.parentElement;
            }
            return [null,null];
            """
            return driver.execute_script(js, artist_node)

        plus_el, counter_el = find_btn_and_counter()
        if not plus_el: return 0

        def submit_modal_visible():
            try:
                modals = driver.find_elements(By.XPATH, "//*[@role='dialog' or contains(@id,'chakra-modal')]")
                for m in modals:
                    txt = (m.text or "").lower()
                    if ("distributed" in txt or "you have" in txt) and "vote" in txt:
                        for b in m.find_elements(By.XPATH, ".//button"):
                            t = (b.text or "").strip().lower()
                            if any(k in t for k in ("submit","confirm","continue")):
                                return True
            except Exception: pass
            return False

        def read_card_count():
            try:
                if not counter_el: return None
                s = driver.execute_script(
                    "return (arguments[0] && arguments[0].textContent) ? arguments[0].textContent.trim() : '';",
                    counter_el
                )
                m = re.search(r"\d+", s or "")
                return int(m.group(0)) if m else None
            except Exception:
                return None

        current = read_card_count()
        real_votes = 0
        disabled_streak = 0
        requery_count  = 0
        reopen_count   = 0

        while True:
            if stop_event.is_set(): break
            if submit_modal_visible(): break

            aria = (plus_el.get_attribute("aria-disabled") or "").lower()
            if aria == "true" or plus_el.get_attribute("disabled") is not None:
                disabled_streak += 1
                if disabled_streak > DISABLED_PATIENCE: break
                time.sleep(0.03)
                continue
            disabled_streak = 0

            if not safe_click(driver, plus_el): break
            time.sleep(random.uniform(CLICK_MIN, CLICK_MAX))

            base = current
            confirmed = False
            for _ in range(CONFIRM_POLL_STEPS):
                time.sleep(CONFIRM_POLL_MS/1000.0)
                now = read_card_count()
                if now is not None and base is not None and now > base:
                    delta = now - base
                    real_votes += delta
                    current = now
                    confirmed = True
                    requery_count = 0
                    break

            if not confirmed:
                requery_count += 1
                # re-find button+counter if a few clicks didn't confirm
                if requery_count >= STALL_REQUERY_LIMIT:
                    plus_el, counter_el = find_btn_and_counter()
                    requery_count = 0
                    if not plus_el:
                        reopen_count += 1
                        if reopen_count > STALL_REOPEN_LIMIT:
                            break
                        # reopen accordion and try again
                        if not open_section(): break
                        plus_el, counter_el = find_btn_and_counter()
                        if not plus_el: break

            if real_votes >= SAFETY_CEILING: break

        # Submit if modal present
        def click_submit():
            MODAL = "//*[@role='dialog' or contains(@id,'chakra-modal')]"
            for xp, to in [
                (MODAL+"//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']", 4),
                (MODAL+"//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]", 3)
            ]:
                try:
                    btn = WebDriverWait(driver, to, poll_frequency=0.05).until(
                        EC.element_to_be_clickable((By.XPATH, xp))
                    )
                    driver.execute_script("arguments[0].click();", btn)
                    return True
                except Exception: pass
            return False

        if submit_modal_visible(): click_submit()
        return real_votes

    def logout_and_wait():
        if stop_event.is_set(): return
        # short pause so the UI settles
        time.sleep(random.uniform(*PAUSE_AFTER_SUBMIT))
        try:
            b = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log out')]")
            safe_click(driver, b)
        except NoSuchElementException:
            try:
                driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
                driver.get(HUB_URL)
            except Exception: pass
        # short human pause
        time.sleep(random.uniform(*HUMAN_PAUSE_BETWEEN))

    # ----- per-thread loop -----
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
            print(f"[T{worker_id}] ✅ Votes submitted — loop {loops_label} | {email or 'N/A'}")

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
        print(f"🧮 Total votes (all threads): {_global_submit_count}")
        print(f"🏁 All threads finished in {fmt_elapsed(finish_clock - start_clock)}")

    except Exception:
        import traceback
        print("\n=== FATAL ERROR ===")
        traceback.print_exc()
    finally:
        _pause_exit()
        sys.exit(0)
