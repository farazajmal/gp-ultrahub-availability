import os
import json
import time
from datetime import datetime

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError

CLINICS = {
    "Gladstone": "https://www.hotdoc.com.au/medical-centres/gladstone-QLD-4680/gp-ultra-hub-gladstone/doctors",
    "Calliope": "https://www.hotdoc.com.au/medical-centres/calliope-QLD-4680/outback-gp/doctors",
    "Burnett Heads": "https://www.hotdoc.com.au/medical-centres/burnett-heads-QLD-4670/gp-ultra-hub-burnett-heads/doctors",
    "Toowoomba": "https://www.hotdoc.com.au/medical-centres/kearneys-spring-QLD-4350/gp-ultra-hub-toowoomba-plaza/doctors",
}

BASE_URL = "https://www.hotdoc.com.au"

MAX_RETRIES = 3
RETRY_DELAY = 3

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

        print(f"\n========== {clinic_name} ==========")

        success = False

        for attempt in range(1, MAX_RETRIES + 1):

            print(f"Attempt {attempt}/{MAX_RETRIES}")

            try:

                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

                page.wait_for_selector(
                    ".DoctorAvailability",
                    timeout=30000
                )

                soup = BeautifulSoup(
                    page.content(),
                    "html.parser"
                )

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
                    booking_url = BASE_URL + button["href"]

                    profile_url = None
                    if profile_link:
                        profile_url = BASE_URL + profile_link["href"]

                    role = None
                    gender = None
                    qualifications = []
                    provider_type = "Other"

                    if profile_title:

                        parts = [
                            part.strip()
                            for part in profile_title.get_text(strip=True).split(",")
                        ]

                        if len(parts) >= 1:
                            role = parts[0]

                        if len(parts) >= 2:
                            gender = parts[1]

                        if len(parts) > 2:
                            qualifications = parts[2:]

                    if role:

                        role_lower = role.lower()

                        if "general practitioner" in role_lower:
                            provider_type = "GP"

                        elif "registrar" in role_lower:
                            provider_type = "GP"

                        elif "practice nurse" in role_lower:
                            provider_type = "Nurse"

                        elif "nurse" in role_lower:
                            provider_type = "Nurse"

                        elif "dentist" in role_lower:
                            provider_type = "Dentist"

                        elif "skin" in role_lower:
                            provider_type = "Skin Specialist"

                    bio_text = ""
                    if bio:
                        bio_text = bio.get_text(" ", strip=True)

                    areas_of_interest = [
                        item.get_text(strip=True)
                        for item in interest_items
                    ]

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

# ---------------------------------------
# Compare old data
# ---------------------------------------

old_data = None

if os.path.exists("availability.json"):

    with open("availability.json", "r", encoding="utf-8") as f:
        old_data = json.load(f)

old_compare = None

if old_data:
    old_compare = {
        "clinics": old_data.get("clinics", {})
    }

new_compare = {
    "clinics": output["clinics"]
}

if old_compare == new_compare:

    print("\n✓ No availability changes detected.")
    print("\n==========================================")
    print("Availability Update Complete")
    print("==========================================")
    print(f"Successful clinics : {output['successful_clinics']}/{output['total_clinics']}")
    print(f"Doctors scraped    : {output['total_doctors']}")
    print("\nNo file updated.")
    exit()

output["last_updated"] = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")

with open("availability.json", "w", encoding="utf-8") as f:

    json.dump(
        output,
        f,
        indent=4,
        ensure_ascii=False
    )

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
