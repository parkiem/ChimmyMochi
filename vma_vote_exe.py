# vma_vote_exe.py — cleaned output + per-thread x/y + smart elapsed time
# - Silences Chrome/Edge & driver logs to a temp file (deleted on exit)
# - Hides DevTools + voice_transcription warnings
# - Per-thread progress: loop x/y (or x/∞)
# - Smart elapsed (sec/min/hr)
# - Voting/login flow preserved from your test file

import time, random, re, sys, argparse, threading, tempfile, os, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- Cross-platform "press ANY key to stop" (quiet) ----------
stop_event = threading.Event()

_is_win = (os.name == 'nt')
if _is_win:
    try:
        import msvcrt  # Windows-only
    except Exception:
        msvcrt = None

def _posix_wait_for_keypress():
    import sys as _sys, termios as _termios, tty as _tty, select as _select
    try:
        fd = _sys.stdin.fileno()
        old = _termios.tcgetattr(fd)
    except Exception:
        return
    try:
        _tty.setcbreak(fd)
        while not stop_event.is_set():
            r, _, _ = _select.select([_sys.stdin], [], [], 0.05)
            if r:
                _sys.stdin.read(1)
                stop_event.set()
                break
    finally:
        try:
            _termios.tcsetattr(fd, _termios.TCSADRAIN, old)
        except Exception:
            pass

def _win_wait_for_keypress():
    if msvcrt is None:
        return
    while not stop_event.is_set():
        if msvcrt.kbhit():
            msvcrt.getch()
            stop_event.set()
            break
        time.sleep(0.05)

def start_key_watcher():
    # Print the notice ONCE, and start a background watcher if we have a TTY
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
    except Exception:
        return
    print("▶ Press ANY key to stop...", flush=True)
    t = threading.Thread(
        target=_win_wait_for_keypress if _is_win else _posix_wait_for_keypress,
        daemon=True
    )
    t.start()
# --------------------------------------------------------------------

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
MAX_THREADS = 50  # hard cap

# ---------- Globals ----------
_global_submit_count = 0
_submit_lock = threading.Lock()
_counter_lock = threading.Lock()
_global_vote_no = 0

# NEW: track unique successful login emails
_successful_logins = set()
_login_lock = threading.Lock()

# paths to temp log(s) so we can delete on exit
_temp_driver_log = None
_temp_chrome_log = None

# ---------- Helper to pause before exit ----------
def _pause_exit():
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass

# ---------- CLI ----------
def parse_args():
    p = argparse.ArgumentParser(description=f"VMA voter (Selenium, multi-thread, max threads = {MAX_THREADS})")
    p.add_argument("--threads", type=int, default=1, help=f"Number of parallel threads (max {MAX_THREADS})")
    p.add_argument("--loops",   type=int, default=1, help="Loops per thread (0 = infinite)")
    p.add_argument("--edge", action="store_true", help="Use Edge instead of Chrome")
    p.add_argument("--win",  default="480,360", help="Window size WxH (default 480,360)")
    p.add_argument("--pos",  default="0,0",    help="Window position X,Y (default 0,0)")
    return p.parse_args()

# ---------- Small utils ----------
def rdelay(a=0.08, b=0.16):
    time.sleep(random.uniform(a, b))

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

def next_vote_no() -> int:
    global _global_vote_no
    with _counter_lock:
        _global_vote_no += 1
        return _global_vote_no

def fmt_elapsed(s: float) -> str:
    if s >= 3600:
        return f"{s/3600:.2f} hr"
    if s >= 60:
        return f"{s/60:.2f} min"
    return f"{s:.1f} sec"

# ---------- Real-name email generator (unchanged) ----------
FIRST = ["john","michael","sarah","emily","david","chris","anna","lisa","mark","paul","james","laura",
         "peter","susan","robert","nancy","kevin","mary","brian","julia","alex","joshua","olivia","matthew",
         "daniel","jennifer","thomas","andrew","stephanie","karen","tyler","nicole","heather","eric","amanda",
         "ryan","brandon","rachel","jason","patrick","victoria","kimberly","melissa","ashley","brittany","helen",
         "timothy","catherine","dennis","jacob","ethan","zoe","nathan","grace","henry","noah","ava","mia",
         "isabella","sophia"]
