import os
import json
import re
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


logger = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

# Number of doctors scraped simultaneously.
# Start with 3. Increase to 4 if HotDoc/runners handle it well.
MAX_WORKERS = 3

PAGE_LOAD_TIMEOUT = 15000
ELEMENT_TIMEOUT = 4000
GRID_TIMEOUT = 8000


# ============================================================
# TIME / DATE PARSING
# ============================================================

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

    if month_idx < now.month - 6:
        year += 1

    try:
        return datetime(year, month_idx, day_num)
    except Exception:
        return None


# ============================================================
# AVAILABILITY PATCH BUILDER
# ============================================================

def build_patches_from_slots(raw_slots, date_headers=None):
    """Helper function to build structured time patches from raw time slots."""

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

            if h_idx < len(date_headers):

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


# ============================================================
# SAFE CLICK HELPERS
# ============================================================

def click_if_present(page, locator, timeout):
    """
    Click an element if it becomes visible.

    Unlike the original fixed sleeps, this doesn't wait longer
    than necessary once the element is ready.
    """

    try:
        element = page.locator(locator).first

        if element.is_visible(timeout=timeout):
            element.click(timeout=timeout)
            return True

    except Exception:
        pass

    return False


def wait_for_any(page, locators, timeout):
    """
    Wait for the first available element from a list.
    """

    for locator in locators:

        try:
            element = page.locator(locator).first

            if element.is_visible(timeout=timeout):
                return True

        except Exception:
            continue

    return False


# ============================================================
# DOCTOR SCRAPER
# ============================================================

def scrape_doctor_proven(page, clinic_slug, doctor_slug):

    url = (
        "https://www.hotdoc.com.au/request/consult/for"
        f"?defaults=practice-{clinic_slug},practitioner-{doctor_slug}"
    )

    try:

        logger.info(
            f"[{doctor_slug}] Loading HotDoc..."
        )

        # ----------------------------------------------------
        # Initial navigation
        # ----------------------------------------------------

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_LOAD_TIMEOUT
        )


        # ----------------------------------------------------
        # Step 1: For myself
        #
        # SAME ACTION AS YOUR ORIGINAL SCRIPT
        # ----------------------------------------------------

        clicked = click_if_present(
            page,
            "text='For myself'",
            4000
        )

        if clicked:

            # Instead of blindly waiting 1.5 sec,
            # wait for the next screen.
            wait_for_any(
                page,
                [
                    "text='Existing patient'",
                    "text='New patient'",
                    "button:has-text('Appointment')",
                    ".flow-button"
                ],
                4000
            )


        # ----------------------------------------------------
        # Step 2: Existing patient
        #
        # SAME ACTION AS YOUR ORIGINAL SCRIPT
        # ----------------------------------------------------

        clicked = click_if_present(
            page,
            "text='Existing patient'",
            3000
        )

        if clicked:

            wait_for_any(
                page,
                [
                    "button:has-text('Appointment')",
                    ".flow-button",
                    "text='Choose a time'"
                ],
                4000
            )


        # ----------------------------------------------------
        # Step 3: Reason / Appointment
        #
        # SAME ACTION AS YOUR ORIGINAL SCRIPT
        # ----------------------------------------------------

        clicked = click_if_present(
            page,
            "button:has-text('Appointment'), .flow-button",
            3000
        )

        if clicked:

            wait_for_any(
                page,
                [
                    "text='Continue'",
                    "text='Choose a time'",
                    "text=/\\d{1,2}:\\d{2}\\s*(am|pm)/i"
                ],
                4000
            )


        # ----------------------------------------------------
        # Step 4: Continue if present
        #
        # SAME ACTION AS YOUR ORIGINAL SCRIPT
        # ----------------------------------------------------

        clicked = click_if_present(
            page,
            "text='Continue'",
            2000
        )

        if clicked:

            wait_for_any(
                page,
                [
                    "text='Choose a time'",
                    "text=/\\d{1,2}:\\d{2}\\s*(am|pm)/i"
                ],
                GRID_TIMEOUT
            )


        # ----------------------------------------------------
        # Ensure grid is rendered
        #
        # Original code checked body text and then slept 3 sec.
        #
        # We instead poll the DOM for the actual content.
        # ----------------------------------------------------

        time_regex = re.compile(
            r"^\d{1,2}:\d{2}\s*(?:am|pm)$",
            re.IGNORECASE
        )

        grid_ready = False

        for _ in range(40):
            try:

                text = page.locator("body").inner_text(
                    timeout=2000
                )

                if (
                    "Choose a time" in text
                    or any(
                        time_regex.match(
                            line.strip()
                        )
                        for line in text.split("\n")
                    )
                ):
                    grid_ready = True
                    break

            except Exception:
                pass

            # Tiny polling interval instead of 3-second sleep.
            page.wait_for_timeout(200)


        if not grid_ready:

            logger.warning(
                f"[{doctor_slug}] "
                "Timeslot grid not detected"
            )


        # ----------------------------------------------------
        # ONE body read
        # ----------------------------------------------------

        text = page.locator("body").inner_text(
            timeout=5000
        )

        lines = [
            l.strip()
            for l in text.split("\n")
            if l.strip()
        ]


        # ----------------------------------------------------
        # Parse date headers
        # ----------------------------------------------------

        date_pattern = re.compile(
            r"^(\d{1,2})\s+([A-Za-z]{3})$"
        )

        header_dates = []

        for idx, l in enumerate(lines):

            m = date_pattern.match(l)

            if not m:
                continue

            date_obj = parse_date_header(l)

            if date_obj:

                header_dates.append({
                    "date_obj": date_obj,
                    "day_name": date_obj.strftime("%A"),
                    "date_str": date_obj.strftime("%Y-%m-%d"),
                    "raw": l,
                    "label": (
                        f"{date_obj.strftime('%A')}, "
                        f"{date_obj.strftime('%b %d')}"
                    )
                })


        # ----------------------------------------------------
        # Parse time slots
        # ----------------------------------------------------

        raw_times = []

        for l in lines:

            if time_regex.match(l):
                raw_times.append(l.strip())


        # ----------------------------------------------------
        # Build patches
        # ----------------------------------------------------

        patches = build_patches_from_slots(
            raw_times,
            header_dates
        )


        logger.info(
            f"[{doctor_slug}] "
            f"Scraped {len(patches)} day patches "
            f"({len(raw_times)} total slots)"
        )

        return patches


    except PlaywrightTimeoutError as e:

        logger.error(
            f"[{doctor_slug}] Timeout: {e}"
        )

        return []


    except Exception as e:

        logger.error(
            f"[{doctor_slug}] Error scraping: {e}"
        )

        return []


