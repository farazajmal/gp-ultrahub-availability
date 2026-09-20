import os
import json
import time
import re
import uuid
import difflib
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CLINICS_CONFIG = [
    {
        "name": "Gladstone",
        "clinic_id": "15111",
        "slug": "gp-ultra-hub-gladstone",
        "hotdoc_url": "https://www.hotdoc.com.au/medical-centres/gladstone-QLD-4680/gp-ultra-hub-gladstone/doctors"
    },
    {
        "name": "Calliope",
        "clinic_id": "8407",
        "slug": "outback-gp",
        "hotdoc_url": "https://www.hotdoc.com.au/medical-centres/calliope-QLD-4680/outback-gp/doctors"
    },
    {
        "name": "Burnett Heads",
        "clinic_id": "16389",
        "slug": "gp-ultra-hub-burnett-heads",
        "hotdoc_url": "https://www.hotdoc.com.au/medical-centres/burnett-heads-QLD-4670/gp-ultra-hub-burnett-heads/doctors"
    },
    {
        "name": "Toowoomba",
        "clinic_id": "17187",
        "slug": "gp-ultra-hub-toowoomba-plaza",
        "hotdoc_url": "https://www.hotdoc.com.au/medical-centres/kearneys-spring-QLD-4350/gp-ultra-hub-toowoomba-plaza/doctors"
    }
]

WEBSITE_EXPERTS_URL = "https://gpultrahub.com.au/our-experts/"


def _normalize_name(name):
    name = (name or "").lower()
    name = re.sub(r'^dr\.?\s*', '', name)
    return name.strip()


def doctor_name_matches(name1, name2, cutoff=0.7):
    norm1 = _normalize_name(name1)
    norm2 = _normalize_name(name2)

    if norm1 in norm2 or norm2 in norm1:
        return True

    words1 = norm1.split()
    words2 = norm2.split()

    if not words1 or not words2:
        return False

    for w1 in words1:
        best_ratio = max(
            (difflib.SequenceMatcher(None, w1, w2).ratio() for w2 in words2),
            default=0
        )
        if best_ratio >= cutoff:
            return True

    return False


