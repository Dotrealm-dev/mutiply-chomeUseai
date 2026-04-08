"""
╔══════════════════════════════════════════════════════════════════╗
║       Smart Account Manager v2.1 — DepthMap Pipeline            ║
║  Flow: Google Login → Sculptok → Ezremove → Output              ║
╚══════════════════════════════════════════════════════════════════╝

pip install selenium webdriver-manager requests watchdog
"""

import json, os, sys, time, shutil, logging, requests
from pathlib import Path
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
BASE_DIR        = Path(__file__).parent
ACCOUNTS_FILE   = BASE_DIR / "accounts_v2.json"
PROFILES_DIR    = BASE_DIR / "chrome_profiles"
INPUT_DIR       = BASE_DIR / "Workspace" / "Input"
OUTPUT_DIR      = BASE_DIR / "Workspace" / "Output"

GOOGLE_LOGIN_URL = "https://accounts.google.com/v3/signin/identifier?flowName=GlifDesktopChromeSync"
SCULPTOK_URL    = "https://www.sculptok.com/imageGenerator"
EZREMOVE_URL    = "https://ezremove.ai/watermark-remover/"

WEB2_MIN_CREDIT = 4
WEB1_MIN_CREDIT = 2
TIMEOUT_PAGE    = 25
TIMEOUT_RESULT  = 180   # Sculptok รอสูงสุด 3 นาที
TIMEOUT_EZ      = 120   # Ezremove รอสูงสุด 2 นาที
POLL_INTERVAL   = 2     # เช็คทุก 2 วินาที

# ══════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("DepthBot")


# ══════════════════════════════════════════════
#  JSON HELPERS
# ══════════════════════════════════════════════
def load_accounts():
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_accounts(accounts):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)

def update_credit(email: str, site: str, new_value: int):
    """site = 'web2' (Sculptok) หรือ 'web1' (Ezremove)"""
    accounts = load_accounts()
    key = f"{site}_credits"
    for acc in accounts:
        if acc["email"] == email:
            log.info(f"📝 {email[:32]} │ {key}: {acc[key]} → {new_value}")
            acc[key] = new_value
            break
    save_accounts(accounts)

def print_summary():
    log.info("═" * 65)
    log.info("📊 สรุป Credit:")
    for acc in load_accounts():
        log.info(
            f"  {acc['email'][:38]:<38} │ "
            f"Sculptok:{acc['web2_credits']:>3} │ "
            f"Ezremove:{acc['web1_credits']:>3}"
        )
    log.info("═" * 65)


# ══════════════════════════════════════════════
#  DRIVER SETUP (Chrome Profile)
# ══════════════════════════════════════════════
def setup_driver(profile_dir: str) -> webdriver.Chrome:
    """
    เปิด Chrome ด้วย Profile เฉพาะของแต่ละ account
    Profile เก็บ Cookie/Session ถาวร → ไม่ต้อง Login ซ้ำ
    """
    profile_path = PROFILES_DIR / profile_dir
    profile_path.mkdir(parents=True, exist_ok=True)

    opt = Options()
    opt.add_argument(f"--user-data-dir={profile_path.resolve()}")
    opt.add_argument("--profile-directory=Default")
    # ป้องกัน Bot Detection
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option("useAutomationExtension", False)
    opt.add_argument("--start-maximized")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opt
    )
    # ซ่อน webdriver flag
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    return driver


# ══════════════════════════════════════════════
#  ★ GOOGLE LOGIN
# ══════════════════════════════════════════════
def is_google_logged_in(driver: webdriver.Chrome) -> bool:
    """
    ตรวจว่า Google Login อยู่แล้วหรือเปล่า
    วิธี: เปิด myaccount.google.com แล้วดู URL ปัจจุบัน
    """
    try:
        driver.get("https://myaccount.google.com/")
        time.sleep(3)
        url = driver.current_url
        # ถ้า login อยู่ → URL จะเป็น myaccount.google.com/...
        # ถ้ายัง → redirect ไปหน้า signin
        logged_in = "signin" not in url and "identifier" not in url and "ServiceLogin" not in url
        return logged_in
    except Exception:
        return False


def _human_type(element, text: str):
    """พิมพ์ทีละตัวเหมือนคนพิมพ์จริง (ลด Bot Detection)"""
    for ch in text:
        element.send_keys(ch)
        time.sleep(0.06)


