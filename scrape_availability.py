import os
import json
import re
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# How many doctors to scrape at the same time. Raise for more speed,
# lower if the site starts blocking / rate-limiting you.
CONCURRENCY = int(os.environ.get("SCRAPE_CONCURRENCY", "4"))


def parse_time_to_minutes(t_str):
    t_str = t_str.strip().lower()
    try:
        dt = datetime.strptime(t_str, "%I:%M %p")
        return dt.hour * 60 + dt.minute
    except Exception:
        return -1


def parse_date_header(raw_header):
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})", raw_header)
    if not m:
        return None
    day_num = int(m.group(1))
    month_str = m.group(2).capitalize()
    if month_str not in month_names:
        return None
    month_idx = month_names.index(month_str) + 1

    now = datetime.now()
    year = now.year
    if month_idx < now.month - 6:
        year += 1

    try:
        return datetime(year, month_idx, day_num)
    except Exception:
        return None


def build_patches_from_slots(raw_slots, date_headers=None):
    """Helper function to build structured time patches from raw time slots."""
    if not raw_slots:
        return []

    day_groups = []
    current_group = []
    prev_minutes = -1

    for t in raw_slots:
        minutes = parse_time_to_minutes(t)
        if prev_minutes != -1 and minutes <= prev_minutes:
            day_groups.append(current_group)
            current_group = []
        current_group.append(t)
        prev_minutes = minutes

    if current_group:
        day_groups.append(current_group)

    patches = []
    if date_headers and len(date_headers) >= len(day_groups):
        start_offset = len(date_headers) - len(day_groups)
        for group_idx, discrete_slots in enumerate(day_groups):
            h_idx = start_offset + group_idx
            if h_idx < len(date_headers):
                hd = date_headers[h_idx]
                start_t = discrete_slots[0]
                end_t = discrete_slots[-1]
                display_time = f"{start_t} - {end_t}" if len(discrete_slots) > 1 else start_t

                patches.append({
                    "date": hd["date_str"],
                    "day_name": hd["day_name"],
                    "date_label": hd["label"],
                    "start_time": start_t,
                    "end_time": end_t,
                    "time_range": display_time,
                    "discrete_slots": discrete_slots,
                    "display": display_time,
                    "display_full": f"{hd['label']}: {display_time}"
                })
    else:
        for idx, discrete_slots in enumerate(day_groups):
            start_t = discrete_slots[0]
            end_t = discrete_slots[-1]
            display_time = f"{start_t} - {end_t}" if len(discrete_slots) > 1 else start_t
            patches.append({
                "start_time": start_t,
                "end_time": end_t,
                "time_range": display_time,
                "discrete_slots": discrete_slots,
                "display": display_time,
                "display_full": display_time
            })

    return patches


async def _click_when_ready(page, text, timeout=4000):
    """Wait for an element to actually be visible, then click it.
    Same step as before, but we don't sleep a fixed amount first —
    we proceed the moment the element is ready (or give up after `timeout`)."""
    try:
        loc = page.locator(f"text='{text}'").first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()
        return True
    except Exception:
        return False


async def _click_appointment_button(page, timeout=4000):
    try:
        loc = page.locator("button:has-text('Appointment'), .flow-button").first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()
        return True
    except Exception:
        return False