def scrape_website_doctors():
    """Scrapes doctor profiles from the official GP UltraHub website."""
    logging.info("========== Stage 1: Scraping GP UltraHub Website Experts ==========")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    website_doctors = []

    try:
        resp = requests.get(WEBSITE_EXPERTS_URL, headers=headers, timeout=25)
        if resp.status_code != 200:
            logging.warning(f"Failed to fetch {WEBSITE_EXPERTS_URL} (status {resp.status_code})")
            return website_doctors

        soup = BeautifulSoup(resp.content, "html.parser")
        links = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "our-experts/" in href and href.strip("/") != "https://gpultrahub.com.au/our-experts":
                links.add(href)

        logging.info(f"Found {len(links)} doctor profile links on website.")

        for url in sorted(links):
            try:
                r = requests.get(url, headers=headers, timeout=20)
                if r.status_code != 200:
                    continue

                s = BeautifulSoup(r.content, "html.parser")

                h1 = s.find("h1")
                doc_name = h1.get_text(strip=True) if h1 else ""

                qualifications = []
                areas_of_interest = []
                bio_paragraphs = []

                for ul in s.find_all("ul"):
                    if ul.find_parents(["header", "footer", "nav"]) or "Home" in ul.get_text():
                        continue
                    prev = ul.find_previous(["p", "h2", "h3", "h4", "h5", "h6"])
                    if prev and any(k in prev.get_text().lower() for k in ["area", "interest", "expertise", "special"]):
                        items = [li.get_text(strip=True) for li in ul.find_all("li") if li.get_text(strip=True)]
                        if items:
                            areas_of_interest.extend(items)

                for p in s.find_all("p"):
                    if p.find_parents(["header", "footer", "nav"]):
                        continue
                    p_text = p.get_text(strip=True)
                    if not p_text or "Select your preferred location" in p_text or "Home" in p_text:
                        continue

                    if re.match(r'^(MBBS|FRACGP|MRCGP|BSc|MCPS|RACGP)', p_text, re.IGNORECASE) or (
                        len(p_text) < 60 and any(q in p_text for q in ['MBBS', 'FRACGP', 'MRCGP', 'MCPS', 'MRCPI'])
                    ):
                        qualifications.extend([q.strip() for q in re.split(r'[,/|]', p_text) if q.strip()])
                    elif any(k in p_text.lower() for k in ["areas of interest:", "areas of expertise:"]):
                        inline_match = re.split(r'areas of (?:interest|expertise):?', p_text, flags=re.IGNORECASE)
                        if len(inline_match) > 1 and inline_match[1].strip():
                            interests = [i.strip() for i in re.split(r'[,;\n•]', inline_match[1]) if i.strip()]
                            areas_of_interest.extend(interests)
                    else:
                        bio_paragraphs.append(p_text)

                h6 = s.find("h6")
                if h6:
                    h6_text = h6.get_text(strip=True)
                    if "Areas of Expertise:" in h6_text:
                        parts = h6_text.split("Areas of Expertise:")
                        bio_paragraphs.append(parts[0].strip())
                        if len(parts) > 1:
                            sub_parts = [sp.strip() for sp in re.split(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', parts[1]) if sp.strip()]
                            if sub_parts:
                                areas_of_interest.extend(sub_parts)
                            else:
                                areas_of_interest.append(parts[1].strip())
                    else:
                        bio_paragraphs.append(h6_text)

                cleaned_interests = []
                for item in areas_of_interest:
                    item_clean = item.strip()
                    if item_clean and item_clean not in cleaned_interests:
                        cleaned_interests.append(item_clean)

                website_doctors.append({
                    "url": url,
                    "doctor": doc_name,
                    "qualifications": list(set(qualifications)),
                    "areas_of_interest": cleaned_interests,
                    "bio": " ".join(bio_paragraphs).strip()
                })
                logging.info(f"  ✓ Scraped website profile: {doc_name} ({len(cleaned_interests)} interests)")

            except Exception as e:
                logging.warning(f"  ✗ Error scraping {url}: {e}")

    except Exception as e:
        logging.error(f"Error connecting to website: {e}")

    return website_doctors


def scrape_doctor_metadata():
    """Scrapes static metadata for doctors across all clinics and saves to doctors_metadata.json."""
    website_doctors = scrape_website_doctors()

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "accept": "application/au.com.hotdoc.v5",
        "app-origin": "website",
        "app-platform": "web",
        "app-timezone": "Australia/Brisbane",
        "content-type": "application/json; charset=utf-8",
        "device-based-auth": "true",
    }

    output = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
        "total_clinics": len(CLINICS_CONFIG),
        "clinics": {}
    }

    logging.info("\n========== Stage 2: Scraping HotDoc Clinic Metadata & Doctor IDs ==========")

    for clinic_cfg in CLINICS_CONFIG:
        clinic_name = clinic_cfg["name"]
        clinic_id = clinic_cfg["clinic_id"]
        slug = clinic_cfg["slug"]

        headers["app-current-session-uuid"] = str(uuid.uuid4())
        headers["app-device-uuid"] = str(uuid.uuid4())

        clinic_url = f"https://www.hotdoc.com.au/api/patient/clinics/{slug}?id={slug}"
        clinic_data = None

        for attempt in range(4):
            try:
                res = session.get(clinic_url, headers=headers, timeout=15)
                if res.status_code == 200:
                    clinic_data = res.json()
                    break
                elif res.status_code == 429:
                    wait_sec = (attempt + 1) * 15
                    logging.warning(f"Rate limited (429) fetching clinic info for {clinic_name}. Waiting {wait_sec}s...")
                    time.sleep(wait_sec)
                else:
                    logging.warning(f"Clinic API HTTP {res.status_code} for {clinic_name}")
            except Exception as e:
                logging.warning(f"Error fetching clinic API for {clinic_name}: {e}")
                time.sleep(3)

        if not clinic_data:
            logging.error(f"Failed to fetch metadata for {clinic_name}.")
            output["clinics"][clinic_name] = []
            continue

        raw_doctors = clinic_data.get("doctors", [])
        doctor_reasons = clinic_data.get("doctor_reasons", [])

        doc_avail_map = {}
        for dr in doctor_reasons:
            d_id = dr.get("doctor_id")
            avail_id = dr.get("availability_type_id")
            if d_id and avail_id:
                doc_avail_map.setdefault(d_id, []).append(str(avail_id))

        clinic_docs = []

        for d in raw_doctors:
            doc_id = d.get("id")
            full_name = d.get("full_name") or d.get("name") or ""
            doc_slug = d.get("slug") or ""
            prof = d.get("profession", "") or ""
            spec = d.get("specialty", "") or ""
            gender = (d.get("gender") or "").capitalize()
            qualifications = d.get("qualifications") or []
            bio_text = d.get("statement") or ""

            # HotDoc profile page URL as requested by user
            listing_path = d.get("listing_path")
            if listing_path:
                profile_url = f"https://www.hotdoc.com.au{listing_path}"
            elif doc_slug:
                profile_url = f"https://www.hotdoc.com.au/medical-centres/{slug}/doctors/{doc_slug}"
            else:
                profile_url = f"https://www.hotdoc.com.au/medical-centres/book/appointment/start?clinic={clinic_id}&doctor={doc_id}"

            role_text = f"{spec} {prof}".strip()
            if "General Practitioner" in role_text or "GP" in spec:
                provider_type = "GP"
                role = "General Practitioner"
            elif "Nurse" in role_text:
                provider_type = "Nurse"
                role = "Practice Nurse"
            elif "Physio" in role_text:
                provider_type = "Physiotherapist"
                role = "Physiotherapist"
            elif "Dentist" in role_text:
                provider_type = "Dentist"
                role = "Dentist"
            else:
                provider_type = "Other"
                role = spec or prof or "Medical Specialist"

            matched_website_doc = None
            for w_doc in website_doctors:
                if doctor_name_matches(full_name, w_doc["doctor"]):
                    matched_website_doc = w_doc
                    break

            areas_of_interest = []
            if matched_website_doc:
                if matched_website_doc.get("areas_of_interest"):
                    areas_of_interest = matched_website_doc["areas_of_interest"]
                if matched_website_doc.get("bio"):
                    bio_text = matched_website_doc["bio"]
                if matched_website_doc.get("qualifications"):
                    qualifications = list(set(qualifications + matched_website_doc["qualifications"]))

            avail_ids = doc_avail_map.get(doc_id, [])

            clinic_docs.append({
                "doctor_id": doc_id,
                "doctor": full_name,
                "role": role,
                "provider_type": provider_type,
                "gender": gender,
                "qualifications": qualifications,
                "areas_of_interest": areas_of_interest,
                "bio": bio_text,
                "clinic": clinic_name,
                "clinic_id": clinic_id,
                "clinic_slug": slug,
                "doctor_slug": doc_slug,
                "availability_type_ids": avail_ids,
                "profile_url": profile_url,
                "booking_url": profile_url
            })
            logging.info(f"  ✓ Processed metadata for {full_name} ({len(avail_ids)} availability types)")

        output["clinics"][clinic_name] = clinic_docs

    with open("doctors_metadata.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)

    logging.info("\n==========================================")
    logging.info("Doctor Metadata Scrape Complete")
    logging.info("Saved metadata to doctors_metadata.json")
    logging.info("==========================================")


if __name__ == "__main__":
    scrape_doctor_metadata()
