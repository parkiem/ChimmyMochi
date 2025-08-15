import time, random, re, sys, argparse, threading, tempfile, os, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- Cross-platform "press ANY key to stop" ----------
stop_event = threading.Event()

if os.name == 'nt':
    try:
        import msvcrt
        _WIN = True
    except Exception:
        _WIN = False
else:
    _WIN = False

def _posix_wait_for_keypress():
    import sys as _sys, termios as _termios, tty as _tty, select as _select
    fd = _sys.stdin.fileno()
    try:
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
        _termios.tcsetattr(fd, _termios.TCSADRAIN, old)

def _win_wait_for_keypress():
    while not stop_event.is_set():
        if msvcrt.kbhit():
            msvcrt.getch()
            stop_event.set()
            break
        time.sleep(0.05)

def start_key_watcher():
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
    except Exception:
        return
    t = threading.Thread(
        target=_win_wait_for_keypress if _WIN else _posix_wait_for_keypress,
        daemon=True
    )
    t.start()

# ---------- Status ticker ----------
_status_ticker_event = threading.Event()
def fmt_elapsed_compact(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    if m:
        return f"{m}m"
    return "0m"

def start_status_ticker(start_ts: float, threads: int, loops: int):
    def _tick():
        while not _status_ticker_event.is_set():
            elapsed = fmt_elapsed_compact(time.time() - start_ts)
            loops_label = "∞" if loops == 0 else str(loops)
            sys.stdout.write(
                f"\r▶ Press ANY key to stop... | elapsed {elapsed} | threads={threads} | loops/thread={loops_label}   "
            )
            sys.stdout.flush()
            time.sleep(2.0)
        sys.stdout.write("\r⏹ Stopping... letting threads exit gracefully.                                   \n")
        sys.stdout.flush()
    threading.Thread(target=_tick, daemon=True).start()

# ---------- Selenium & Drivers ----------
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException, StaleElementReferenceException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

# ---------- Config ----------
VOTE_URL = "https://www.mtv.com/vma/vote"
CATEGORY_ID = "#accordion-button-best-k-pop"
ARTIST_X_H3 = "//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']"
MAX_THREADS = 50

# ---------- Globals ----------
_global_submit_count = 0
_submit_lock = threading.Lock()
_counter_lock = threading.Lock()
_global_vote_no = 0
_successful_logins = set()
_login_lock = threading.Lock()
_temp_driver_log = None
_temp_chrome_log = None

# ---------- Helper to pause ----------
def _pause_exit():
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass

# ---------- CLI ----------
def parse_args():
    p = argparse.ArgumentParser(description=f"VMA voter (Selenium, multi-thread, max threads = {MAX_THREADS})")
    p.add_argument("--threads", type=int, default=1)
    p.add_argument("--loops",   type=int, default=1)
    p.add_argument("--edge", action="store_true")
    p.add_argument("--win",  default="480,360")
    p.add_argument("--pos",  default="0,0")
    return p.parse_args()

# ---------- Utils ----------
def rdelay(a=0.08, b=0.16): time.sleep(random.uniform(a, b))
def safe_click(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", el)
        time.sleep(0.01)
        el.click(); return True
    except (ElementClickInterceptedException, WebDriverException):
        try: driver.execute_script("arguments[0].click();", el); return True
        except WebDriverException: return False
def wait_css(driver, sel, timeout=8):
    try: return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
    except TimeoutException: return None
def quick_wait(driver, condition, timeout=6.0, poll=0.05):
    return WebDriverWait(driver, timeout, poll_frequency=poll).until(condition)
def next_vote_no():
    global _global_vote_no
    with _counter_lock:
        _global_vote_no += 1
        return _global_vote_no

# ---------- Email generator ----------
FIRST = ["john","michael","sarah","emily","david","chris","anna","lisa","mark","paul"]
LAST = ["smith","johnson","wil","brown","jones","moi","davis","garcia","rod","martinez"]
DOMAINS = ["gmail.com","outlook.com","yahoo.com","icloud.com","aol.com"]
def gen_email():
    return f"{random.choice(FIRST)}{random.choice(LAST)}{random.randint(100, 9999)}@{random.choice(DOMAINS)}".lower()

# ---------- Worker ----------
def worker(worker_id: int, loops: int, use_edge: bool, win_size: str, win_pos: str):
    global _temp_driver_log, _temp_chrome_log

    try:   w, h = [int(x) for x in win_size.split(",")]
    except: w, h = 480, 360
    try:   x, y = [int(x) for x in win_pos.split(",")]
    except: x, y = 0, 0

    drv_log = tempfile.NamedTemporaryFile(prefix="drv_", suffix=".log", delete=False)
    _temp_driver_log = drv_log.name
    chrome_log = tempfile.NamedTemporaryFile(prefix="chr_", suffix=".log", delete=False)
    _temp_chrome_log = chrome_log.name
    drv_log.close(); chrome_log.close()

    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument(f"--window-size={w},{h}")
    for a in [
        "--disable-logging","--log-level=3","--no-default-browser-check",
        "--disable-background-networking","--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows","--disable-renderer-backgrounding",
        "--disable-notifications","--disable-speech-api","--mute-audio",
        "--disable-features=AudioServiceOutOfProcess,MediaSessionService,CalculateNativeWinOcclusion,Translate",
        f"--log-file={_temp_chrome_log}","--v=0"
    ]:
        opts.add_argument(a)
    try:
        opts.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
        opts.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})
    except Exception: pass

    # Driver setup
    driver = None
    try:
        service = EdgeService(EdgeChromiumDriverManager().install()) if use_edge else ChromeService(ChromeDriverManager().install())
        driver = webdriver.Edge(service=service, options=opts) if use_edge else webdriver.Chrome(service=service, options=opts)

        # Your login, vote, logout functions here
        def login():
            # Implement actual login
            return True, gen_email()
        def vote_jimin_only():
            # 20 fast clicks
            for _ in range(20):
                if stop_event.is_set(): return False
                time.sleep(random.uniform(0.10, 0.15))  # reduced
                time.sleep(random.uniform(0.10, 0.15))  # reduced
                time.sleep(random.uniform(0.05, 0.08))  # reduced
            return True
        def logout_and_wait():
            time.sleep(random.uniform(0.5, 0.8))  # shortened

        current_loop = 0
        while True:
            if stop_event.is_set(): break
            if loops != 0 and current_loop >= loops: break
            current_loop += 1

            ok, email = login()
            if not ok: break
            if stop_event.is_set(): break
            if not vote_jimin_only(): break

            with _submit_lock:
                global _global_submit_count
                _global_submit_count += 20
            print(f"[T{worker_id}] ✅ Votes Submitted — loops # {current_loop}/{'∞' if loops==0 else loops} | {email}")

            logout_and_wait()
            if stop_event.is_set(): break

    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
        for pth in (_temp_driver_log, _temp_chrome_log):
            try:
                if pth and os.path.exists(pth): os.remove(pth)
            except Exception: pass

