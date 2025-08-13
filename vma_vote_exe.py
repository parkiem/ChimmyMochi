# vma_vote_exe.py — Jimin-only, multi-thread (max 20), flexible vote counting
# Shows actual votes per loop and per-thread totals; prints grand total at the end.
# Examples:
#   vma_vote_exe.exe
#   vma_vote_exe.exe --threads 3 --loops 10
#   vma_vote_exe.exe --threads 5 --loops 0 --headless

import time, random, sys, re, argparse
from typing import Set, Tuple
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
        num   = random.randint(100, 9999)  # 3 digits
        email = f"{first}{last}{num}@{random.choice(DOMAINS)}"
        if email not in used:
            used.add(email)
            return email

# ----- Helpers -----
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
    Click the 'Submit' button in the '10 votes distributed' modal by text or close variants.
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

# ---------- Flexible voting (returns *actual* votes added this loop) ----------
def vote_jimin_only(driver) -> int:
    """
    Returns the actual number of votes added this loop (0..10).
    """
    # Open K-pop accordion
    btn = wait_css(driver, "#accordion-button-best-k-pop", 5)
    if not btn:
        return 0
    driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", btn)
    rdelay(0.8, 1.5)
    if (btn.get_attribute("aria-expanded") or "").lower() != "true":
        safe_click(driver, btn); rdelay(1.0, 1.6)

    # Ensure Jimin visible
    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, "//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']"))
        )
    except TimeoutException:
        return 0

    # The Add Vote button near Jimin
    try:
        add_btn = driver.find_element(By.XPATH, "(//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']/following::button[@aria-label='Add Vote'])[1]")
    except NoSuchElementException:
        return 0

    # Optional nearby counter to detect stagnation
    try:
        counter_el = driver.find_element(By.XPATH, "(//h3[translate(normalize-space(.),'JIMIN','jimin')='jimin']/following::p[contains(@class,'chakra-text')])[1]")
    except NoSuchElementException:
        counter_el = None

    last_count = -1
    stagnant = 0
    real_votes = 0

    while True:
        aria_dis = (add_btn.get_attribute("aria-disabled") or "").lower()
        if aria_dis == "true":
            break

        if not safe_click(driver, add_btn):
            break

        real_votes += 1
        rdelay(0.09, 0.16)

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
                    if stagnant >= 3:
                        break
            except StaleElementReferenceException:
                pass

        if real_votes >= 10:  # MTV per-loop cap
            break

    # Try to click the modal's Submit if it appears
    click_submit_modal(driver)
    return real_votes

# ---------- Worker (returns: thread_id, per-thread total votes cast) ----------
def worker(thread_id: int, loops: int, headless: bool) -> Tuple[int, int]:
    used_emails: Set[str] = set()
    current_loop = 0
    thread_total = 0
    driver = None
    try:
        time.sleep(random.uniform(0.2, 1.2))  # stagger start
        driver = make_driver(headless)
        driver.get(VOTE_URL)
        print(f"[T{thread_id}] ▶ started")

        def login() -> bool:
            add_btn = wait_css(driver, 'button[aria-label="Add Vote"]', 15)
            if not add_btn:
                print(f"[T{thread_id}] ⚠️ Add Vote not found")
                return False
            rdelay(0.5, 1.5)
            safe_click(driver, add_btn)

            rdelay(0.8, 1.6)
            try:
                email_input = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, 'input[id^="field-:"]'))
                )
            except TimeoutException:
                print(f"[T{thread_id}] ⚠️ Email input not found")
                return False

            email = generate_unique_email(used_emails)
            email_input.click(); email_input.clear(); email_input.send_keys(email)
            rdelay(0.4, 0.9)

            btn = find_first_text_button(driver, "Log in", "log in")
            if btn:
                safe_click(driver, btn)
            rdelay(1.2, 2.0)
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

            if not login():
                print(f"[T{thread_id}] ⚠️ login failed")
                break

            added = vote_jimin_only(driver)  # <-- flexible count
            thread_total += added
            print(f"[T{thread_id}] Loop {current_loop}: added {added} vote(s) — thread total = {thread_total}")

            logout_and_wait()

        return (thread_id, thread_total)

    finally:
        try:
            if driver: driver.quit()
        except Exception:
            pass

# ----- CLI / entry -----
def parse_args():
    p = argparse.ArgumentParser(description="Jimin-only VMA voter (multi-threaded, flexible counting)")
    p.add_argument("--threads", type=int, default=1, help="browsers in parallel (max 5)")
    p.add_argument("--loops",   type=int, default=None, help="loops per thread (0 = infinite). If omitted, you will be prompted.")
    p.add_argument("--headless", action="store_true", help="run without visible browser windows")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()

    # Cap threads at 5
    threads = max(1, args.threads or 1)

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

    per_thread = {}
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futures = [ex.submit(worker, i+1, loops, headless) for i in range(threads)]
        for fut in as_completed(futures):
            tid, total = fut.result()
            per_thread[tid] = total

    elapsed = time.time() - start

    # ----- Final summaries -----
    print("\n===== Per-thread totals =====")
    grand = 0
    for tid in sorted(per_thread.keys()):
        print(f"Thread {tid}: {per_thread[tid]} vote(s)")
        grand += per_thread[tid]
    print(f"Grand total: {grand} vote(s)")
    print(f"Elapsed: {elapsed / 60:.2f} min")


    sys.exit(0)
