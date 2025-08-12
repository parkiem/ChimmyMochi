# vma_vote_exe.py — Jimin-only, multi-thread (max 5), timing, loops prompt, submit modal auto-click
# Examples:
#   vma_vote_exe.exe                        -> prompts for loops, 1 thread
#   vma_vote_exe.exe --threads 3 --loops 10 -> 3 browsers in parallel, 10 loops each
#   vma_vote_exe.exe --threads 5 --loops 0  -> 5 browsers, infinite loops

import time, random, sys, re, argparse, threading
from typing import Set
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, ElementClickInterceptedException,
    StaleElementReferenceException, WebDriverException
)

from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions

VOTE_URL = "https://www.mtv.com/event/vma/vote/"

# ----- Global vote counter (resets each run) -----
_counter_lock = threading.Lock()
_global_vote_no = 0
def next_vote_no() -> int:
    global _global_vote_no
    with _counter_lock:
        _global_vote_no += 1
        return _global_vote_no

# ----- Email generator: first.last####@domain -----
FIRST_NAMES = [
    "john","michael","sarah","emily","david","chris","anna","lisa","mark","paul",
    "james","laura","peter","susan","robert","nancy","kevin","mary","brian","julia",
    "alex","joshua","olivia","matthew","daniel","jennifer","thomas","andrew","stephanie","karen",
    "tyler","nicole","heather","eric","amanda","ryan","brandon","rachel","jason","patrick",
    "victoria","kimberly","melissa","ashley","brittany","helen","timothy","catherine","dennis","jacob",
    "ethan","zoe","nathan","grace","henry","noah","ava","mia","isabella","sophia",
    "oliver","liam","charlotte","amelia","ella","harper","scarlett","lily","abigail","aubrey"
]
LAST_NAMES = [
    "smith","johnson","williams","brown","jones","miller","davis","garcia","rodriguez","wilson",
    "martinez","anderson","taylor","thomas","hernandez","moore","martin","lee","perez","thompson",
    "white","harris","sanchez","clark","ramirez","lewis","robinson","walker","young","allen",
    "king","wright","scott","torres","nguyen","hill","flores","green","adams","nelson",
    "baker","hall","rivera","campbell","mitchell","carter","roberts","gomez","phillips","evans",
    "turner","parker","edwards","collins","stewart","morris","murphy","cook","rogers","reed"
]
DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "aol.com", "icloud.com", "hotmail.com"]

def generate_unique_email(used: Set[str]) -> str:
    while True:
        first = random.choice(FIRST_NAMES)
        last  = random.choice(LAST_NAMES)
        num   = random.randint(1000, 9999)  # 4 digits
        email = f"{first}.{last}{num}@{random.choice(DOMAINS)}"
        if email not in used:
            used.add(email)
            return email

# ----- Small helpers -----
def rdelay(a: float, b: float):
    time.sleep(random.uniform(a, b))

def safe_click(driver, el) -> bool:
    try:
        el.click()
        return True
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        try:
            driver.execute_script("arguments[0].click()", el)
            return True
        except Exception:
            return False

def wait_css(driver, css, timeout=15):
    try:
        return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR, css)))
    except TimeoutException:
        return None

def find_first_text_button(driver, *texts):
    for t in texts:
        try:
            el = WebDriverWait(driver, 1).until(
                EC.presence_of_element_located((By.XPATH,
                    f"//button[translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')="
                    f"translate('{t}','ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')]"))
            )
            return el
        except TimeoutException:
            continue
    return None

def click_submit_modal(driver, tries=60, gap=0.15):
    """
    Click the yellow 'Submit' button in the 'You have distributed all 10 votes' modal.
    Looks by visible text and common variants, with fallbacks.
    """
    xpath_text_submit = "//button[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'))='submit']"
    xpath_any_submit_like = ("//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit') "
                             "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'confirm') "
                             "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'continue')]")
    for _ in range(tries):
        for xp in (xpath_text_submit, xpath_any_submit_like):
            els = driver.find_elements(By.XPATH, xp)
            for el in els:
                try:
                    if el.is_enabled():
                        if safe_click(driver, el):
                            return True
                except Exception:
                    pass
        time.sleep(gap)
    return False

def make_driver(headless: bool):
    # Try Chrome first, fallback to Edge
    try:
        co = ChromeOptions()
        if headless: co.add_argument("--headless=new")
        co.add_argument("--disable-blink-features=AutomationControlled")
        co.add_argument("--disable-features=IsolateOrigins,site-per-process")
        co.add_argument("--start-maximized")
        return webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=co)
    except Exception:
        eo = EdgeOptions()
        if headless: eo.add_argument("--headless=new")
        eo.add_argument("--disable-blink-features=AutomationControlled")
        eo.add_argument("--disable-features=IsolateOrigins,site-per-process")
        eo.add_argument("--start-maximized")
        return webdriver.Edge(service=EdgeService(EdgeChromiumDriverManager().install()), options=eo)

