import os
import json
import re
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

# Number of doctors scraped simultaneously.
# 4 is a good starting point for GitHub Actions.
MAX_WORKERS = 4

# Keep browser/page timeouts reasonably tight.
PAGE_TIMEOUT = 15000
ELEMENT_TIMEOUT = 4000


def parse_time_to_minutes(t_str):
    t_str = t_str.strip().lower()

    try:
        dt = datetime.strptime(t_str, "%I:%M %p")
        return dt.hour * 60 + dt.minute
    except Exception:
        return -1


def parse_date_header(raw_header):
    month_names = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
    ]

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

    # Handle dates around the year boundary.
    if month_idx < now.month - 6:
        year += 1

    try:
        return datetime(year, month_idx, day_num)
    except Exception:
        return None


def build_patches_from_slots(raw_slots, date_headers=None):
    """Build structured availability patches from raw time slots."""

    if not raw_slots:
        return []

    day_groups = []
    current_group = []
    prev_minutes = -1

    for t in raw_slots:
        minutes = parse_time_to_minutes(t)

        if prev_minutes != -1 and minutes <= prev_minutes:
            day_groups.append(current_group)
            current_group = []

        current_group.append(t)
        prev_minutes = minutes

    if current_group:
        day_groups.append(current_group)

    patches = []

    if date_headers and len(date_headers) >= len(day_groups):
        start_offset = len(date_headers) - len(day_groups)

        for group_idx, discrete_slots in enumerate(day_groups):
            h_idx = start_offset + group_idx

            if h_idx >= len(date_headers):
                continue

            hd = date_headers[h_idx]

            start_t = discrete_slots[0]
            end_t = discrete_slots[-1]

            display_time = (
                f"{start_t} - {end_t}"
                if len(discrete_slots) > 1
                else start_t
            )

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
        for discrete_slots in day_groups:
            start_t = discrete_slots[0]
            end_t = discrete_slots[-1]

            display_time = (
                f"{start_t} - {end_t}"
                if len(discrete_slots) > 1
                else start_t
            )

            patches.append({
                "start_time": start_t,
                "end_time": end_t,
                "time_range": display_time,
                "discrete_slots": discrete_slots,
                "display": display_time,
                "display_full": display_time
            })

    return patches


def safe_click(page, locator, timeout=ELEMENT_TIMEOUT):
    """
    Click an element if it exists.
    Returns True if clicked, False otherwise.
    """
    try:
        element = page.locator(locator).first

        if element.is_visible(timeout=timeout):
            element.click(timeout=timeout)
            return True

    except Exception:
        pass

    return False


def wait_for_any(page, selectors, timeout=ELEMENT_TIMEOUT):
    """
    Wait until any one of the supplied selectors becomes visible.
    """
    for selector in selectors:
        try:
            locator = page.locator(selector).first

            if locator.is_visible(timeout=timeout):
                return True

        except Exception:
            continue

    return False


def scrape_doctor_proven(page, clinic_slug, doctor_slug):
    url = (
        "https://www.hotdoc.com.au/request/consult/for"
        f"?defaults=practice-{clinic_slug},practitioner-{doctor_slug}"
    )

    try:
        logger.info(f"[{doctor_slug}] Opening page")

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        # ---------------------------------------------------------
        # Step 1: For myself
        # ---------------------------------------------------------
        if safe_click(
            page,
            "text='For myself'"
        ):
            # Wait for the next step rather than sleeping.
            wait_for_any(
                page,
                [
                    "text='Existing patient'",
                    "text='New patient'",
                    "button:has-text('Appointment')",
                    ".flow-button"
                ],
                timeout=ELEMENT_TIMEOUT
            )

        # ---------------------------------------------------------
        # Step 2: Existing patient
        # ---------------------------------------------------------
        if safe_click(
            page,
            "text='Existing patient'"
        ):
            wait_for_any(
                page,
                [
                    "button:has-text('Appointment')",
                    ".flow-button",
                    "text='Choose a time'"
                ],
                timeout=ELEMENT_TIMEOUT
            )

        # ---------------------------------------------------------
        # Step 3: Appointment / reason
        # ---------------------------------------------------------
        safe_click(
            page,
            "button:has-text('Appointment'), .flow-button"
        )

        # ---------------------------------------------------------
        # Step 4: Continue if required
        # ---------------------------------------------------------
        safe_click(
            page,
            "text='Continue'"
        )

        # ---------------------------------------------------------
        # Wait for actual availability UI.
        #
        # Instead of arbitrary sleeps, wait until the page contains
        # either "Choose a time" or recognizable time slots.
        # ---------------------------------------------------------
        wait_for_any(
            page,
            [
                "text='Choose a time'",
                "text=/\\d{1,2}:\\d{2}\\s*(am|pm)/i"
            ],
            timeout=8000
        )

        # One final body read.
        text = page.locator("body").inner_text(timeout=5000)

        lines = [
            line.strip()
            for line in text.split("\n")
            if line.strip()
        ]

        # ---------------------------------------------------------
        # Parse dates
        # ---------------------------------------------------------
        date_pattern = re.compile(
            r"^(\d{1,2})\s+([A-Za-z]{3})$"
        )

        header_dates = []

        for idx, line in enumerate(lines):
            match = date_pattern.match(line)

            if not match:
                continue

            date_obj = parse_date_header(line)

            if date_obj:
                header_dates.append({
                    "date_obj": date_obj,
                    "day_name": date_obj.strftime("%A"),
                    "date_str": date_obj.strftime("%Y-%m-%d"),
                    "raw": line,
                    "label": (
                        f"{date_obj.strftime('%A')}, "
                        f"{date_obj.strftime('%b %d')}"
                    )
                })

        # ---------------------------------------------------------
        # Parse times
        # ---------------------------------------------------------
        time_regex = re.compile(
            r"^(\d{1,2}:\d{2}\s*(?:am|pm))$",
            re.IGNORECASE
        )

        raw_times = []

        for line in lines:
            if time_regex.match(line):
                raw_times.append(line.strip())

        patches = build_patches_from_slots(
            raw_times,
            header_dates
        )

        logger.info(
            f"[{doctor_slug}] "
            f"Found {len(patches)} day patches "
            f"({len(raw_times)} slots)"
        )

        return patches

    except PlaywrightTimeoutError as e:
        logger.warning(
            f"[{doctor_slug}] Timeout: {e}"
        )
        return []

    except Exception as e:
        logger.error(
            f"[{doctor_slug}] Error scraping: {e}"
        )
        return []


