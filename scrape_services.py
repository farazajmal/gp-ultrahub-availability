import json
from datetime import datetime

import requests
from bs4 import BeautifulSoup

SERVICES_PAGE_URL = "https://gpultrahub.com.au/services/"

OUTPUT_FILE = "services.json"


def scrape_services():

    response = requests.get(SERVICES_PAGE_URL, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    services = []

    # Every service card is an <a> tag that contains a heading (h3)
    # and a short description (p inside a text-editor widget).
    # This works regardless of how many services exist or how they're
    # nested, so new services added in the future are picked up
    # automatically without any code changes.
    for link in soup.find_all("a", href=True):

        heading = link.select_one(".elementor-heading-title")

        if not heading:
            continue

        description_el = link.select_one(".elementor-widget-text-editor p")
        image_el = link.select_one("img")

        name = heading.get_text(strip=True)
        description = description_el.get_text(strip=True) if description_el else ""
        image_url = image_el["src"] if image_el and image_el.has_attr("src") else None

        # Avoid duplicates if the same card structure appears twice
        if any(s["url"] == link["href"] for s in services):
            continue

        services.append({
            "name": name,
            "description": description,
            "url": link["href"],
            "image_url": image_url,
        })

    return services


def main():

    services = scrape_services()

    output = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
        "total_services": len(services),
        "services": services,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)

    print(f"\n✓ Scraped {len(services)} services.")

    for s in services:
        print(f"- {s['name']}")

    print(f"\n✓ {OUTPUT_FILE} updated.")


if __name__ == "__main__":
    main()