# ----- Worker (per browser) -----
def worker(worker_id: int, loops: int, headless: bool):
    used_emails: Set[str] = set()
    current_loop = 0
    driver = None
    try:
        time.sleep(random.uniform(0.2, 1.2))  # stagger start
        driver = make_driver(headless)
        driver.get(VOTE_URL)
        print(f"[T{worker_id}] ▶ started")

        def login():
            add_btn = wait_css(driver, 'button[aria-label="Add Vote"]', 15)
            if not add_btn:
                print(f"[T{worker_id}] ⚠️ Add Vote not found")
                return False, None
            rdelay(0.5, 1.5)
            safe_click(driver, add_btn)

            rdelay(0.8, 1.6)
            try:
                email_input = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[id^="field-:"]'))
                )
            except TimeoutException:
                print(f"[T{worker_id}] ⚠️ Email input not found")
                return False, None

            email = generate_unique_email(used_emails)
            email_input.click(); email_input.clear(); email_input.send_keys(email)
            rdelay(0.4, 0.9)

            btn = find_first_text_button(driver, "Log in", "log in")
            if btn:
                safe_click(driver, btn)
            rdelay(1.2, 2.0)
            return True, email

        def open_section():
            btn = wait_css(driver, "#accordion-button-best-k-pop", 5)
            if not btn:
                return False
            driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", btn)
            rdelay(0.8, 1.5)
            if (btn.get_attribute("aria-expanded") or "").lower() != "true":
                safe_click(driver, btn); rdelay(1.0, 1.6)
            return True

        def vote_jimin_only():
            if not open_section(): return False
            # Jimin header
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, "//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']"))
                )
            except TimeoutException:
                return False
            # nearest Add Vote after Jimin header
            try:
                btn = driver.find_element(By.XPATH, "(//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']/following::button[@aria-label='Add Vote'])[1]")
            except NoSuchElementException:
                return False

            # Optional nearby counter (for stagnation)
            display = None
            try:
                display = driver.find_element(By.XPATH, "(//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']/following::p[contains(@class,'chakra-text')])[1]")
            except NoSuchElementException:
                pass

            last_count = -1; stagnant = 0
            for _ in range(random.randint(120, 160)):
                aria_dis = (btn.get_attribute("aria-disabled") or "").lower()
                if aria_dis == "true": break
                safe_click(driver, btn)
                rdelay(0.09, 0.16)
                if display:
                    try:
                        txt = display.text or ""
                        m = re.search(r"\d+", txt)
                        if m:
                            n = int(m.group(0))
                            if n == last_count: stagnant += 1
                            else: stagnant, last_count = 0, n
                            if stagnant >= 5: break
                    except StaleElementReferenceException:
                        pass

            # Submit modal (robust)
            if not click_submit_modal(driver):
                # very generic fallbacks
                def click_when_ready(xpath, tries=40, gap=0.15):
                    for _ in range(tries):
                        try:
                            el = driver.find_element(By.XPATH, xpath)
                            if el and el.is_enabled():
                                if safe_click(driver, el): return True
                        except NoSuchElementException:
                            pass
                        time.sleep(gap)
                    return False
                click_when_ready("//button[@type='button' and not(@disabled)]")
                click_when_ready("//button[contains(@class,'chakra-button') and not(@disabled)]")
            return True

        def logout_and_wait():
            rdelay(1.5, 2.5)
            logout_btn = None
            try:
                logout_btn = driver.find_element(By.CSS_SELECTOR, "button.chakra-button.AuthNav__login-btn.css-ki1yvo")
            except NoSuchElementException:
                try:
                    logout_btn = driver.find_element(By.XPATH, "//*[@id='root']//main//button[contains(.,'Log out') or contains(.,'Logout')]")
                except NoSuchElementException:
                    pass
            if logout_btn:
                safe_click(driver, logout_btn)
            gap = random.randint(1000, 3000) + random.randint(2000, 3000)
            time.sleep(gap/1000.0)

        while True:
            if loops != 0 and current_loop >= loops:
                break
            current_loop += 1

            ok, email = login()
            if not ok or not email:
                print(f"[T{worker_id}] ⚠️ login failed"); break
            if not vote_jimin_only():
                print(f"[T{worker_id}] ⚠️ vote failed"); break

            vno = next_vote_no()
            print(f"[T{worker_id}] Vote #{vno} : {email}")
            logout_and_wait()

    finally:
        try:
            if driver: driver.quit()
        except Exception:
            pass

# ----- CLI / entry -----
def parse_args():
    p = argparse.ArgumentParser(description="Jimin-only VMA voter (multi-threaded)")
    p.add_argument("--threads", type=int, default=1, help="browsers in parallel (max 5)")
    p.add_argument("--loops",   type=int, default=None, help="loops per thread (0 = infinite). If omitted, you will be prompted.")
    p.add_argument("--headless", action="store_true", help="run without visible browser windows")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()

    # Cap threads at 5
    threads = max(1, min(5, args.threads or 1))

    # Prompt for loops if not provided
    if args.loops is None:
        try:
            raw = input("Loops per thread (0 = infinite): ").strip()
            loops = int(raw or "0")
        except Exception:
            loops = 0
    else:
        loops = max(0, args.loops)

    headless = bool(args.headless)

    start = time.time()
    print(f"▶ Starting {threads} thread(s); loops per thread = {loops or '∞'}; headless={headless}")

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(worker, i+1, loops, headless) for i in range(threads)]
        for _ in as_completed(futs):
            pass

    elapsed = time.time() - start
    print(f"🏁 All threads finished in {elapsed:.1f}s")
    sys.exit(0)