# ---------- Main ----------
if __name__ == "__main__":
    try:
        args = parse_args()
        try:
            v = input(f"Threads (max {MAX_THREADS}) [{args.threads}]: ").strip()
            if v: args.threads = min(MAX_THREADS, max(1, int(v)))
        except Exception: pass
        try:
            v = input(f"Loops per thread (0 = infinite) [{args.loops}]: ").strip()
            if v: args.loops = max(0, int(v))
        except Exception: pass

        threads  = min(MAX_THREADS, max(1, args.threads))
        loops    = max(0, args.loops)
        use_edge = bool(args.edge)
        win_size = args.win
        win_pos  = args.pos

        start_clock = time.time()
        print(f"▶ Starting {threads} thread(s); loops per thread = {loops or '∞'}; "
              f"browser={'Edge' if use_edge else 'Chrome'}; win={win_size}; pos={win_pos}")

        start_key_watcher()
        start_status_ticker(start_clock, threads, loops)

        try:
            with ThreadPoolExecutor(max_workers=threads) as ex:
                futs = [ex.submit(worker, i+1, loops, use_edge, win_size, win_pos) for i in range(threads)]
                for _ in as_completed(futs):
                    if stop_event.is_set():
                        break
        except KeyboardInterrupt:
            stop_event.set()

        finish_clock = time.time()
        _status_ticker_event.set()
        print(f"🕒 Started : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_clock))}")
        print(f"🕒 Finished: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(finish_clock))}")
        print(f"Elapsed   : {fmt_elapsed_compact(finish_clock - start_clock)}")
        print(f"Total votes submitted: {_global_submit_count}")
        print(f"Unique successful logins: {len(_successful_logins)}")

        # Save successful logins to a text file
        try:
            with open("successful_logins.txt", "a", encoding="utf-8") as f:
              for email in sorted(_successful_logins):
                f.write(email + "\n")
            print(f"✅ Saved {len(_successful_logins)} successful logins to successful_logins.txt")
        except Exception as e:
            print(f"⚠️ Could not save login list: {e}")


    finally:
        _pause_exit()
