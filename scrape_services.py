import json
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://gpultrahub.com.au"
SERVICES_PAGE_URL = f"{BASE_URL}/services/"

OUTPUT_FILE = "services.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GPUltraHubBot/1.0)"
}


def get_soup(url):
    response = requests.get(url, timeout=20, headers=HEADERS)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def scrape_services_list():
    """Get the list of services with their names, short teasers, and URLs
    from the main /services/ listing page."""

    soup = get_soup(SERVICES_PAGE_URL)

    services = []

    for link in soup.find_all("a", href=True):

        heading = link.select_one(".elementor-heading-title")

        if not heading:
            continue

        description_el = link.select_one(".elementor-widget-text-editor p")
        image_el = link.select_one("img")

        name = heading.get_text(strip=True)
        teaser = description_el.get_text(strip=True) if description_el else ""
        image_url = image_el["src"] if image_el and image_el.has_attr("src") else None
        url = link["href"]

        if any(s["url"] == url for s in services):
            continue

        services.append({
            "name": name,
            "teaser": teaser,
            "url": url,
            "image_url": image_url,
        })

    return services


def scrape_service_detail(url):
    """Visit an individual service page and pull the full description,
    preserving bullet points as a readable list."""

    soup = get_soup(url)

    entry = soup.select_one("div.entry-content")

    if not entry:
        return ""

    lines = []

    for widget in entry.select(".elementor-widget-text-editor .elementor-widget-container"):

        found_structured_content = False

        for child in widget.find_all(["p", "ul"], recursive=False):

            if child.name == "p":
                text = child.get_text(" ", strip=True)
                if text:
                    lines.append(text)
                    found_structured_content = True

            elif child.name == "ul":
                for li in child.find_all("li"):
                    text = li.get_text(" ", strip=True)
                    if text:
                        lines.append(f"- {text}")
                        found_structured_content = True

        # Some widgets have plain text with no <p>/<ul> wrapper at all
        if not found_structured_content:
            text = widget.get_text(" ", strip=True)
            if text:
                lines.append(text)

    return "\n".join(lines)


def scrape_clinic_locations(sample_page_url):
    """Extract clinic name, address, and phone number from the site
    footer, which is the same across every page."""

    soup = get_soup(sample_page_url)

    footer = soup.select_one("footer")

    if not footer:
        return []

    clinics = []

    for li in footer.select("li.elementor-icon-list-item"):

        name_el = li.select_one("h6")

        if not name_el:
            continue

        name = name_el.get_text(strip=True)

        text_container = li.select_one(".elementor-icon-list-text")
        phone_el = text_container.select_one("b") if text_container else None
        phone = phone_el.get_text(strip=True) if phone_el else None

        full_text = text_container.get_text(" ", strip=True) if text_container else ""
        address = full_text.replace(name, "", 1)
        if phone:
            address = address.replace(phone, "")
        address = address.strip(" ,")

        clinics.append({
            "clinic": name,
            "address": address,
            "phone": phone,
        })

    return clinics


def main():

    print("Scraping services list...")
    services = scrape_services_list()
    print(f"Found {len(services)} services.")

    for service in services:

        print(f"  Fetching details: {service['name']}")

        try:
            service["details"] = scrape_service_detail(service["url"])
        except Exception as e:
            print(f"  ! Failed to fetch details for {service['name']}: {e}")
            service["details"] = ""

        time.sleep(1)  # be polite to the server

    print("\nScraping clinic locations from footer...")

    clinic_locations = []

    if services:
        try:
            clinic_locations = scrape_clinic_locations(services[0]["url"])
            print(f"Found {len(clinic_locations)} clinic locations.")
        except Exception as e:
            print(f"! Failed to fetch clinic locations: {e}")

    output = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
        "total_services": len(services),
        "services": services,
        "clinic_locations": clinic_locations,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)

    print(f"\n✓ {OUTPUT_FILE} updated.")
    print(f"✓ {len(services)} services, {len(clinic_locations)} clinic locations.")


if __name__ == "__main__":
    main()