def scrape_single_doctor(args):
    """
    Worker function.

    Each thread gets its own Playwright instance/browser/page.
    This avoids sharing Playwright objects between threads.
    """

    clinic_name, doc = args

    doc_name = doc["doctor"]
    clinic_slug = doc["clinic_slug"]
    doctor_slug = doc["doctor_slug"]

    logger.info(
        f"[START] {doc_name} ({doctor_slug})"
    )

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
            ]
        )

        context = browser.new_context(
            viewport={
                "width": 1280,
                "height": 900
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )

        # ---------------------------------------------------------
        # Block resources that aren't required for availability data.
        # This can significantly reduce page load time.
        # ---------------------------------------------------------
        def handle_route(route):
            request = route.request
            resource_type = request.resource_type

            if resource_type in {
                "image",
                "font",
                "media"
            }:
                route.abort()
            else:
                route.continue_()

        context.route("**/*", handle_route)

        page = context.new_page()

        page.set_default_timeout(ELEMENT_TIMEOUT)
        page.set_default_navigation_timeout(PAGE_TIMEOUT)

        patches = scrape_doctor_proven(
            page,
            clinic_slug,
            doctor_slug
        )

        browser.close()

    # Build output in exactly the same structure as before.
    doc_data = dict(doc)

    doc_data["availability_patches"] = patches
    doc_data["patches"] = patches

    if patches:
        p0 = patches[0]

        d_name = (
            p0.get("day_name")
            or p0.get("date")
        )

        date_lbl = p0.get("date_label") or ""

        times = [
            p.get("display")
            for p in patches
            if (
                p.get("day_name") == d_name
                or p.get("date") == d_name
            )
        ]

        prefix = (
            f"{d_name}, {date_lbl}"
            if date_lbl
            else d_name
        )

        if times and prefix:
            doc_data["availability"] = (
                f"{prefix} from "
                + " and ".join(times)
            )
        else:
            doc_data["availability"] = (
                "Call clinic to book"
            )

    else:
        doc_data["availability"] = (
            "Call clinic to book"
        )

    logger.info(
        f"[DONE] {doc_name} ({doctor_slug})"
    )

    return clinic_name, doc_name, doc_data


def scrape_availability(days_ahead=14):
    """
    Scrape all doctors concurrently.
    """

    meta_path = "doctors_metadata.json"

    if not os.path.exists(meta_path):
        logger.error(
            f"Metadata file missing at {meta_path}"
        )
        return

    with open(
        meta_path,
        "r",
        encoding="utf-8"
    ) as f:
        metadata = json.load(f)

    availability = {}

    # ---------------------------------------------------------
    # Flatten all doctors into one list.
    # ---------------------------------------------------------
    jobs = []

    for clinic_name, docs in metadata.get(
        "clinics",
        {}
    ).items():

        availability[clinic_name] = {}

        for doc in docs:
            jobs.append(
                (
                    clinic_name,
                    doc
                )
            )

    logger.info(
        f"Starting scrape for {len(jobs)} doctors "
        f"using {MAX_WORKERS} workers"
    )

    # ---------------------------------------------------------
    # Run doctors concurrently.
    # ---------------------------------------------------------
    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                scrape_single_doctor,
                job
            ): job
            for job in jobs
        }

        for future in as_completed(futures):

            clinic_name, doc = futures[future]

            try:
                (
                    result_clinic,
                    doc_name,
                    doc_data
                ) = future.result()

                availability[
                    result_clinic
                ][doc_name] = doc_data

            except Exception as e:
                logger.error(
                    f"Failed processing "
                    f"{doc.get('doctor', 'unknown')}: {e}"
                )

                # Preserve doctor even if scraping failed.
                doc_data = dict(doc)
                doc_data["availability_patches"] = []
                doc_data["patches"] = []
                doc_data["availability"] = (
                    "Call clinic to book"
                )

                availability[
                    clinic_name
                ][doc["doctor"]] = doc_data

    # ---------------------------------------------------------
    # Save output.
    # ---------------------------------------------------------
    out_file = "availability.json"

    with open(
        out_file,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            availability,
            f,
            indent=2
        )

    logger.info(
        f"SUCCESS: Saved live availability "
        f"to {out_file}"
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    scrape_availability()
