import os
import sys
import json
import re
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

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

def scrape_doctor_proven(page, clinic_slug, doctor_slug):
    url = f"https://www.hotdoc.com.au/request/consult/for?defaults=practice-{clinic_slug},practitioner-{doctor_slug}"
    
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        
        # Step 1: For myself (4s max auto-wait timeout)
        try:
            page.locator("text='For myself'").first.click(timeout=4000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
            
        # Step 2: Existing patient (3s max timeout)
        try:
            page.locator("text='Existing patient'").first.click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
            
        # Step 3: Reason (3s max timeout)
        try:
            page.locator("button:has-text('Appointment'), .flow-button").first.click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
            
        # Step 4: Continue button if present
        try:
            cont = page.get_by_text("Continue").first
            if cont and cont.is_visible():
                cont.click(timeout=2000)
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # Ensure grid is rendered
        text = page.locator("body").inner_text()
        if "Choose a time" not in text:
            page.wait_for_timeout(3000)
            text = page.locator("body").inner_text()

        # Force screenshot layout render tick
        page.screenshot(path=f"scratch/grid_loc_{doctor_slug}.png")
        text = page.locator("body").inner_text()
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        
        date_pattern = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})$")
        header_dates = []
        
        for idx, l in enumerate(lines):
            m = date_pattern.match(l)
            if m:
                day_name = lines[idx-1] if idx > 0 and lines[idx-1] in ["Today", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] else ""
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

def scrape_availability(days_ahead=14):
    from playwright.sync_api import sync_playwright

    meta_path = "doctors_metadata.json"
    if not os.path.exists(meta_path):
        logger.error(f"Metadata file missing at {meta_path}")
        return

    with open(meta_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    availability = {}
    os.makedirs("scratch", exist_ok=True)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        for clinic_name, docs in metadata.get("clinics", {}).items():
            logger.info(f"\n--- Scraping Clinic: {clinic_name} ({len(docs)} doctors) ---")
            availability[clinic_name] = {}
            
            for doc in docs:
                doc_name = doc["doctor"]
                clinic_slug = doc["clinic_slug"]
                doctor_slug = doc["doctor_slug"]
                
                logger.info(f"Scraping {doc_name} ({doctor_slug})...")
                page = context.new_page()
                patches = scrape_doctor_proven(page, clinic_slug, doctor_slug)
                page.close()
                
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

                availability[clinic_name][doc_name] = doc_data

        browser.close()

    out_file = "availability.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(availability, f, indent=2)
    logger.info(f"\nSUCCESS: Saved updated live availability to {out_file}")

if __name__ == "__main__":
    scrape_availability()