LAST = ["smith","johnson","wil","brown","jones","moi","davis","garcia","rod","martinez",
        "huem","lop","gon","wilson","son","thomas","kim","moore","park","martin",
        "lee","thompson","white","har","sanchez","clark","ramirez","lewis","robin","walker"]
DOMAINS = ["gmail.com","outlook.com","yahoo.com","icloud.com","aol.com"]
def gen_email():
    fn = random.choice(FIRST); ln = random.choice(LAST); num = random.randint(100, 9999)
    return f"{fn}{ln}{num}@{random.choice(DOMAINS)}".lower()

# ---------- Worker ----------
def worker(worker_id: int, loops: int, use_edge: bool, win_size: str, win_pos: str):
    global _temp_driver_log, _temp_chrome_log

    # Parse size/pos args
    try:   w, h = [int(x) for x in win_size.split(",")]
    except: w, h = 480, 360
    try:   x, y = [int(x) for x in win_pos.split(",")]
    except: x, y = 0, 0

    # create temp files for logs (deleted at the end)
    drv_log = tempfile.NamedTemporaryFile(prefix="drv_", suffix=".log", delete=False)
    _temp_driver_log = drv_log.name
    chrome_log = tempfile.NamedTemporaryFile(prefix="chr_", suffix=".log", delete=False)
    _temp_chrome_log = chrome_log.name
    drv_log.close(); chrome_log.close()

    # Options: visible small window, anti-throttle flags, silence noisy subsystems
    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument(f"--window-size={w},{h}")

    # Send Chromium logs to a file (and keep stdout/stderr quiet)
    # NOTE: excludeSwitches removes the "DevTools listening" banner
    for a in [
        "--disable-logging",
        "--log-level=3",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-notifications",
        "--disable-speech-api",
        "--mute-audio",
        "--disable-features=AudioServiceOutOfProcess,MediaSessionService,CalculateNativeWinOcclusion,Translate",
        f"--log-file={_temp_chrome_log}",
        "--v=0",
    ]:
        opts.add_argument(a)

    try:
        # Hide DevTools banner + reduce stderr chatter
        opts.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
        opts.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})
    except Exception:
        pass

    # Selenium 4 Service — route chromedriver/msedgedriver logs to file
    try:
        if use_edge:
            service = EdgeService(EdgeChromiumDriverManager().install(), log_path=_temp_driver_log)
            driver = webdriver.Edge(service=service, options=opts)
        else:
            service = ChromeService(ChromeDriverManager().install(), log_path=_temp_driver_log)
            driver = webdriver.Chrome(service=service, options=opts)

        try:
            driver.set_window_position(x, y)
        except Exception:
            pass
    except Exception as e:
        print(f"[T{worker_id}] ❌ Unable to start browser: {e}")
        # cleanup logs on failure
        for p in (_temp_driver_log, _temp_chrome_log):
            try: os.remove(p)
            except Exception: pass
        return

    def login():
        try:
            driver.get(VOTE_URL)
        except WebDriverException as e:
            print(f"[T{worker_id}] nav error: {e}")
            return False, None

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

        # --- CLICK ADD VOTE (your logic preserved) ---
        for _ in range(22):
            try:
                driver.execute_script("arguments[0].click();", add_btn)
            except WebDriverException:
                pass
            time.sleep(random.uniform(0.08, 0.18))

        # --- SUBMIT (modal only) ---
        def click_submit_modal():
            MODAL_SCOPE = "//*[@role='dialog' or contains(@id,'chakra-modal')]"
            X_EQ  = MODAL_SCOPE + "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']"
            X_HAS = MODAL_SCOPE + "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]"

            btn = None
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

    def logout_and_wait():
        time.sleep(random.uniform(1.0, 1.8))
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
        time.sleep(random.uniform(2.0, 3.5))

    # --------- Main per-thread loop ----------
    current_loop = 0
    try:
        while True:
            if stop_event.is_set():
                break
            if loops != 0 and current_loop >= loops:
                break
            current_loop += 1

            if stop_event.is_set():
                break
            ok, email = login()
            if not ok:
                print(f"[T{worker_id}] ⚠️ login failed"); break

            # NEW: record successful login email (only if an email was used)
            if email:
                with _login_lock:
                    _successful_logins.add(email)

            if stop_event.is_set():
                break
            if not vote_jimin_only():
                print(f"[T{worker_id}] ⚠️ vote failed"); break

            # (keep your existing vote counter in case you still want it)
            with _submit_lock:
                global _global_submit_count
                _global_submit_count += 20  # keep your existing accounting

            # per-thread loop progress x/y (or x/∞)
            loops_label = f"{current_loop}/{'∞' if loops == 0 else loops}"
            print(f"[T{worker_id}] ✅ Votes Submitted — loops # {loops_label} | {email or 'N/A'}")

            logout_and_wait()
            if stop_event.is_set():
                break

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        # remove temp logs
        for p in (_temp_driver_log, _temp_chrome_log):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