# ============================================================
# SINGLE DOCTOR WORKER
# ============================================================

def scrape_single_doctor(browser, clinic_name, doc):
    """
    Scrape one doctor using a new page inside the SAME browser/context.

    Browser creation is expensive, so we don't create a browser
    for every doctor.
    """

    doc_name = doc["doctor"]
    clinic_slug = doc["clinic_slug"]
    doctor_slug = doc["doctor_slug"]

    logger.info(
        f"Scraping {doc_name} ({doctor_slug})..."
    )

    page = browser.new_page(
        viewport={
            "width": 1280,
            "height": 900
        }
    )

    page.set_default_timeout(
        ELEMENT_TIMEOUT
    )

    page.set_default_navigation_timeout(
        PAGE_LOAD_TIMEOUT
    )

    try:

        patches = scrape_doctor_proven(
            page,
            clinic_slug,
            doctor_slug
        )

    finally:

        page.close()


    # --------------------------------------------------------
    # Preserve your original output structure
    # --------------------------------------------------------

    doc_data = dict(doc)

    doc_data["availability_patches"] = patches
    doc_data["patches"] = patches


    if patches:

        p0 = patches[0]

        d_name = (
            p0.get("day_name")
            or p0.get("date")
        )

        date_lbl = (
            p0.get("date_label")
            or ""
        )

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


    return clinic_name, doc_name, doc_data


# ============================================================
# MAIN SCRAPER
# ============================================================

def scrape_availability(days_ahead=14):

    from playwright.sync_api import sync_playwright


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


    # --------------------------------------------------------
    # Prepare doctor jobs
    # --------------------------------------------------------

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
        f"Starting scraper for {len(jobs)} doctors "
        f"with {MAX_WORKERS} workers"
    )


    # --------------------------------------------------------
    # ONE Playwright instance
    # ONE Chromium browser
    # ONE context
    #
    # Pages are created per doctor and closed afterward.
    # --------------------------------------------------------

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox"
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


        # ----------------------------------------------------
        # Block resources that aren't needed for scraping.
        #
        # We KEEP JS and CSS because HotDoc needs them.
        #
        # We only block:
        #   images
        #   fonts
        #   media
        #
        # This reduces bandwidth and rendering overhead.
        # ----------------------------------------------------

        def handle_route(route):

            resource_type = (
                route.request.resource_type
            )

            if resource_type in {
                "image",
                "font",
                "media"
            }:

                route.abort()

            else:

                route.continue_()


        context.route(
            "**/*",
            handle_route
        )


        # ----------------------------------------------------
        # Parallel doctor scraping
        # ----------------------------------------------------

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            futures = {}

            for clinic_name, doc in jobs:

                future = executor.submit(
                    scrape_single_doctor,
                    context,
                    clinic_name,
                    doc
                )

                futures[future] = (
                    clinic_name,
                    doc
                )


            for future in as_completed(
                futures
            ):

                clinic_name, doc = futures[
                    future
                ]

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
                        f"Failed scraping "
                        f"{doc.get('doctor', 'unknown')}: "
                        f"{e}"
                    )


                    # Preserve doctor in output
                    # even if scraping fails.

                    doc_data = dict(doc)

                    doc_data[
                        "availability_patches"
                    ] = []

                    doc_data[
                        "patches"
                    ] = []

                    doc_data[
                        "availability"
                    ] = "Call clinic to book"


                    availability[
                        clinic_name
                    ][doc["doctor"]] = doc_data


        browser.close()


    # --------------------------------------------------------
    # Save JSON
    # --------------------------------------------------------

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
        f"\nSUCCESS: Saved updated live availability "
        f"to {out_file}"
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    scrape_availability()
