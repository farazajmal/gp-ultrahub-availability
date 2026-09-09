import os
import json
import time
import re
import difflib
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError

CLINICS = {
    "Gladstone": "https://www.hotdoc.com.au/medical-centres/gladstone-QLD-4680/gp-ultra-hub-gladstone/doctors",
    "Calliope": "https://www.hotdoc.com.au/medical-centres/calliope-QLD-4680/outback-gp/doctors",
    "Burnett Heads": "https://www.hotdoc.com.au/medical-centres/burnett-heads-QLD-4670/gp-ultra-hub-burnett-heads/doctors",
    "Toowoomba": "https://www.hotdoc.com.au/medical-centres/kearneys-spring-QLD-4350/gp-ultra-hub-toowoomba-plaza/doctors",
}

HOTDOC_BASE_URL = "https://www.hotdoc.com.au"
WEBSITE_EXPERTS_URL = "https://gpultrahub.com.au/our-experts/"

MAX_RETRIES = 3
RETRY_DELAY = 3


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
    print("\n========== Scraping GP UltraHub Website Experts ==========")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    website_doctors = []

    try:
        resp = requests.get(WEBSITE_EXPERTS_URL, headers=headers, timeout=25)
        if resp.status_code != 200:
            print(f"Failed to fetch {WEBSITE_EXPERTS_URL} (status {resp.status_code})")
            return website_doctors

        soup = BeautifulSoup(resp.content, "html.parser")
        links = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "our-experts/" in href and href.strip("/") != "https://gpultrahub.com.au/our-experts":
                links.add(href)

        print(f"Found {len(links)} doctor profile links on website.")

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

                # Find lists for areas of interest / expertise
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

                    # Qualifications paragraph check
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

                # Clean and deduplicate areas of interest
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
                print(f"  ✓ Scraped website profile: {doc_name} ({len(cleaned_interests)} interests)")

            except Exception as e:
                print(f"  ✗ Error scraping {url}: {e}")

    except Exception as e:
        print(f"Error connecting to website: {e}")

    return website_doctors


