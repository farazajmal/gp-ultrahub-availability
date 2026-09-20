import os
import json
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone

import requests
from scrape_doctors import scrape_doctor_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def build_patches_from_slots(raw_slots, doctor_booking_url):
    """
    Merges contiguous or close discrete time slots into availability patches (windows).
    """
    if not raw_slots:
        return []

    slots_by_day = {}
    for s in raw_slots:
        st_str = s.get("start_time")
        et_str = s.get("end_time")
        if not st_str or not et_str:
            continue
        try:
            st = datetime.fromisoformat(st_str)
            et = datetime.fromisoformat(et_str)
            day_str = s.get("day") or st.strftime("%Y-%m-%d")
            slots_by_day.setdefault(day_str, []).append((st, et))
        except Exception:
            continue

    patches = []

    for day_str in sorted(slots_by_day.keys()):
        day_slots = sorted(slots_by_day[day_str], key=lambda x: x[0])
        if not day_slots:
            continue

        current_start, current_end = day_slots[0]

        for next_start, next_end in day_slots[1:]:
            gap_seconds = (next_start - current_end).total_seconds()
            if gap_seconds <= 1800:  # <= 30 mins gap merges into same patch
                current_end = max(current_end, next_end)
            else:
                patches.append(_format_patch(day_str, current_start, current_end, doctor_booking_url))
                current_start, current_end = next_start, next_end

        patches.append(_format_patch(day_str, current_start, current_end, doctor_booking_url))

    return patches


def _format_patch(day_str, start_dt, end_dt, booking_url):
    day_name = start_dt.strftime("%A")
    st_label = start_dt.strftime("%I:%M %p").lstrip("0").lower()
    et_label = end_dt.strftime("%I:%M %p").lstrip("0").lower()
    
    display_time = f"{st_label} - {et_label}"
    display_full = f"{day_name} ({start_dt.strftime('%b %d')}): {display_time}"
    
    return {
        "date": day_str,
        "day_name": day_name,
        "start_time": st_label,
        "end_time": et_label,
        "display": display_time,
        "display_full": display_full,
        "booking_url": booking_url,
        "start_iso": start_dt.isoformat(),
        "end_iso": end_dt.isoformat()
    }