def _click_next_btn(driver):
    """กดปุ่ม Next ในหน้า Google Login (ลอง selector หลายแบบ)"""
    for sel in [
        "div#identifierNext button",
        "div#passwordNext button",
        "button[jsname='LgbsSe']",
        "#next",
        "button[type='submit']",
    ]:
        try:
            driver.find_element(By.CSS_SELECTOR, sel).click()
            return
        except NoSuchElementException:
            continue
    # Fallback: Enter key
    from selenium.webdriver.common.keys import Keys
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.RETURN)


def google_login(driver: webdriver.Chrome, email: str, password: str) -> bool:
    """
    Login Google อัตโนมัติ
    - ถ้าเจอ CAPTCHA / 2FA → รอให้ผู้ใช้ทำเอง แล้วกด Enter
    - คืนค่า True = สำเร็จ
    """
    log.info(f"  🔑 Login Google: {email}")
    wait = WebDriverWait(driver, TIMEOUT_PAGE)

    try:
        driver.get(GOOGLE_LOGIN_URL)
        time.sleep(2)

        # ── กรอก Email ──
        email_el = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']"))
        )
        email_el.clear()
        _human_type(email_el, email)
        time.sleep(0.5)
        _click_next_btn(driver)
        time.sleep(2)

        # ── กรอก Password ──
        try:
            pw_el = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
            )
            pw_el.clear()
            _human_type(pw_el, password)
            time.sleep(0.5)
            _click_next_btn(driver)
            time.sleep(3)
        except TimeoutException:
            log.warning("  ⚠️  ไม่พบช่องรหัสผ่าน — อาจมีขั้นตอนพิเศษ")

        # ── ตรวจสอบผล ──
        time.sleep(3)
        current_url = driver.current_url

        # Login สำเร็จทันที
        if "myaccount.google.com" in current_url or "google.com/u/0" in current_url:
            log.info(f"  ✅ Google Login สำเร็จ: {email}")
            return True

        # มีขั้นตอนเพิ่ม (2FA, Phone Verify ฯลฯ)
        if any(k in current_url for k in ["signin", "challenge", "identifier", "approve"]):
            log.warning(f"  ⚠️  ต้องทำขั้นตอนเพิ่มเติม (2FA / Verify) สำหรับ {email}")
            log.warning(f"  👉 ทำให้เสร็จในหน้าต่าง Chrome แล้วกด Enter ที่นี่")
            input("  >>> กด Enter เมื่อ Login เสร็จแล้ว: ")
            return is_google_logged_in(driver)

        # กรณีอื่น — ถือว่าสำเร็จ
        log.info(f"  ✅ Google Login สำเร็จ: {email}")
        return True

    except Exception as e:
        log.error(f"  ❌ Google Login Error: {e}")
        return False


def ensure_google_login(driver: webdriver.Chrome, account: dict) -> bool:
    """
    เช็คก่อน — ถ้า Login อยู่แล้วไม่ต้องทำอะไร (เร็ว)
    ถ้ายัง → Login อัตโนมัติ
    """
    if is_google_logged_in(driver):
        log.info(f"  ✔️  Google ยัง Login อยู่ ({account['email'][:30]})")
        return True
    log.info(f"  ℹ️  ยังไม่ Login → เริ่ม Login อัตโนมัติ")
    return google_login(driver, account["email"], account["pw"])


# ══════════════════════════════════════════════
#  LIVE CREDIT READERS
# ══════════════════════════════════════════════
def get_live_credit_sculptok(driver: webdriver.Chrome) -> int:
    """อ่าน credit จริงจาก class="text-white font-bold ml-2" """
    try:
        driver.get(SCULPTOK_URL)
        el = WebDriverWait(driver, TIMEOUT_PAGE).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".text-white.font-bold.ml-2"))
        )
        credit = int(el.text.strip())
        log.info(f"  💳 Sculptok live credit: {credit}")
        return credit
    except (TimeoutException, ValueError, NoSuchElementException) as e:
        log.warning(f"  ⚠️  อ่าน Sculptok credit ไม่ได้: {e}")
        return -1


def get_live_credit_ezremove(driver: webdriver.Chrome) -> int:
    """อ่าน credit จริงจาก class="coin-amount" """
    try:
        driver.get(EZREMOVE_URL)
        el = WebDriverWait(driver, TIMEOUT_PAGE).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".coin-amount"))
        )
        credit = int(el.text.strip())
        log.info(f"  💳 Ezremove live credit: {credit}")
        return credit
    except (TimeoutException, ValueError, NoSuchElementException) as e:
        log.warning(f"  ⚠️  อ่าน Ezremove credit ไม่ได้: {e}")
        return -1


