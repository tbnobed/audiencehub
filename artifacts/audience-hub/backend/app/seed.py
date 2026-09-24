"""Deterministic, CSV-only synthetic seed data for Audience Hub.

The default output directory is ``backend/seed-data``.  ``--load`` is an
integration hook for a future real importer: it expects
``app.importer.import_seed_files(files: Mapping[str, Path])`` to synchronously
submit the generated CSV paths through the normal import-job pipeline.  This
module deliberately never writes generated records directly to the database.
"""

import csv
import json
import random
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import phonenumbers
from faker import Faker


SCALE_DENSITY = {"small": 0.5, "medium": 1.0, "large": 2.0}
CSV_FILES = (
    "donor_crm_contacts.csv",
    "giving_platform_gifts.csv",
    "esp_contacts.csv",
    "five9_calls.csv",
    "zeta_enrichment.csv",
)
FUNDS = ("General", "Missions", "Building", "Media")
CAMPAIGNS = ("Annual Appeal", "Spring Outreach", "Year-End Giving", "Community")
APPEALS = ("YE24", "SPRING24", "ANNUAL25", "DIGITAL", "DIRECT_MAIL")
INTERESTS = ("missions", "community", "education", "media", "youth", "outreach")


def _write_csv(path: Path, columns: tuple[str, ...]):
    handle = path.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    return handle, writer


def _mixed_date(value: date, rng: random.Random) -> str:
    formats = ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y")
    return value.strftime(rng.choice(formats))


def _mixed_datetime(value: datetime, rng: random.Random) -> str:
    value = value.astimezone(timezone.utc)
    if rng.randrange(3) == 0:
        return value.strftime("%Y-%m-%d %H:%M:%S+00:00")
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _valid_us_phone(fake: Faker, rng: random.Random) -> str:
    """Return a Faker-generated US number accepted by phonenumbers."""
    for _ in range(100):
        candidate = fake.phone_number()
        try:
            parsed = phonenumbers.parse(candidate, "US")
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed) and phonenumbers.region_code_for_number(parsed) == "US":
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    # Faker can produce a non-valid exchange repeatedly for some seeds. Keep
    # the fallback deterministic and validate it before allowing it into a file.
    for _ in range(1000):
        digits = fake.numerify("2##2######")
        try:
            parsed = phonenumbers.parse("+1" + digits, None)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    raise RuntimeError("Could not generate a valid Faker US phone number")


def _email(fake: Faker, rng: random.Random) -> str:
    local = fake.user_name().replace(".", "").replace("-", "_").lower()
    # Reserved example domains are safe for synthetic data. gmail.test exists
    # only to exercise Gmail-style normalization without reaching real users.
    domain = rng.choice(("example.com", "example.org", "example.net", "gmail.test"))
    return f"{local}@{domain}"


def _gmail_variant(email: str, rng: random.Random) -> str:
    local, domain = email.split("@", 1)
    if domain != "gmail.test":
        return rng.choice((email.lower(), email.upper(), f" {email} "))
    local = local.replace(".", "")
    if len(local) > 2 and rng.random() < 0.65:
        split_at = rng.randint(1, len(local) - 1)
        local = local[:split_at] + "." + local[split_at:]
    if rng.random() < 0.6:
        local += "+" + rng.choice(("esp", "newsletter", "family", "2025"))
    result = f"{local}@{domain}"
    return rng.choice((result, result.upper(), f" {result} "))


def _sample_gift_date(rng: random.Random) -> date:
    months = [month for _year in range(5) for month in range(1, 13)]
    weights = [2.7 if month in (11, 12) else 1.0 for month in months]
    selected = rng.choices(months, weights=weights, k=1)[0]
    year = rng.randint(2020, 2024)
    if selected == 2:
        day = rng.randint(1, 28)
    elif selected in (4, 6, 9, 11):
        day = rng.randint(1, 30)
    else:
        day = rng.randint(1, 31)
    return date(year, selected, day)


def _record_truth(records: dict[str, dict[str, str | None]], source: str,
                  external_id: str, person_id: str,
                  household_id: str | None) -> None:
    records[f"{source}:{external_id}"] = {
        "person_id": person_id,
        "shared_household_id": household_id,
    }


