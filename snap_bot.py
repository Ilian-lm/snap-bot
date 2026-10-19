"""
Bot de surveillance Eurostar Snap — Paris Gare du Nord -> London St Pancras.
Vérifie le calendrier Snap à intervalle régulier et envoie une notif iPhone (ntfy)
dès qu'un des jours surveillés devient disponible. Ne réserve rien, ne paie rien.
"""
import os, re, time, random, datetime as dt
import requests
from playwright.sync_api import sync_playwright

# ---------- Réglages ----------
DATES = ["2026-10-24", "2026-10-25", "2026-10-26"]          # jours surveillés
LABELS = {"2026-10-24": "samedi 24 oct", "2026-10-25": "dimanche 25 oct", "2026-10-26": "lundi 26 oct"}
SNAP_URL = "https://snap.eurostar.com/uk-en"
INTERVAL_MIN = 3            # minutes entre deux vérifications (+ un peu d'aléatoire)
RUN_MAX_MIN = 340           # durée max d'un run GitHub (limite 6 h)
STOP_AT = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.timezone.utc)  # fin de la surveillance
TOPIC = os.environ["NTFY_TOPIC"]
# ------------------------------


def notify(title, msg, priority="urgent", click=SNAP_URL, tags="train"):
    try:
        requests.post(
            f"https://ntfy.sh/{TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Priority": priority,
                     "Click": click, "Tags": tags},
            timeout=15,
        )
    except Exception as e:
        print("ntfy error:", e)


def still_bookable(d):
    # Snap se réserve au plus tard la veille ; on arrête de surveiller un jour passé.
    day = dt.date.fromisoformat(d)
    return dt.datetime.now(dt.timezone.utc).date() < day


def read_calendar(page):
    """Ouvre Snap, règle Paris -> Londres, ouvre le calendrier, renvoie {date: dispo?}."""
    resp = page.goto(SNAP_URL, wait_until="domcontentloaded", timeout=60000)
    print("HTTP", resp.status if resp else None, flush=True)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    try:
        page.get_by_role("button", name="Continue with necessary cookies only").click(timeout=8000)
    except Exception:
        pass
    dest = page.get_by_placeholder("To")
    try:
        dest.wait_for(state="visible", timeout=45000)
    except Exception:
        body = page.inner_text("body")[:400].replace("\n", " ") if page.locator("body").count() else ""
        print("DIAG url:", page.url, "| title:", page.title(), "| body:", body, flush=True)
        raise
    dest.click()
    page.wait_for_timeout(800)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type("Paris", delay=60)
    page.get_by_role("option", name=re.compile("Paris Gare du Nord")).click(timeout=15000)
    page.get_by_alt_text(re.compile("Switch 'origin' and 'destination'")).click()
    page.wait_for_timeout(800)
    origin = page.get_by_placeholder("From").input_value()
    if "Paris" not in origin:
        raise RuntimeError(f"Origine inattendue: {origin}")
    page.get_by_role("button", name=re.compile("^Outbound date")).click()
    page.wait_for_selector(f'[data-date="{DATES[0]}"]', timeout=20000)
    page.wait_for_timeout(1500)
    out = {}
    for d in DATES:
        el = page.locator(f'[data-date="{d}"]:not([hidden])').first
        out[d] = el.count() > 0 and el.get_attribute("aria-disabled") != "true"
    return out


def read_slots(page, d):
    """Best effort : clique le jour puis Search, et cherche les créneaux affichés."""
    try:
        page.locator(f'[data-date="{d}"]:not([hidden])').first.click()
        page.wait_for_timeout(1000)
        for name in ["Confirm", "Done", "Apply", "Continue"]:
            b = page.get_by_role("button", name=name)
            if b.count():
                b.first.click(); break
        page.locator('button[type="submit"]').first.click(timeout=8000)
        page.wait_for_timeout(8000)
        txt = page.inner_text("body")
        lines = [l.strip() for l in txt.splitlines()
                 if re.search(r"14:00|before|after|morning|afternoon|£|€", l, re.I)]
        return " | ".join(dict.fromkeys(lines))[:250]
    except Exception as e:
        print("slots error:", e)
        return ""


def main():
    start = time.time()
    first_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if first_run:
        notify("Bot Snap lancé", "Surveillance Paris → Londres, 24-26 oct. Tu recevras une alerte dès qu'une place sort.",
               priority="default", tags="white_check_mark")
    known = {d: False for d in DATES}
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        while time.time() - start < RUN_MAX_MIN * 60 and dt.datetime.now(dt.timezone.utc) < STOP_AT:
            ctx = browser.new_context(locale="en-GB", timezone_id="Europe/London",
                                      viewport={"width": 480, "height": 900})
            page = ctx.new_page()
            try:
                status = read_calendar(page)
                fails = 0
                print(dt.datetime.now().isoformat(timespec="seconds"), status, flush=True)
                for d, ok in status.items():
                    if not still_bookable(d):
                        continue
                    if ok and not known[d]:
                        slots = read_slots(page, d)
                        msg = f"Billet Snap dispo : Paris → Londres, {LABELS[d]}."
                        if slots:
                            msg += f"\nAperçu : {slots}"
                        msg += "\nTouche la notif pour réserver."
                        notify(f"🚄 Snap dispo {LABELS[d]}", msg)
                    if not ok and known[d]:
                        notify(f"Snap {LABELS[d]} : plus dispo", "La place est repartie.",
                               priority="default", tags="x")
                    known[d] = ok
            except Exception as e:
                fails += 1
                print("check error:", e, flush=True)
                try:
                    page.screenshot(path=f"error_{int(time.time())}.png")
                except Exception:
                    pass
                if fails == 3:
                    notify("Bot Snap : problème",
                           "3 vérifications d'affilée ont échoué (site bloqué ou modifié). Vérifie toi-même en attendant.",
                           priority="high", tags="warning")
            finally:
                ctx.close()
            time.sleep(INTERVAL_MIN * 60 + random.randint(0, 40))
        browser.close()


if __name__ == "__main__":
    main()