# ---------- Main ----------
if __name__ == "__main__":
    try:
        args = parse_args()

        # prompts
        try:
            v = input(f"Threads (max {MAX_THREADS}) [{args.threads}]: ").strip()
            if v:
                args.threads = min(MAX_THREADS, max(1, int(v)))
        except Exception:
            pass

        try:
            v = input(f"Loops per thread (0 = infinite) [{args.loops}]: ").strip()
            if v:
                args.loops = max(0, int(v))
        except Exception:
            pass

        threads  = min(MAX_THREADS, max(1, args.threads))
        loops    = max(0, args.loops)
        use_edge = bool(args.edge)
        win_size = args.win
        win_pos  = args.pos

        start_clock = time.time()
        print(f"▶ Starting {threads} thread(s); loops per thread = {loops or '∞'}; "
              f"browser={'Edge' if use_edge else 'Chrome'}; win={win_size}; pos={win_pos}")

        start_key_watcher()
        try:
            with ThreadPoolExecutor(max_workers=threads) as ex:
                futs = [ex.submit(worker, i+1, loops, use_edge, win_size, win_pos) for i in range(threads)]
                for _ in as_completed(futs):
                    if stop_event.is_set():
                        break
        except KeyboardInterrupt:
            stop_event.set()

        finish_clock = time.time()
        print(f"🕒 Started : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_clock))}")
        print(f"🕒 Finished: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finish_clock))}")
        # NEW: print unique successful login count
        print(f"🧮  Total successful logins (all threads): {len(_successful_logins)}")
        # (optional: keep this line too if you still want to see total votes)
        # print(f"🧮 Total votes (all threads): {_global_submit_count}")
        print(f"🏁 All threads finished in {fmt_elapsed(finish_clock - start_clock)}")

    except Exception:
        import traceback
        print("\n=== FATAL ERROR ===")
        traceback.print_exc()
    finally:
        # Save successful logins to a text file with a header
        from datetime import datetime

        report_path = "successful_logins.txt"
        run_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        count = len(_successful_logins)
        
        try:
            # Add a blank line if the file already has content (so runs are separated)
            needs_gap = os.path.exists(report_path) and os.path.getsize(report_path) > 0            
            with open(report_path, "a", encoding="utf-8") as f:
                if needs_gap:
                    f.write("\n")
                f.write(f"=== {run_ts} | {count} emails ===\n")               
                for email in sorted(_successful_logins):
                    f.write(email + "\n")
            print(f"✅ Saved {count} successful logins to {report_path}")
        except Exception as e:
            print(f"⚠️ Could not save login list: {e}")

        _pause_exit()
        sys.exit(0)
