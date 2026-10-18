"""Deterministic fictional display data, independent of the audit's event RNG."""
import random


# Relative frequency weights emphasize common US names in older adult cohorts.
# These are display fixtures, not identities sampled from a people database.
FIRST_NAMES = (
    ("James", 52), ("Mary", 51), ("Robert", 49), ("John", 48),
    ("Patricia", 39), ("Michael", 38), ("Linda", 36), ("William", 35),
    ("Barbara", 33), ("David", 32), ("Elizabeth", 30), ("Richard", 29),
    ("Susan", 28), ("Joseph", 27), ("Thomas", 26), ("Margaret", 25),
    ("Charles", 24), ("Nancy", 23), ("Karen", 22), ("Daniel", 21),
    ("Betty", 20), ("Paul", 19), ("Sandra", 18), ("Donald", 17),
    ("Dorothy", 16), ("Mark", 15), ("Carol", 15), ("Steven", 14),
    ("Ruth", 13), ("Sharon", 12), ("Kenneth", 12), ("Deborah", 11),
    ("George", 11), ("Donna", 10), ("Edward", 10), ("Shirley", 9),
    ("Ronald", 9), ("Cynthia", 8), ("Anthony", 8), ("Helen", 7),
)
LAST_NAMES = (
    ("Smith", 244), ("Johnson", 193), ("Williams", 163), ("Brown", 144),
    ("Jones", 143), ("Garcia", 117), ("Miller", 116), ("Davis", 111),
    ("Rodriguez", 109), ("Martinez", 106), ("Hernandez", 104),
    ("Lopez", 87), ("Gonzalez", 84), ("Wilson", 80), ("Anderson", 78),
    ("Thomas", 76), ("Taylor", 75), ("Moore", 72), ("Jackson", 71),
    ("Martin", 70), ("Lee", 69), ("Perez", 68), ("Thompson", 66),
    ("White", 66), ("Harris", 62), ("Sanchez", 61), ("Clark", 56),
    ("Ramirez", 55), ("Lewis", 53), ("Robinson", 53), ("Walker", 52),
    ("Young", 48), ("Allen", 48), ("King", 46), ("Wright", 46),
    ("Scott", 44), ("Torres", 44), ("Nguyen", 44), ("Hill", 43),
    ("Adams", 42), ("Nelson", 42), ("Baker", 42), ("Hall", 41),
    ("Rivera", 39), ("Campbell", 39), ("Mitchell", 38), ("Carter", 37),
    ("Roberts", 37), ("Evans", 36), ("Turner", 35),
)
LOCATIONS = (
    (("Nashville", "TN", "37203"), 10),
    (("Knoxville", "TN", "37902"), 5),
    (("Charlotte", "NC", "28202"), 9),
    (("Raleigh", "NC", "27601"), 7),
    (("Richmond", "VA", "23220"), 9),
    (("Norfolk", "VA", "23510"), 5),
    (("Atlanta", "GA", "30309"), 9),
    (("Birmingham", "AL", "35203"), 5),
    (("Greenville", "SC", "29601"), 6),
    (("Jacksonville", "FL", "32202"), 6),
    (("Dallas", "TX", "75201"), 6),
    (("Louisville", "KY", "40202"), 4),
    (("Baltimore", "MD", "21201"), 6),
    (("Pittsburgh", "PA", "15222"), 5),
    (("Philadelphia", "PA", "19103"), 5),
    (("Columbus", "OH", "43215"), 3),
    (("Indianapolis", "IN", "46204"), 3),
    (("Phoenix", "AZ", "85004"), 2),
    (("Sacramento", "CA", "95814"), 2),
    (("Portland", "OR", "97205"), 1),
)
EMAIL_PROVIDERS = (
    ("gmail", 35), ("yahoo", 22), ("aol", 16), ("outlook", 13),
    ("icloud", 8), ("community.org", 4), ("alumni.edu", 2),
)


def _weighted(rng, entries):
    values, weights = zip(*entries)
    return rng.choices(values, weights=weights, k=1)[0]


def display_person(index, random_seed):
    """Return only names, fake email and location; never change identity coverage."""
    rng = random.Random(f"audit-display-v1:{random_seed}:{index}")
    first = _weighted(rng, FIRST_NAMES)
    last = _weighted(rng, LAST_NAMES)
    provider = _weighted(rng, EMAIL_PROVIDERS)
    # The entire domain is reserved, including .org/.edu-flavored provider
    # subdomains. The unique suffix survives case/dot/plus normalization.
    email = f"{first.lower()}.{last.lower()}{index:08d}@{provider}.example.org"
    city, region, postal_code = _weighted(rng, LOCATIONS)
    street = rng.choice(("Oak", "Maple", "Cedar", "Pine", "Willow", "Magnolia",
                         "Dogwood", "Elm", "Hickory", "Laurel"))
    suffix = rng.choice(("Lane", "Drive", "Road", "Court", "Way"))
    return {
        "first_name": first,
        "last_name": last,
        "email": email,
        "address1": f"{rng.randrange(100, 9900)} {street} {suffix}",
        "city": city,
        "region": region,
        "postal_code": postal_code,
        "country": "US",
    }