# ══════════════════════════════════════════════
#  SMART ACCOUNT SELECTORS
# ══════════════════════════════════════════════
def get_best_account_sculptok():
    """
    วนหา account ที่ใช้ Sculptok ได้:
      1. กรองจาก JSON ก่อน (ประหยัดเวลา)
      2. เปิด Chrome Profile
      3. Ensure Google Login ★
      4. เช็ค Live Credit
    """
    log.info(f"🔍 หา account Sculptok (ต้องการ ≥ {WEB2_MIN_CREDIT})")

    for acc in load_accounts():
        # กรองเบื้องต้น
        if acc["web2_credits"] < WEB2_MIN_CREDIT:
            log.info(f"  ⏭️  ข้าม {acc['email'][:35]} (JSON={acc['web2_credits']})")
            continue

        log.info(f"  🔓 เปิด Profile: {acc['profile_dir']}")
        driver = setup_driver(acc["profile_dir"])

        try:
            # ★ Google Login ก่อน
            if not ensure_google_login(driver, acc):
                log.error(f"  ❌ Google Login ล้มเหลว → ข้าม")
                driver.quit()
                continue

            live = get_live_credit_sculptok(driver)

            # ถ้าอ่านไม่ได้ลองอีกครั้ง
            if live == -1:
                time.sleep(3)
                live = get_live_credit_sculptok(driver)

            if live >= WEB2_MIN_CREDIT:
                log.info(f"  ✅ ใช้: {acc['email']} (credit={live})")
                return acc, driver, live

            log.info(f"  ❌ credit ไม่พอ ({live}) → อัปเดต JSON")
            update_credit(acc["email"], "web2", max(live, 0))
            driver.quit()

        except Exception as e:
            log.error(f"  💥 Error: {e}")
            try: driver.quit()
            except: pass

    return None, None, 0


def get_best_account_ezremove(preferred_email: str = None):
    """วนหา account ที่ใช้ Ezremove ได้ (ลอง preferred account ก่อน)"""
    log.info(f"🔍 หา account Ezremove (ต้องการ ≥ {WEB1_MIN_CREDIT})")
    accounts = load_accounts()

    # ให้ preferred_email มาก่อน
    if preferred_email:
        accounts = sorted(accounts, key=lambda a: 0 if a["email"] == preferred_email else 1)

    for acc in accounts:
        if acc["web1_credits"] < WEB1_MIN_CREDIT:
            log.info(f"  ⏭️  ข้าม {acc['email'][:35]} (JSON={acc['web1_credits']})")
            continue

        log.info(f"  🔓 เปิด Profile: {acc['profile_dir']}")
        driver = setup_driver(acc["profile_dir"])

        try:
            if not ensure_google_login(driver, acc):
                log.error(f"  ❌ Google Login ล้มเหลว → ข้าม")
                driver.quit()
                continue

            live = get_live_credit_ezremove(driver)
            if live == -1:
                time.sleep(3)
                live = get_live_credit_ezremove(driver)

            if live >= WEB1_MIN_CREDIT:
                log.info(f"  ✅ ใช้: {acc['email']} (credit={live})")
                return acc, driver, live

            log.info(f"  ❌ credit ไม่พอ ({live}) → อัปเดต JSON")
            update_credit(acc["email"], "web1", max(live, 0))
            driver.quit()

        except Exception as e:
            log.error(f"  💥 Error: {e}")
            try: driver.quit()
            except: pass

    return None, None, 0


# ══════════════════════════════════════════════
#  DOWNLOAD HELPER
# ══════════════════════════════════════════════
def download_image(driver: webdriver.Chrome, img_url: str, save_path: Path) -> bool:
    """ดาวน์โหลดรูปโดยใช้ Cookie จาก Selenium session"""
    try:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        headers = {"User-Agent": driver.execute_script("return navigator.userAgent;")}
        resp    = requests.get(img_url, cookies=cookies, headers=headers,
                               stream=True, timeout=60)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            shutil.copyfileobj(resp.raw, f)
        log.info(f"  💾 บันทึก: {save_path.name} ({save_path.stat().st_size // 1024} KB)")
        return True
    except Exception as e:
        log.error(f"  ❌ download_image: {e}")
        return False