def main():
    # 1. Scrape Website Experts
    website_doctors = scrape_website_doctors()

    # 2. Scrape HotDoc Live Availability
    output = {
        "last_updated": None,
        "total_clinics": len(CLINICS),
        "successful_clinics": 0,
        "failed_clinics": [],
        "total_doctors": 0,
        "clinics": {}
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for clinic_name, url in CLINICS.items():
            print(f"\n========== {clinic_name} (HotDoc) ==========")
            success = False

            for attempt in range(1, MAX_RETRIES + 1):
                print(f"Attempt {attempt}/{MAX_RETRIES}")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_selector(".DoctorAvailability", timeout=30000)

                    soup = BeautifulSoup(page.content(), "html.parser")
                    doctors = soup.select(".DoctorAvailabilityRow")

                    clinic_results = []

                    for doctor in doctors:
                        button = doctor.select_one(".DoctorAvailability-actionButton")
                        availability = doctor.select_one(".DoctorAvailability-earliestAvailable")
                        profile_link = doctor.select_one(".DoctorAvailabilityRow-doctorLink")
                        profile_title = doctor.select_one(".DoctorAvailabilityRow-profileText p")
                        bio = doctor.select_one(".server-html p")
                        interest_items = doctor.select(".DoctorAvailabilityRow-profileText ul li")

                        if not button or not availability:
                            continue

                        doctor_name = button.get_text(strip=True).replace("View ", "")
                        booking_url = HOTDOC_BASE_URL + button["href"]

                        profile_url = None
                        if profile_link:
                            profile_url = HOTDOC_BASE_URL + profile_link["href"]

                        role = None
                        gender = None
                        qualifications = []
                        provider_type = "Other"

                        if profile_title:
                            parts = [part.strip() for part in profile_title.get_text(strip=True).split(",")]
                            if len(parts) >= 1:
                                role = parts[0]
                            if len(parts) >= 2:
                                gender = parts[1]
                            if len(parts) > 2:
                                qualifications = parts[2:]

                        if role:
                            role_lower = role.lower()
                            if "general practitioner" in role_lower or "registrar" in role_lower:
                                provider_type = "GP"
                            elif "nurse" in role_lower:
                                provider_type = "Nurse"
                            elif "dentist" in role_lower:
                                provider_type = "Dentist"
                            elif "skin" in role_lower:
                                provider_type = "Skin Specialist"

                        bio_text = ""
                        if bio:
                            bio_text = bio.get_text(" ", strip=True)

                        areas_of_interest = [item.get_text(strip=True) for item in interest_items]

                        # 3. Merge Website data (Priority: Website > HotDoc fallback)
                        matched_website_doc = None
                        for w_doc in website_doctors:
                            if doctor_name_matches(doctor_name, w_doc["doctor"]):
                                matched_website_doc = w_doc
                                break

                        if matched_website_doc:
                            # Use website specialties if available, otherwise keep hotdoc
                            if matched_website_doc.get("areas_of_interest"):
                                areas_of_interest = matched_website_doc["areas_of_interest"]
                            # Use website bio if available
                            if matched_website_doc.get("bio"):
                                bio_text = matched_website_doc["bio"]
                            # Use website qualifications if available
                            if matched_website_doc.get("qualifications"):
                                qualifications = list(set(qualifications + matched_website_doc["qualifications"]))

                        clinic_results.append({
                            "doctor": doctor_name,
                            "role": role,
                            "provider_type": provider_type,
                            "gender": gender,
                            "qualifications": qualifications,
                            "areas_of_interest": areas_of_interest,
                            "bio": bio_text,
                            "clinic": clinic_name,
                            "availability": availability.get_text(strip=True),
                            "booking_url": booking_url,
                            "profile_url": profile_url
                        })

                    output["clinics"][clinic_name] = clinic_results
                    output["successful_clinics"] += 1
                    output["total_doctors"] += len(clinic_results)

                    print(f"✓ Success ({len(clinic_results)} doctors found)")
                    success = True
                    break

                except TimeoutError:
                    print("Timeout.")
                except Exception as e:
                    print("Error:", e)

                if attempt < MAX_RETRIES:
                    print(f"Retrying in {RETRY_DELAY} seconds...\n")
                    time.sleep(RETRY_DELAY)

            if not success:
                print(f"✗ Failed after {MAX_RETRIES} attempts.")
                output["failed_clinics"].append({
                    "clinic": clinic_name,
                    "reason": f"Failed after {MAX_RETRIES} attempts"
                })
                output["clinics"][clinic_name] = []

        browser.close()

    # 4. Save and Compare
    old_data = None
    if os.path.exists("availability.json"):
        with open("availability.json", "r", encoding="utf-8") as f:
            old_data = json.load(f)

    old_compare = None
    if old_data:
        old_compare = {"clinics": old_data.get("clinics", {})}

    new_compare = {"clinics": output["clinics"]}

    if old_compare == new_compare:
        print("\n✓ No availability changes detected.")
        print("\n==========================================")
        print("Availability Update Complete")
        print("==========================================")
        print(f"Successful clinics : {output['successful_clinics']}/{output['total_clinics']}")
        print(f"Doctors scraped    : {output['total_doctors']}")
        print("\nNo file updated.")
        return

    output["last_updated"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")

    with open("availability.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)

    print("\n✓ Availability changed.")
    print("✓ availability.json updated.")
    print("\n==========================================")
    print("Availability Update Complete")
    print("==========================================")
    print(f"Successful clinics : {output['successful_clinics']}/{output['total_clinics']}")
    print(f"Doctors scraped    : {output['total_doctors']}")

    if output["failed_clinics"]:
        print("\nFailed Clinics:")
        for clinic in output["failed_clinics"]:
            print("-", clinic["clinic"])
    else:
        print("\nAll clinics scraped successfully!")


if __name__ == "__main__":
    main()
