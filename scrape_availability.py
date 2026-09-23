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
        if prev_minutes != -1 and minutes < prev_minutes:
            day_groups.append(current_group)
            current_group = []
        current_group.append(t)
        prev_minutes = minutes
        
    if current_group:
        day_groups.append(current_group)

    patches = []
    if date_headers and len(date_headers) >= len(day_groups):
        start_offset = 0
        # If headers contain past/closed days at start, adjust start_offset
        if len(date_headers) > len(day_groups):
            diff = len(date_headers) - len(day_groups)
            if diff > 0 and date_headers[0]["day_name"] in ["Sunday", "Saturday"]:
                start_offset = 0
            else:
                start_offset = diff

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
    scrape_status = "ok"
    
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        
        # Step 1: For myself
        try:
            page.locator("text='For myself'").first.click(timeout=4000)
            page.wait_for_timeout(1500)
        except Exception as e:
            logger.debug(f"[{doctor_slug}] Step 'For myself' notice: {e}")
            
        # Step 2: Existing patient
        try:
            page.locator("text='Existing patient'").first.click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception as e:
            logger.debug(f"[{doctor_slug}] Step 'Existing patient' notice: {e}")
            
        # Step 3: Reason / Appointment type
        try:
            page.locator("button:has-text('Appointment'), .flow-button").first.click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception as e:
            logger.debug(f"[{doctor_slug}] Step 'Appointment button' notice: {e}")
            
        # Step 4: Continue button if present
        try:
            cont = page.get_by_text("Continue").first
            if cont and cont.is_visible():
                cont.click(timeout=2000)
                page.wait_for_timeout(3000)
        except Exception as e:
            logger.debug(f"[{doctor_slug}] Step 'Continue' notice: {e}")

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
        time_regex = re.compile(r"^(\d{1,2}:\d{2}\s*(?:am|pm))$", re.IGNORECASE)

        # 1. Extract all date headers from top carousel
        header_dates = []
        for idx, l in enumerate(lines):
            m = date_pattern.match(l)
            if m:
                date_obj = parse_date_header(l)
                if date_obj and date_obj.strftime("%Y-%m-%d") not in [h["date_str"] for h in header_dates]:
                    header_dates.append({
                        "date_obj": date_obj,
                        "day_name": date_obj.strftime("%A"),
                        "date_str": date_obj.strftime("%Y-%m-%d"),
                        "raw": l,
                        "label": f"{date_obj.strftime('%A')}, {date_obj.strftime('%b %d')}"
                    })

        # 2. Extract all time slots from body
        raw_times = []
        for l in lines:
            if time_regex.match(l):
                raw_times.append(l.strip())

        # 3. Group raw times into day groups by time reset (< prev_minutes)
        day_groups = []
        current_group = []
        prev_minutes = -1
        for t in raw_times:
            minutes = parse_time_to_minutes(t)
            if prev_minutes != -1 and minutes < prev_minutes:
                day_groups.append(current_group)
                current_group = []
            current_group.append(t)
            prev_minutes = minutes
        if current_group:
            day_groups.append(current_group)

        # 4. Align day groups to header dates
        start_idx = 0
        if header_dates and len(header_dates) > len(day_groups):
            diff = len(header_dates) - len(day_groups)
            # If header_dates[0] is today and past business hours or weekend, start from next available weekday
            if header_dates[0]["day_name"] in ["Sunday", "Saturday"]:
                start_idx = 0
                while start_idx < len(header_dates) and header_dates[start_idx]["day_name"] in ["Sunday", "Saturday"]:
                    start_idx += 1
            else:
                # If first header is weekday but has no slots, align start_idx to first open weekday
                start_idx = 0
                if diff > 0 and len(day_groups) > 0:
                    # Match start_idx by checking day groups count
                    start_idx = diff

        slot_counts = {}
        patches = []
        curr_h_idx = start_idx if (header_dates and start_idx < len(header_dates)) else 0

        for group_idx, discrete_slots in enumerate(day_groups):
            hd = header_dates[curr_h_idx] if (header_dates and curr_h_idx < len(header_dates)) else None
            start_t = discrete_slots[0]
            end_t = discrete_slots[-1]
            display_time = f"{start_t} - {end_t}" if len(discrete_slots) > 1 else start_t

            if hd:
                d_str = hd["date_str"]
                slot_counts[d_str] = len(discrete_slots)
                patches.append({
                    "date": d_str,
                    "day_name": hd["day_name"],
                    "date_label": hd["label"],
                    "start_time": start_t,
                    "end_time": end_t,
                    "time_range": display_time,
                    "discrete_slots": discrete_slots,
                    "display": display_time,
                    "display_full": f"{hd['label']}: {display_time}"
                })
                curr_h_idx += 1
                while curr_h_idx < len(header_dates) and header_dates[curr_h_idx]["day_name"] == "Sunday" and (len(day_groups) - group_idx - 1) < (len(header_dates) - curr_h_idx):
                    curr_h_idx += 1
            else:
                patches.append({
                    "start_time": start_t,
                    "end_time": end_t,
                    "time_range": display_time,
                    "discrete_slots": discrete_slots,
                    "display": display_time,
                    "display_full": display_time
                })

        logger.info(f"[{doctor_slug}] Found {len(header_dates)} date headers, slot counts per header: {slot_counts}")

        scrape_status = "ok" if patches else ("no_availability" if "Choose a time" in text or "Existing patient" in text else "error")
        return patches, scrape_status

    except Exception as e:
        logger.error(f"[{doctor_slug}] Error scraping: {e}")
        return [], "error"