# ══════════════════════════════════════════════
#  PROCESS SCULPTOK  (3-Layer Smart Wait)
# ══════════════════════════════════════════════
def process_sculptok(acc, driver, image_path: Path, credit_before: int):
    log.info(f"🎨 Sculptok: {image_path.name}")
    wait = WebDriverWait(driver, TIMEOUT_PAGE)

    try:
        driver.get(SCULPTOK_URL)
        time.sleep(2)

        # Upload (input ซ่อนอยู่ ต้องใช้ JS ทำให้ visible)
        log.info("  📤 Upload...")
        el = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input.el-upload__input")
        ))
        driver.execute_script("arguments[0].style.display='block';", el)
        el.send_keys(str(image_path.resolve()))
        time.sleep(3)

        # Layer 1 — รอ result element ปรากฏ
        log.info("  ⏳ [L1] รอ result element ปรากฏ...")
        result_img = WebDriverWait(driver, TIMEOUT_RESULT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "img.el-image__inner"))
        )

        # Layer 2 — รอ src URL จริง (ไม่ใช่ loading gif / blank)
        log.info("  ⏳ [L2] รอ src URL จริง...")
        img_url  = ""
        deadline = time.time() + TIMEOUT_RESULT
        while time.time() < deadline:
            img_url = result_img.get_attribute("src") or ""
            if img_url and "data:image/gif" not in img_url and img_url.startswith("http"):
                log.info(f"  ✅ URL พร้อม: {img_url[:70]}...")
                break
            time.sleep(POLL_INTERVAL)
        else:
            raise TimeoutException("Layer2: src URL ไม่ปรากฏภายในเวลาที่กำหนด")

        # Layer 3 — ตรวจ credit ลดลง = งานสำเร็จจริง
        log.info("  ⏳ [L3] ตรวจ credit หลังเสร็จ...")
        time.sleep(1)
        credit_after = get_live_credit_sculptok(driver)
        if credit_after != -1:
            update_credit(acc["email"], "web2", credit_after)
            log.info(f"  💳 ใช้ไป {credit_before - credit_after} credit │ เหลือ {credit_after}")
        else:
            update_credit(acc["email"], "web2", max(0, credit_before - 4))

        # Download
        ts   = datetime.now().strftime("%H%M%S")
        dest = OUTPUT_DIR / "depth" / f"{image_path.stem}_depth_{ts}.png"
        if download_image(driver, img_url, dest):
            log.info(f"  🎉 Sculptok สำเร็จ → {dest.name}")
            return dest
        return None

    except TimeoutException as e:
        log.error(f"  ❌ Timeout Sculptok: {e}")
        return None
    except Exception as e:
        log.error(f"  ❌ Sculptok Error: {e}")
        return None


# ══════════════════════════════════════════════
#  PROCESS EZREMOVE
# ══════════════════════════════════════════════
def process_ezremove(acc, driver, depth_path: Path, credit_before: int):
    log.info(f"🧹 Ezremove: {depth_path.name}")
    wait = WebDriverWait(driver, TIMEOUT_PAGE)

    try:
        driver.get(EZREMOVE_URL)
        time.sleep(2)

        # Upload
        log.info("  📤 Upload...")
        dropzone = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, ".dropzone__inner")
        ))
        upload_el = dropzone.find_element(By.CSS_SELECTOR, "input[type='file']")
        driver.execute_script("arguments[0].style.display='block';", upload_el)
        upload_el.send_keys(str(depth_path.resolve()))
        time.sleep(3)

        # รอผล
        RESULT_SEL = "img.absolute.top-0.left-0.w-full.h-full.object-cover.z-10"
        log.info(f"  ⏳ รอผล Ezremove (timeout {TIMEOUT_EZ}s)...")
        result_img = WebDriverWait(driver, TIMEOUT_EZ).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, RESULT_SEL))
        )

        img_url  = ""
        deadline = time.time() + TIMEOUT_EZ
        while time.time() < deadline:
            img_url = result_img.get_attribute("src") or ""
            if img_url and "data:image/gif" not in img_url and img_url.startswith("http"):
                log.info(f"  ✅ URL พร้อม: {img_url[:70]}...")
                break
            time.sleep(POLL_INTERVAL)
        else:
            raise TimeoutException("Ezremove: src URL ไม่ปรากฏภายในเวลาที่กำหนด")

        # อัปเดต credit
        credit_after = get_live_credit_ezremove(driver)
        if credit_after != -1:
            update_credit(acc["email"], "web1", credit_after)
            log.info(f"  💳 เหลือ Ezremove credit: {credit_after}")
        else:
            update_credit(acc["email"], "web1", max(0, credit_before - 2))

        # Download
        ts   = datetime.now().strftime("%H%M%S")
        stem = depth_path.stem.replace("_depth", "")
        dest = OUTPUT_DIR / f"{stem}_final_{ts}.png"
        if download_image(driver, img_url, dest):
            log.info(f"  🎉 Ezremove สำเร็จ → {dest.name}")
            return dest
        return None

    except TimeoutException as e:
        log.error(f"  ❌ Timeout Ezremove: {e}")
        return None
    except Exception as e:
        log.error(f"  ❌ Ezremove Error: {e}")
        return None


