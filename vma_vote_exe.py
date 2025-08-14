# vma_vote_exe.py — Stable flow + human-like delays + anti-throttle (tiny visible window)
# Changes in this version:
# - Strict modal detection (only inside role=dialog; no page-global 'Submit' fallback)
# - Robust Add Vote lookup relative to 'Jimin' text (works if button is before/after the name)
# - Flexible cap (10/20); waits through button debounce; per-thread totals; any-key stop (starts after prompts)
# - Silenced browser logs; elapsed time auto-formatted

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
# Go straight to the category page (as in your screenshot).
VOTE_URL = "https://www.mtv.com/event/vma/vote/best-k-pop"
CATEGORY_ID = "#accordion-button-best-k-pop"  # kept for backward compatibility; missing on category page

# Match 'Jimin' in several tag types
ARTIST_X = (
    "//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::p or self::span]"
    "[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'jimin')]"
)

MAX_THREADS = 100
MAX_VOTES_PER_LOOP_SAFETY = 20  # hard safety ceiling; site cap is ~10 or 20

# ---------- Globals ----------
_global_submit_count = 0
_submit_lock = threading.Lock()

# stop-hotkey
try:
    import msvcrt  # Windows console single-key
except Exception:
    msvcrt = None
stop_event = threading.Event()

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

def parse_loops_input(raw: str, threads: int):
    raw = (raw or "").strip()
    if not raw:
        return [0] * threads  # default: infinite
    if "," in raw:
        parts = []
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
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
        try:
            v = max(0, int(raw))
        except:
            v = 0
        return [v] * threads

def fmt_elapsed(s: float) -> str:
    if s >= 3600: return f"{s / 3600:.2f} hr"
    if s >= 60:   return f"{s / 60:.2f} min"
    return f"{s:.1f} sec"

def start_hotkey_listener():
    """Press ANY key (or Enter on non-Windows) to stop all threads."""
    def _listen():
        try:
            if msvcrt:
                msvcrt.getch()
            else:
                input()
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
LAST = ["smith","johnson","gons","brown","jones","miller","davis","garcia","mini","martinez",
        "hiu","lopez","gonzalez","kim","anderson","thomas","moisr","moore","jackson","martin",
        "lee","thompson","lim","harris","sanchez","clark","romz","lewis","robinson","walker"]
DOMAINS = ["gmail.com","outlook.com","yahoo.com","icloud.com","aol.com"]

def gen_email():
    fn = random.choice(FIRST)
    ln = random.choice(LAST)
    num = random.randint(1000, 99999)
    return f"{fn}{ln}{num}@{random.choice(DOMAINS)}".lower()