def scrape_availability(days_ahead=14):
    """
    Lightweight, ultra-fast 15-minute scraper.
    Reads pre-cached doctor IDs from doctors_metadata.json and fetches live availability patches.
    """
    if not os.path.exists("doctors_metadata.json"):
        logging.info("doctors_metadata.json not found. Running doctor metadata scraper first...")
        scrape_doctor_metadata()

    with open("doctors_metadata.json", "r", encoding="utf-8") as f:
        metadata = json.load(f)

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "accept": "application/au.com.hotdoc.v5",
        "accept-language": "en-US,en;q=0.9",
        "app-origin": "website",
        "app-platform": "web",
        "app-timezone": "Australia/Brisbane",
        "content-type": "application/json; charset=utf-8",
        "device-based-auth": "true",
        "is-kiosk": "false",
        "is-walk-ins": "false",
    }

    output = {
        "last_updated": None,
        "total_clinics": len(metadata.get("clinics", {})),
        "successful_clinics": 0,
        "failed_clinics": [],
        "total_doctors": 0,
        "clinics": {}
    }

    now_utc = datetime.now(timezone.utc)
    start_dt = datetime(now_utc.year, now_utc.month, now_utc.day, 14, 0, 0, tzinfo=timezone.utc) - timedelta(days=1)
    end_dt = start_dt + timedelta(days=days_ahead, seconds=86399, microseconds=999000)

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S.999Z")

    for clinic_name, doctors in metadata.get("clinics", {}).items():
        logging.info(f"\n========== Stage 3: Fetching Time Slots for {clinic_name} ==========")
        headers["app-current-session-uuid"] = str(uuid.uuid4())
        headers["app-device-uuid"] = str(uuid.uuid4())

        clinic_slug = doctors[0].get("clinic_slug") if doctors else "gp-ultra-hub-gladstone"
        clinic_html_urls = {
            "gp-ultra-hub-gladstone": "https://www.hotdoc.com.au/medical-centres/gladstone-QLD-4680/gp-ultra-hub-gladstone/doctors",
            "outback-gp": "https://www.hotdoc.com.au/medical-centres/calliope-QLD-4680/outback-gp/doctors",
            "gp-ultra-hub-burnett-heads": "https://www.hotdoc.com.au/medical-centres/burnett-heads-QLD-4670/gp-ultra-hub-burnett-heads/doctors",
            "gp-ultra-hub-toowoomba": "https://www.hotdoc.com.au/medical-centres/toowoomba-city-QLD-4350/gp-ultra-hub-toowoomba/doctors"
        }
        html_url = clinic_html_urls.get(clinic_slug)
        if html_url:
            try:
                session.get(html_url, headers={
                    "User-Agent": headers["User-Agent"],
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
                }, timeout=10)
            except Exception:
                pass

        clinic_api_url = f"https://www.hotdoc.com.au/api/patient/clinics/{clinic_slug}?id={clinic_slug}"
        doc_avail_map = {}
        try:
            resp = session.get(clinic_api_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                c_data = resp.json()
                for dr in c_data.get("doctor_reasons", []):
                    d_id = dr.get("doctor_id")
                    a_id = dr.get("availability_type_id")
                    if d_id and a_id:
                        doc_avail_map.setdefault(d_id, []).append(str(a_id))
        except Exception as e:
            logging.warning(f"Could not fetch clinic API for {clinic_slug}: {e}")

        clinic_results = []

        for d in doctors:
            doc_id = d.get("doctor_id")
            full_name = d.get("doctor")
            clinic_id = d.get("clinic_id")
            booking_url = d.get("profile_url") or d.get("booking_url")
            avail_ids = doc_avail_map.get(doc_id) or d.get("availability_type_ids") or []

            raw_slots = []
            seen_slot_ids = set()

            # Take up to first 3 availability type IDs per doctor to capture all available slot types
            for a_id in avail_ids[:3]:
                params = [
                    ("start_time", start_iso),
                    ("end_time", end_iso),
                    ("timezone", "Australia/Brisbane"),
                    ("clinic_id", clinic_id),
                    ("doctor_ids[]", str(doc_id)),
                    ("availability_type_ids[]", str(a_id))
                ]

                for attempt in range(4):
                    try:
                        res = session.get("https://www.hotdoc.com.au/api/patient/time_slots", headers=headers, params=params, timeout=12)
                        if res.status_code == 200:
                            data = res.json()
                            for s in data.get("time_slots", []):
                                s_id = s.get("id")
                                if s_id not in seen_slot_ids:
                                    seen_slot_ids.add(s_id)
                                    raw_slots.append(s)
                            break
                        elif res.status_code == 429:
                            wait_sec = (attempt + 1) * 15
                            logging.warning(f"Rate limited (429) on doc {full_name}, waiting {wait_sec}s...")
                            time.sleep(wait_sec)
                    except Exception as e:
                        logging.warning(f"Error requesting slots for {full_name}: {e}")
                        break

                time.sleep(1.5)

            patches = build_patches_from_slots(raw_slots, booking_url)

            if patches:
                earliest_patch = patches[0]
                availability_summary = earliest_patch["display_full"]
            else:
                availability_summary = "Call clinic to book"

            doc_record = dict(d)
            doc_record["availability"] = availability_summary
            doc_record["availability_patches"] = patches
            doc_record["booking_url"] = booking_url

            clinic_results.append(doc_record)
            logging.info(f"  ✓ Processed {full_name} ({len(patches)} availability patches)")

        output["clinics"][clinic_name] = clinic_results
        if clinic_results:
            output["successful_clinics"] += 1
            output["total_doctors"] += len(clinic_results)

        time.sleep(2)

    if output["successful_clinics"] > 0:
        output["last_updated"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        with open("availability.json", "w", encoding="utf-8") as f:
            json.dump(output, f, indent=4, ensure_ascii=False)
        logging.info("\n==========================================")
        logging.info("Availability Update Complete")
        logging.info("==========================================")
        logging.info(f"Successful clinics : {output['successful_clinics']}/{output['total_clinics']}")
        logging.info(f"Doctors scraped    : {output['total_doctors']}")
        logging.info("Saved output to availability.json")
    else:
        logging.error("Zero clinics were successfully scraped. Preserving existing availability.json")


def main():
    scrape_availability(days_ahead=14)


if __name__ == "__main__":
    main()
