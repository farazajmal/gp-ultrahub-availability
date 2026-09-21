import os
import sys
import json
import re
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

def parse_time_to_minutes(t_str):
    t_str = t_str.strip().lower()
    dt = datetime.strptime(t_str, "%I:%M %p")
    return dt.hour * 60 + dt.minute

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
        
    date_obj = datetime(year, month_idx, day_num)
    return date_obj

def scrape_doctor_live_grid(page, clinic_slug, doctor_slug):
    url = f"https://www.hotdoc.com.au/request/consult/for?defaults=practice-{clinic_slug},practitioner-{doctor_slug}"
    
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        
        # 1. Myself
        btn1 = page.locator("text='For myself'").first
        if btn1.is_visible():
            btn1.click()
            page.wait_for_timeout(1000)
            
        # 2. Existing
        btn2 = page.locator("text='Existing patient'").first
        if btn2.is_visible():
            btn2.click()
            page.wait_for_timeout(1000)
            
        # 3. Reason
        reason = page.locator("button:has-text('Appointment'), .flow-button").first
        if reason.is_visible():
            reason.click()
            page.wait_for_timeout(1000)
            
        # 4. Continue
        try:
            cont = page.get_by_text("Continue").first
            if cont and cont.is_visible():
                cont.click()
                page.wait_for_timeout(2500)
        except Exception:
            pass

        # Take screenshot for layout trigger
        page.screenshot(path=f"scratch/last_grid_{doctor_slug}.png")

        text = page.locator("body").inner_text()
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        
        # Parse date headers from text
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
                    
        # Parse time slots
        time_regex = re.compile(r"^(\d{1,2}:\d{2}\s*(?:am|pm))$", re.IGNORECASE)
        raw_times = []
        for l in lines:
            if time_regex.match(l):
                raw_times.append(l.strip())
                
        # Group times by time-reset
        day_groups = []
        current_group = []
        prev_minutes = -1
        
        for t in raw_times:
            minutes = parse_time_to_minutes(t)
            if prev_minutes != -1 and minutes <= prev_minutes:
                day_groups.append(current_group)
                current_group = []
            current_group.append(t)
            prev_minutes = minutes
            
        if current_group:
            day_groups.append(current_group)

        # Match day_groups to header_dates
        # Note: Day groups are ordered by day that has slots!
        # Find which header_dates actually match each group by checking position
        patches = []
        if day_groups and header_dates:
            # We match group i to header_dates starting from the first non-past header
            # E.g. if header_dates has [Today, Tue, Wed, Thu, Fri] and day_groups has 3 items
            # The first day with slots is Tue (index 1 in header_dates)
            # Let's match based on available header dates offset
            start_offset = 0
            if len(header_dates) > len(day_groups):
                start_offset = len(header_dates) - len(day_groups)
                # Ensure offset isn't out of range
                if start_offset < 0:
                    start_offset = 0

            for group_idx, discrete_slots in enumerate(day_groups):
                h_idx = start_offset + group_idx
                if h_idx < len(header_dates):
                    hd = header_dates[h_idx]
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

        print(f"[{doctor_slug}] Scraped {len(patches)} day patches ({len(raw_times)} total slots)")
        return patches

    except Exception as e:
        print(f"[{doctor_slug}] Error scraping: {e}")
        return []

def main():
    meta_path = "doctors_metadata.json"
    if not os.path.exists(meta_path):
        print("Metadata file missing")
        return

    with open(meta_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    availability = {}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for clinic_name, docs in metadata.get("clinics", {}).items():
            print(f"\n--- Scraping Clinic: {clinic_name} ({len(docs)} doctors) ---")
            availability[clinic_name] = {}
            for doc in docs:
                doc_name = doc["doctor"]
                clinic_slug = doc["clinic_slug"]
                doctor_slug = doc["doctor_slug"]
                
                print(f"Scraping {doc_name} ({doctor_slug})...")
                patches = scrape_doctor_live_grid(page, clinic_slug, doctor_slug)
                
                availability[clinic_name][doc_name] = {
                    "doctor": doc_name,
                    "clinic": clinic_name,
                    "doctor_id": doc["doctor_id"],
                    "clinic_id": doc["clinic_id"],
                    "booking_url": doc["booking_url"],
                    "patches": patches
                }

        browser.close()

    out_file = "availability.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(availability, f, indent=2)
    print(f"\nSUCCESS: Saved updated live availability to {out_file}")

if __name__ == "__main__":
    main()
