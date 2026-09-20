import logging
from scrape_availability import scrape_availability, build_patches_from_slots
from scrape_doctors import scrape_doctor_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def scrape_all_clinics(days_ahead=14):
    scrape_availability(days_ahead=days_ahead)


def main():
    scrape_availability(days_ahead=14)


if __name__ == "__main__":
    main()