# ---------- Core flow (per thread) ----------
def worker(worker_id: int, loops_for_this_thread: int, use_edge: bool, win_size: str, win_pos: str):
    # Parse size/pos args
    try: w, h = [int(x) for x in win_size.split(",")]
    except Exception: w, h = 480, 360
    try: x, y = [int(x) for x in win_pos.split(",")]
    except Exception: x, y = 0, 0

    # Options: visible small window, anti-throttle, silence logs
    opts = EdgeOptions() if use_edge else ChromeOptions()
    opts.add_argument(f"--window-size={w},{h}")
    for a in [
        "--disable-logging", "--log-level=3",
        "--no-default-browser-check", "--disable-background-networking",
        "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]:
        opts.add_argument(a)
    try:
        opts.add_experimental_option("excludeSwitches", ["enable-logging","enable-automation"])
        opts.add_experimental_option('useAutomationExtension', False)
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
        try:
            driver.set_window_position(x, y)
        except Exception:
            pass
    except Exception as e:
        print(f"[T{worker_id}] ❌ Unable to start browser: {e}")
        return

    def login():
        if stop_event.is_set(): return False, None
        try:
            driver.get(VOTE_URL)
        except WebDriverException as e:
            print(f"[T{worker_id}] nav error: {e}")
            return False, None

        rdelay(0.6, 1.0)
        if stop_event.is_set(): return False, None

        # Try to trigger login by clicking any "Add Vote" if present
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Add Vote']")
            safe_click(driver, btn); rdelay(0.4, 0.7)
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
        for _ in range(50):
            if stop_event.is_set(): return False, None
            try:
                btn = driver.find_element(By.XPATH, "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='log in']")
            except NoSuchElementException:
                try:
                    btn = driver.find_element(By.XPATH, "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'log in')]")
                except NoSuchElementException:
                    btn = None
            if btn and safe_click(driver, btn):
                logged = True; break
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
        # If accordion exists (old layout), open it; else treat as already open
        btn = driver.find_elements(By.CSS_SELECTOR, CATEGORY_ID)
        if not btn: return True
        btn = btn[0]
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", btn)
        time.sleep(random.uniform(0.30, 0.50))
        if (btn.get_attribute("aria-expanded") or "").lower() != "true":
            safe_click(driver, btn); time.sleep(random.uniform(0.50, 0.80))
        return True

    def vote_jimin_only_and_count() -> int:
        if stop_event.is_set(): return 0
        if not open_section(): return 0

        # Locate the 'Jimin' node on category page
        try:
            artist_node = WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.XPATH, ARTIST_X))
            )
        except TimeoutException:
            return 0

        try:
            driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", artist_node)
            time.sleep(0.2)
        except Exception:
            pass

        # ---- FIX #2: robust Add Vote lookup (nearest ancestor that contains both name + button)
        try:
            add_btn = driver.find_element(
                By.XPATH,
                "("
                "(" + ARTIST_X + ")/ancestor-or-self::*[.//button[@aria-label='Add Vote']]"
                ")[1]//button[@aria-label='Add Vote'][1]"
            )
        except NoSuchElementException:
            # last resort: any visible Add Vote button
            try:
                add_btn = driver.find_element(By.XPATH, "//button[@aria-label='Add Vote' and not(@disabled)]")
            except NoSuchElementException:
                return 0

        # Nearby counter to detect stagnation
        try:
            counter_el = driver.find_element(By.XPATH, f"({ARTIST_X}/following::p[contains(@class,'chakra-text')])[1]")
        except NoSuchElementException:
            counter_el = None

        # ---- FIX #1: strict modal detection (dialog only; no page-global Submit)
        def submit_modal_visible_and_cap():
            """
            True only when the real vote-complete modal is open.
            Looks inside role=dialog / chakra modal; never checks the page chrome.
            """
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

        real_votes = 0
        last_count = -1
        stagnant = 0
        dynamic_cap = MAX_VOTES_PER_LOOP_SAFETY
        disabled_streak = 0

        while True:
            if stop_event.is_set(): break

            vis, cap = submit_modal_visible_and_cap()
            if cap: dynamic_cap = min(dynamic_cap, cap)
            if vis: break

            aria = (add_btn.get_attribute("aria-disabled") or "").lower()
            if aria == "true" or add_btn.get_attribute("disabled") is not None:
                # wait through debounce instead of quitting immediately
                disabled_streak += 1
                if disabled_streak > 60:  # ~3–4s total wait
                    break
                time.sleep(0.06)
                continue
            disabled_streak = 0

            if not safe_click(driver, add_btn):
                break

            real_votes += 1
            time.sleep(random.uniform(0.12, 0.20))

            vis, cap = submit_modal_visible_and_cap()
            if cap: dynamic_cap = min(dynamic_cap, cap)
            if vis: break

            if counter_el:
                try:
                    txt = counter_el.text or ""
                    m = re.search(r"\d+", txt)
                    if m:
                        n = int(m.group(0))
                        if n == last_count:
                            stagnant += 1
                        else:
                            stagnant, last_count = 0, n
                        if stagnant >= 5:
                            break
                except StaleElementReferenceException:
                    pass

            if real_votes >= dynamic_cap: break
            if real_votes >= MAX_VOTES_PER_LOOP_SAFETY: break

        # Click Submit if present (dialog only)
        def click_submit_modal():
            MODAL_SCOPE = "//*[@role='dialog' or contains(@id,'chakra-modal')]"
            X_EQ  = MODAL_SCOPE + "//button[normalize-space(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']"
            X_HAS = MODAL_SCOPE + "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit')]"

            btn = None
            for xp, to in [(X_EQ, 6), (X_HAS, 4)]:
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
        return real_votes

    def logout_and_wait():
        if stop_event.is_set(): return
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
        time.sleep(random.uniform(2.5, 5.5))

    # --------- Main per-thread loop ----------
    current_loop = 0
    thread_total = 0
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

            # Show loop progress instead of votes total
            loops_label = f"{current_loop}/{'∞' if loops_for_this_thread == 0 else loops_for_this_thread}"
            print(f"[T{worker_id}] ✅ Submitted {added} vote(s) — loop {loops_label} | {email or 'N/A'}")


            logout_and_wait()

    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ---------- CLI ----------
def parse_args():
    parser = argparse.ArgumentParser(description=f"VMA voter (Selenium, multi-thread, max threads = {MAX_THREADS})")
    parser.add_argument("--threads", type=int, default=1, help=f"Number of parallel threads (max {MAX_THREADS})")
    parser.add_argument("--loops", type=int, default=1, help="Loops per thread (0 = infinite) — overridden by interactive input")
    parser.add_argument("--edge", action="store_true", help="Use Edge instead of Chrome")
    parser.add_argument("--win", default="480,360", help="Window size WxH (default 480,360)")
    parser.add_argument("--pos", default="0,0", help="Window position X,Y (default 0,0)")
    return parser.parse_args()

# ---------- Helper to pause before exit ----------
def _pause_exit():
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass

# ---------- Main ----------
if __name__ == "__main__":
    try:
        args = parse_args()

        # Prompt threads
        try:
            val = input(f"Threads (max {MAX_THREADS}) [{args.threads}]: ").strip()
            if val:
                args.threads = min(MAX_THREADS, max(1, int(val)))
        except Exception:
            pass

        threads  = min(MAX_THREADS, max(1, args.threads))
        use_edge = bool(args.edge)
        win_size = args.win
        win_pos  = args.pos

        # Prompt loops — single number OR comma-list per thread
        try:
            hint = f"{args.loops}"
            val = input(f"Loops per thread (0 = infinite). Single number OR comma-list for each thread [{hint}]: ").strip()
            loops_list = [max(0, args.loops)] * threads if not val else parse_loops_input(val, threads)
        except Exception:
            loops_list = [0] * threads

        # Start hotkey AFTER prompts so it doesn't steal input
        start_hotkey_listener()
        print("▶ Press ANY key to stop…")

        start_clock = time.time()
        print(f"▶ Starting {threads} thread(s); loops per thread = {loops_list}; browser={'Edge' if use_edge else 'Chrome'}; win={win_size}; pos={win_pos}")

        with ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(worker, i+1, loops_list[i], use_edge, win_size, win_pos) for i in range(threads)]
            for _ in as_completed(futs):
                pass

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