def validate_availability_data(availability, days_ahead=14):
    warnings = []
    now = datetime.now()
    min_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    max_date = (now + timedelta(days=days_ahead + 2)).strftime("%Y-%m-%d")
    dup_word_regex = re.compile(r"\b(\w+)\s*,\s*\1\b", re.IGNORECASE)

    for clinic_name, docs in availability.items():
        if not isinstance(docs, dict):
            continue
        for doc_name, doc in docs.items():
            patches = doc.get("availability_patches") or []
            dates_seen = set()
            for p in patches:
                p_date = p.get("date")
                if p_date:
                    if p_date < min_date or p_date > max_date:
                        w = f"[{doc_name}] Patch date {p_date} outside expected range [{min_date}, {max_date}]"
                        logger.warning(w)
                        warnings.append(w)
                    if p_date in dates_seen:
                        w = f"[{doc_name}] Duplicate patch date found: {p_date}"
                        logger.warning(w)
                        warnings.append(w)
                    dates_seen.add(p_date)

            avail_summary = doc.get("availability") or ""
            if dup_word_regex.search(avail_summary):
                w = f"[{doc_name}] Repeated word in availability summary: '{avail_summary}'"
                logger.warning(w)
                warnings.append(w)

            type_ids = doc.get("availability_type_ids") or []
            if len(type_ids) != len(set(type_ids)):
                w = f"[{doc_name}] Duplicate availability_type_ids found"
                logger.warning(w)
                warnings.append(w)

    if warnings:
        logger.warning(f"Validation completed with {len(warnings)} warning(s).")
    else:
        logger.info("Validation completed: 0 issues found.")
    return warnings


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
                patches, scrape_status = scrape_doctor_proven(page, clinic_slug, doctor_slug)
                page.close()
                
                doc_data = dict(doc)
                doc_data["availability_patches"] = patches
                doc_data["patches"] = patches
                doc_data["scrape_status"] = scrape_status

                if patches:
                    p0 = patches[0]
                    target_date = p0.get("date")
                    date_lbl = p0.get("date_label") or p0.get("day_name") or target_date
                    times = [p.get("display") for p in patches if p.get("date") == target_date]
                    if times and date_lbl:
                        doc_data["availability"] = f"{date_lbl} from " + " and ".join(times)
                    else:
                        doc_data["availability"] = "Call clinic to book"
                else:
                    doc_data["availability"] = "Call clinic to book"

                availability[clinic_name][doc_name] = doc_data

        browser.close()

    # Pre-write Validation Pass
    validate_availability_data(availability, days_ahead=days_ahead)

    out_file = "availability.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(availability, f, indent=2)
    logger.info(f"\nSUCCESS: Saved updated live availability to {out_file}")


if __name__ == "__main__":
    scrape_availability()