# ══════════════════════════════════════════════
#  PIPELINE
# ══════════════════════════════════════════════
def run_pipeline(image_path: Path):
    log.info("═" * 65)
    log.info(f"🚀 Pipeline: {image_path.name}")
    log.info("═" * 65)

    if not image_path.exists():
        log.error(f"❌ ไม่พบไฟล์: {image_path}")
        return None

    # ── STEP 1: Sculptok ──
    sc_acc, sc_driver, sc_credit = get_best_account_sculptok()
    if sc_acc is None:
        log.error("❌ ไม่มี Sculptok account พร้อมใช้")
        print_summary()
        return None

    depth_path = None
    try:
        depth_path = process_sculptok(sc_acc, sc_driver, image_path, sc_credit)
    finally:
        try: sc_driver.quit()
        except: pass

    if depth_path is None:
        log.error("❌ Sculptok ล้มเหลว — หยุด Pipeline")
        return None

    # ── STEP 2: Ezremove ──
    # ลองใช้ account เดิมก่อน (ประหยัดเวลาเปิด Browser)
    ez_acc, ez_driver, ez_credit = get_best_account_ezremove(
        preferred_email=sc_acc["email"]
    )
    if ez_acc is None:
        log.error("❌ ไม่มี Ezremove account พร้อมใช้")
        log.info(f"ℹ️  Depth Map อยู่ที่: {depth_path}")
        print_summary()
        return None

    final_path = None
    try:
        final_path = process_ezremove(ez_acc, ez_driver, depth_path, ez_credit)
    finally:
        try: ez_driver.quit()
        except: pass

    if final_path:
        log.info("═" * 65)
        log.info(f"🏆 Pipeline เสร็จสมบูรณ์!")
        log.info(f"   Input     : {image_path.name}")
        log.info(f"   Depth Map : {depth_path.name}")
        log.info(f"   Final     : {final_path.name}")
        log.info("═" * 65)
        print_summary()

    return final_path


# ══════════════════════════════════════════════
#  WATCHDOG — Monitor Input Folder
# ══════════════════════════════════════════════
def watch_input_folder():
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    SUPPORTED  = {".png", ".jpg", ".jpeg"}
    processing = set()

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory: return
            path = Path(event.src_path)
            if path.suffix.lower() not in SUPPORTED: return
            if str(path) in processing: return

            processing.add(str(path))
            log.info(f"🆕 ไฟล์ใหม่จาก Blender: {path.name}")
            time.sleep(0.8)  # รอให้ copy เสร็จก่อน

            try:
                run_pipeline(path)
            except Exception as e:
                log.error(f"💥 Pipeline error: {e}")
            finally:
                processing.discard(str(path))

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    obs = Observer()
    obs.schedule(Handler(), str(INPUT_DIR), recursive=False)
    obs.start()
    log.info(f"👁  Monitor: {INPUT_DIR}")
    log.info("🤖 Bot พร้อมแล้ว! รอรับรูปจาก Blender addon...")

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log.info("⛔ หยุด Bot")
        obs.stop()
    obs.join()


# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) > 1:
        # รันแบบระบุไฟล์โดยตรง: python main_bot_v2.py C:/path/image.png
        result = run_pipeline(Path(sys.argv[1]))
        sys.exit(0 if result else 1)
    else:
        # Watch Mode — รับไฟล์จาก Blender addon
        try:
            watch_input_folder()
        except ImportError:
            log.error("ติดตั้ง watchdog ก่อน: pip install watchdog")
            sys.exit(1)