async def scrape_doctor_proven(page, clinic_slug, doctor_slug):
    url = f"https://www.hotdoc.com.au/request/consult/for?defaults=practice-{clinic_slug},practitioner-{doctor_slug}"

    try:
        await page.goto(url, wait_until="domcontentloaded")

        # Step 1: For myself
        ok1 = await _click_when_ready(page, "For myself", timeout=8000)

        # Step 2: Existing patient
        ok2 = await _click_when_ready(page, "Existing patient", timeout=6000)

        # Step 3: Reason
        ok3 = await _click_appointment_button(page, timeout=6000)

        # Step 4: Continue button if present
        ok4 = True
        try:
            cont = page.get_by_text("Continue").first
            await cont.wait_for(state="visible", timeout=3500)
            await cont.click()
        except Exception:
            ok4 = False

        logger.info(
            f"[{doctor_slug}] step status - for_myself={ok1} "
            f"existing_patient={ok2} reason={ok3} continue={ok4}"
        )

        # Ensure grid is rendered — wait for the actual text instead of
        # sleep-then-check-then-sleep-again.
        grid_ready = True
        try:
            await page.get_by_text("Choose a time").first.wait_for(state="visible", timeout=8000)
        except Exception:
            grid_ready = False
            # Give it one more beat in case it's still hydrating.
            await page.wait_for_timeout(2000)

        if not grid_ready:
            logger.warning(f"[{doctor_slug}] 'Choose a time' text never appeared — grid may not have loaded")

        # Force screenshot layout render tick
        await page.screenshot(path=f"scratch/grid_loc_{doctor_slug}.png")
        text = await page.locator("body").inner_text()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        date_pattern = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})$")
        header_dates = []

        for idx, l in enumerate(lines):
            m = date_pattern.match(l)
            if m:
                date_obj = parse_date_header(l)
                if date_obj:
                    header_dates.append({
                        "date_obj": date_obj,
                        "day_name": date_obj.strftime("%A"),
                        "date_str": date_obj.strftime("%Y-%m-%d"),
                        "raw": l,
                        "label": f"{date_obj.strftime('%A')}, {date_obj.strftime('%b %d')}"
                    })

        time_regex = re.compile(r"^(\d{1,2}:\d{2}\s*(?:am|pm))$", re.IGNORECASE)
        raw_times = []
        for l in lines:
            if time_regex.match(l):
                raw_times.append(l.strip())

        patches = build_patches_from_slots(raw_times, header_dates)
        logger.info(f"[{doctor_slug}] Scraped {len(patches)} day patches ({len(raw_times)} total slots)")
        return patches

    except Exception as e:
        logger.error(f"[{doctor_slug}] Error scraping: {e}")
        return []


async def _scrape_one(browser, clinic_name, doc, semaphore, availability):
    async with semaphore:
        doc_name = doc["doctor"]
        clinic_slug = doc["clinic_slug"]
        doctor_slug = doc["doctor_slug"]

        logger.info(f"Scraping {doc_name} ({doctor_slug})...")

        # Each doctor gets its OWN browser context (own cookies/session),
        # not a shared one. HotDoc's booking flow tracks the "for myself /
        # existing patient / reason" selections via session state, so
        # concurrent doctors sharing a context were overwriting each
        # other's progress mid-flow and coming back with no slots.
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        try:
            patches = await scrape_doctor_proven(page, clinic_slug, doctor_slug)
        finally:
            await page.close()
            await context.close()

        doc_data = dict(doc)
        doc_data["availability_patches"] = patches
        doc_data["patches"] = patches

        if patches:
            p0 = patches[0]
            d_name = p0.get("day_name") or p0.get("date")
            date_lbl = p0.get("date_label") or ""
            times = [p.get("display") for p in patches if (p.get("day_name") == d_name or p.get("date") == d_name)]
            prefix = f"{d_name}, {date_lbl}" if date_lbl else d_name
            if times and prefix:
                doc_data["availability"] = f"{prefix} from " + " and ".join(times)
            else:
                doc_data["availability"] = "Call clinic to book"
        else:
            doc_data["availability"] = "Call clinic to book"

        availability.setdefault(clinic_name, {})[doc_name] = doc_data


async def scrape_availability_async(days_ahead=14):
    from playwright.async_api import async_playwright

    meta_path = "doctors_metadata.json"
    if not os.path.exists(meta_path):
        logger.error(f"Metadata file missing at {meta_path}")
        return

    with open(meta_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    availability = {}
    os.makedirs("scratch", exist_ok=True)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        tasks = []
        for clinic_name, docs in metadata.get("clinics", {}).items():
            availability.setdefault(clinic_name, {})
            for doc in docs:
                tasks.append(_scrape_one(browser, clinic_name, doc, semaphore, availability))

        logger.info(f"Scraping {len(tasks)} doctors with concurrency={CONCURRENCY}...")
        await asyncio.gather(*tasks, return_exceptions=False)

        await browser.close()

    out_file = "availability.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(availability, f, indent=2)
    logger.info(f"\nSUCCESS: Saved updated live availability to {out_file}")


def scrape_availability(days_ahead=14):
    """Sync entry point — kept so you can call this exactly like before."""
    asyncio.run(scrape_availability_async(days_ahead=days_ahead))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scrape_availability()