def generate_seed(profiles: int = 50_000, scale: str = "small",
                  random_seed: int = 20250308, output_dir: str | Path | None = None) -> dict[str, Any]:
    """Write five deterministic CSVs and ground_truth.json; return output metadata."""
    if profiles < 1:
        raise ValueError("--profiles must be at least 1")
    if scale not in SCALE_DENSITY:
        raise ValueError(f"Unsupported scale {scale!r}; choose small, medium, or large")

    rng = random.Random(random_seed)
    fake = Faker("en_US")
    fake.seed_instance(random_seed)
    destination = Path(output_dir) if output_dir is not None else Path(__file__).resolve().parent.parent / "seed-data"
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    density = SCALE_DENSITY[scale]

    people: list[dict[str, Any]] = []
    for index in range(1, profiles + 1):
        people.append({
            "person_id": f"person-{index:08d}",
            "external_id": f"crm-{index:08d}",
            "first_name": fake.first_name(),
            "last_name": fake.last_name(),
            "email": _email(fake, rng),
            "phone": _valid_us_phone(fake, rng),
            "address1": fake.street_address(),
            "address2": fake.secondary_address() if rng.random() < 0.12 else "",
            "city": fake.city(),
            "region": fake.state_abbr(),
            "postal_code": fake.postcode(),
            "country": "US",
            "created_at": date(2018, 1, 1) + timedelta(days=rng.randrange(2500)),
        })

    # A small number of pairs intentionally share a household phone. The
    # corresponding person/record annotations make these expected merges clear.
    household_ids: dict[str, str] = {}
    household_members: dict[str, list[str]] = {}
    household_count = profiles // 50
    household_order = list(range(profiles))
    rng.shuffle(household_order)
    for household_number in range(household_count):
        first_index, second_index = household_order[household_number * 2:household_number * 2 + 2]
        household_id = f"household-{household_number + 1:06d}"
        first, second = people[first_index], people[second_index]
        second["phone"] = first["phone"]
        for member in (first, second):
            household_ids[member["person_id"]] = household_id
            household_members.setdefault(household_id, []).append(member["person_id"])

    records: dict[str, dict[str, str | None]] = {}
    counts = {name: 0 for name in CSV_FILES}

    crm_path = destination / "donor_crm_contacts.csv"
    crm_handle, crm_writer = _write_csv(crm_path, (
        "external_id", "email", "phone", "first_name", "last_name", "address1",
        "address2", "city", "region", "postal_code", "country", "created_at",
    ))
    try:
        for index, person in enumerate(people):
            row = {key: person[key] for key in (
                "external_id", "email", "phone", "first_name", "last_name",
                "address1", "address2", "city", "region", "postal_code", "country",
            )}
            row["created_at"] = _mixed_date(person["created_at"], rng)
            # Intentional junk/blocklisted identifiers and malformed phone
            # values exercise validation without introducing a real email domain.
            if index % 113 == 0:
                row["email"] = "noemail@example.com"
            if index % 97 == 0:
                row["phone"] = "(000) 000-0000"
            crm_writer.writerow(row)
            _record_truth(records, "donor_crm", person["external_id"], person["person_id"],
                          household_ids.get(person["person_id"]))
            counts[CSV_FILES[0]] += 1
            if index % 101 == 0:
                duplicate = dict(row)
                duplicate["external_id"] = f"{person['external_id']}-dup"
                crm_writer.writerow(duplicate)
                _record_truth(records, "donor_crm", duplicate["external_id"], person["person_id"],
                              household_ids.get(person["person_id"]))
                counts[CSV_FILES[0]] += 1
    finally:
        crm_handle.close()

    # Exactly 40% email matches and 10% phone-only matches (within rounding).
    # Shuffling prevents overlap categories from correlating with generated IDs.
    overlap_order = list(range(profiles))
    rng.shuffle(overlap_order)
    email_overlap = set(overlap_order[:round(profiles * 0.40)])
    phone_overlap = set(overlap_order[round(profiles * 0.40):round(profiles * 0.50)])
    esp_path = destination / "esp_contacts.csv"
    esp_handle, esp_writer = _write_csv(esp_path, (
        "external_id", "contact_external_id", "email", "phone", "first_name", "last_name",
        "email_consent", "sms_consent", "consent_captured_at", "hard_bounce", "bounce_reason",
    ))
    try:
        for index, person in enumerate(people):
            if index not in email_overlap and index not in phone_overlap:
                continue
            email = (_gmail_variant(person["email"], rng) if index in email_overlap
                     else _email(fake, rng))
            bounced = rng.random() < 0.025
            esp_id = f"esp-{index + 1:08d}"
            captured = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=rng.randrange(1826))
            esp_writer.writerow({
                "external_id": esp_id,
                "contact_external_id": person["external_id"],
                "email": email,
                "phone": person["phone"],
                "first_name": person["first_name"],
                "last_name": person["last_name"],
                "email_consent": rng.choices(("opted_in", "opted_out", "unknown"), (0.72, 0.13, 0.15))[0],
                "sms_consent": rng.choices(("opted_in", "opted_out", "unknown"), (0.46, 0.16, 0.38))[0],
                "consent_captured_at": _mixed_datetime(captured, rng),
                "hard_bounce": str(bounced).lower(),
                "bounce_reason": "hard_bounce" if bounced else "",
            })
            _record_truth(records, "esp", esp_id, person["person_id"],
                          household_ids.get(person["person_id"]))
            counts[CSV_FILES[2]] += 1
            if index % 173 == 0:
                duplicate_id = f"{esp_id}-dup"
                duplicate = {
                    "external_id": duplicate_id,
                    "contact_external_id": person["external_id"],
                    "email": email,
                    "phone": person["phone"],
                    "first_name": person["first_name"],
                    "last_name": person["last_name"],
                    "email_consent": "unknown",
                    "sms_consent": "unknown",
                    "consent_captured_at": _mixed_datetime(captured, rng),
                    "hard_bounce": "false",
                    "bounce_reason": "",
                }
                esp_writer.writerow(duplicate)
                _record_truth(records, "esp", duplicate_id, person["person_id"],
                              household_ids.get(person["person_id"]))
                counts[CSV_FILES[2]] += 1
    finally:
        esp_handle.close()

    gifts_path = destination / "giving_platform_gifts.csv"
    gifts_handle, gifts_writer = _write_csv(gifts_path, (
        "external_id", "contact_external_id", "email", "phone", "amount", "currency",
        "gift_date", "fund", "campaign", "appeal_code", "channel", "payment_method",
        "is_recurring", "recurring_plan_id",
    ))
    gift_rng = random.Random(random_seed ^ 0xBEEF)
    try:
        for person_index, person in enumerate(people):
            recurring = gift_rng.random() < 0.15
            recurring_plan = f"plan-{person_index + 1:08d}" if recurring else ""
            gift_number = 0
            dates = []
            if recurring:
                for year in range(2020, 2025):
                    for month in range(1, 13):
                        if year == 2024 and month > 12:
                            continue
                        day = min(gift_rng.randint(1, 28), 28)
                        dates.append((date(year, month, day), True))
            for _ in range(max(0, int(gift_rng.expovariate(1 / (2.2 * density))))):
                dates.append((_sample_gift_date(gift_rng), False))
            for gift_date, is_recurring in dates:
                gift_number += 1
                amount = min(25000.0, max(5.0, gift_rng.lognormvariate(3.75, 0.85)))
                gift_id = f"gift-{person_index + 1:08d}-{gift_number:03d}"
                gifts_writer.writerow({
                    "external_id": gift_id,
                    "contact_external_id": person["external_id"],
                    "email": person["email"],
                    "phone": person["phone"],
                    "amount": f"{amount:.2f}",
                    "currency": "USD",
                    "gift_date": _mixed_date(gift_date, gift_rng),
                    "fund": gift_rng.choice(FUNDS),
                    "campaign": gift_rng.choice(CAMPAIGNS),
                    "appeal_code": gift_rng.choice(APPEALS),
                    "channel": gift_rng.choice(("online", "mail", "phone", "event", "other")),
                    "payment_method": gift_rng.choice(("card", "ach", "check", "cash")),
                    "is_recurring": str(is_recurring).lower(),
                    "recurring_plan_id": recurring_plan if is_recurring else "",
                })
                _record_truth(records, "giving_platform", gift_id, person["person_id"],
                              household_ids.get(person["person_id"]))
                counts[CSV_FILES[1]] += 1
    finally:
        gifts_handle.close()

    calls_path = destination / "five9_calls.csv"
    calls_handle, calls_writer = _write_csv(calls_path, (
        "external_id", "message_id", "email", "phone", "type", "name",
        "occurred_at", "received_at", "properties", "context",
    ))
    event_rng = random.Random(random_seed ^ 0xF1F9)
    try:
        for person_index, person in enumerate(people):
            call_count = int(event_rng.expovariate(1 / (0.75 * density)))
            for call_number in range(call_count):
                occurred = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(
                    days=event_rng.randrange(1826), seconds=event_rng.randrange(86400))
                call_id = f"call-{person_index + 1:08d}-{call_number + 1:03d}"
                calls_writer.writerow({
                    "external_id": call_id,
                    "message_id": call_id,
                    "email": person["email"],
                    "phone": person["phone"],
                    "type": "track",
                    "name": event_rng.choice(("Inbound Call", "Outbound Call", "Call Connected", "Voicemail")),
                    "occurred_at": _mixed_datetime(occurred, event_rng),
                    "received_at": _mixed_datetime(occurred + timedelta(seconds=event_rng.randrange(1, 3600)), event_rng),
                    "properties": json.dumps({
                        "direction": event_rng.choice(("inbound", "outbound")),
                        "duration_seconds": event_rng.randint(0, 2400),
                        "disposition": event_rng.choice(("connected", "voicemail", "no_answer", "follow_up")),
                    }, sort_keys=True, separators=(",", ":")),
                    "context": json.dumps({"channel": "phone", "vendor": "five9"}, sort_keys=True),
                })
                _record_truth(records, "five9", call_id, person["person_id"],
                              household_ids.get(person["person_id"]))
                counts[CSV_FILES[3]] += 1
    finally:
        calls_handle.close()

    enrichment_path = destination / "zeta_enrichment.csv"
    enrichment_handle, enrichment_writer = _write_csv(enrichment_path, (
        "external_id", "contact_external_id", "email", "phone", "hh_income_band",
        "age_band", "interests", "donor_propensity_score", "enriched_at",
    ))
    enrichment_rng = random.Random(random_seed ^ 0x2E7A)
    try:
        for index, person in enumerate(people):
            enrichment_id = f"zeta-{index + 1:08d}"
            enriched = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=enrichment_rng.randrange(366))
            enrichment_writer.writerow({
                "external_id": enrichment_id,
                "contact_external_id": person["external_id"],
                "email": person["email"],
                "phone": person["phone"],
                "hh_income_band": enrichment_rng.choice(("under_35k", "35k_74k", "75k_149k", "150k_plus")),
                "age_band": enrichment_rng.choice(("18_24", "25_34", "35_44", "45_54", "55_64", "65_plus")),
                "interests": enrichment_rng.choice(INTERESTS),
                "donor_propensity_score": f"{enrichment_rng.random():.4f}",
                "enriched_at": _mixed_datetime(enriched, enrichment_rng),
            })
            _record_truth(records, "zeta_enrichment", enrichment_id, person["person_id"],
                          household_ids.get(person["person_id"]))
            counts[CSV_FILES[4]] += 1
    finally:
        enrichment_handle.close()

    ground_truth = {
        "format_version": 1,
        "random_seed": random_seed,
        "scale": scale,
        "requested_people": profiles,
        "record_count": len(records),
        "records": records,
        "shared_households": household_members,
    }
    ground_truth_path = destination / "ground_truth.json"
    with ground_truth_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(ground_truth, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")

    return {
        "output_dir": destination,
        "files": {name: destination / name for name in CSV_FILES},
        "ground_truth": ground_truth_path,
        "counts": counts,
        "record_count": len(records),
    }


def load_generated_files(files: dict[str, Path]) -> None:
    """Call the documented real-importer hook, or fail explicitly if absent."""
    import importlib
    import importlib.util

    if importlib.util.find_spec("app.importer") is None:
        raise RuntimeError(
            "--load requested, but app.importer.import_seed_files is not available. "
            "Implement that callable to submit the CSV paths through the real import-job pipeline."
        )
    importer = importlib.import_module("app.importer")
    hook: Callable[[dict[str, Path]], Any] | None = getattr(importer, "import_seed_files", None)
    if not callable(hook):
        raise RuntimeError(
            "--load requested, but app.importer.import_seed_files(files: Mapping[str, Path]) "
            "is not callable."
        )
    hook(files)