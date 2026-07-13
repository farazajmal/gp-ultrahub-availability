import os
from playwright.sync_api import sync_playwright, TimeoutError
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import time

CLINICS = {
    "Gladstone": "https://www.hotdoc.com.au/medical-centres/gladstone-QLD-4680/gp-ultra-hub-gladstone/doctors",
    "Calliope": "https://www.hotdoc.com.au/medical-centres/calliope-QLD-4680/outback-gp/doctors",
    "Burnett Heads": "https://www.hotdoc.com.au/medical-centres/burnett-heads-QLD-4670/gp-ultra-hub-burnett-heads/doctors",
    "Toowoomba": "https://www.hotdoc.com.au/medical-centres/kearneys-spring-QLD-4350/gp-ultra-hub-toowoomba-plaza/doctors",
}

BASE_URL = "https://www.hotdoc.com.au"

MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds

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

                doctors = soup.select(".DoctorAvailability")

                clinic_results = []

                for doctor in doctors:

                    button = doctor.select_one(
                        ".DoctorAvailability-actionButton"
                    )

                    availability = doctor.select_one(
                        ".DoctorAvailability-earliestAvailable"
                    )

                    if not button or not availability:
                        continue

                    doctor_name = (
                        button.get_text(strip=True)
                        .replace("View ", "")
                    )

                    booking_url = BASE_URL + button["href"]

                    clinic_results.append({
                        "doctor": doctor_name,
                        "clinic": clinic_name,
                        "availability": availability.get_text(strip=True),
                        "booking_url": booking_url
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

output["last_updated"] = datetime.now(
    ZoneInfo("Australia/Brisbane")
).strftime("%Y-%m-%d %I:%M:%S %p AEST")

json_text = json.dumps(
    output,
    indent=4,
    ensure_ascii=False
)

file_changed = True

if os.path.exists("availability.json"):

    with open(
        "availability.json",
        "r",
        encoding="utf-8"
    ) as f:

        existing = f.read()

    if existing == json_text:
        file_changed = False

if file_changed:

    with open(
        "availability.json",
        "w",
        encoding="utf-8"
    ) as f:

        f.write(json_text)

    print("\n✓ availability.json updated.")

else:

    print("\n✓ No changes detected.")

print("\n==========================================")
print("Availability Update Complete")
print("==========================================")
print(f"Successful clinics : {output['successful_clinics']}/{output['total_clinics']}")
print(f"Doctors scraped    : {output['total_doctors']}")

if output["failed_clinics"]:

    print("\nFailed Clinics:")

    for clinic in output["failed_clinics"]:
        print(f"- {clinic['clinic']}")

else:

    print("\nAll clinics scraped successfully!")
print("==========================================")
print(f"Successful clinics : {output['successful_clinics']}/{output['total_clinics']}")
print(f"Doctors scraped    : {output['total_doctors']}")

if output["failed_clinics"]:

    print("\nFailed Clinics:")

    for clinic in output["failed_clinics"]:
        print(f"- {clinic['clinic']}")

else:

    print("\nAll clinics scraped successfully!")

print("\navailability.json updated.")
