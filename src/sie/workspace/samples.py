"""Small made-up example files for the start screen, built in memory from fixed seeds.

They are invented (not real records of anything) and say so on the page; they exist so a visitor can try
the whole journey before choosing a file of their own. Nothing is read from disk or from the network, and
the same name always gives the same bytes."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    id: str
    title: str
    blurb: str
    filename: str


SAMPLES = (
    Sample("sales", "Shop sales", "Orders over a year: revenue by region and product, a monthly trend.", "example-sales.csv"),
    Sample("customers", "Customer churn", "Customers with a yes/no outcome to predict; has blanks and repeats to clean.", "example-customers.csv"),
    Sample("students", "Exam results", "Scores by class and school: averages, groups and what goes with a high score.", "example-students.csv"),
)  # fmt: skip


def _sales() -> str:
    r = random.Random(11)
    rows = ["order_id,order_date,region,product,units,unit_price,revenue"]
    for i in range(1, 361):
        month, day = 1 + (i * 7) % 12, 1 + (i * 5) % 28
        region = r.choice(["North", "South", "East", "West"])
        product = r.choice(["Tea", "Coffee", "Cocoa", "Biscuits", "Honey"])
        units = r.randint(1, 20)
        price = {"Tea": 4.5, "Coffee": 7.0, "Cocoa": 6.0, "Biscuits": 3.0, "Honey": 9.5}[product]
        revenue = "" if i % 37 == 0 else f"{units * price * (1 + (month - 6) / 40):.2f}"
        rows.append(f"{i},2025-{month:02d}-{day:02d},{region},{product},{units},{price},{revenue}")
    return "\n".join(rows) + "\n"


def _customers() -> str:
    r = random.Random(7)
    rows = ["customer_id,region,plan,monthly_fee,signed_up,age,churned"]
    for i in range(1, 281):
        plan = r.choice(["basic", "pro", "team"])
        fee = {"basic": 9, "pro": 19, "team": 49}[plan] + (i % 3)
        churned = "yes" if (plan == "basic" and r.random() < 0.55) or r.random() < 0.08 else "no"
        age = "" if i % 13 == 0 else str(r.randint(18, 72))
        rows.append(
            f"{i},{r.choice(['North', 'South', 'East', 'West'])},{plan},{fee},"
            f"{1 + i % 28:02d}/{1 + i % 12:02d}/2024,{age},{churned}"
        )
    rows += [rows[4], rows[9]]  # two repeated rows to find
    return "\n".join(rows) + "\n"


def _students() -> str:
    r = random.Random(5)
    rows = ["student_id,school,class,study_hours,absences,maths,reading"]
    for i in range(1, 241):
        hours = round(r.uniform(0, 12), 1)
        base = 45 + hours * 3.2 - r.randint(0, 6) * 1.5
        maths = max(0, min(100, round(base + r.gauss(0, 7))))
        reading = max(0, min(100, round(base + 4 + r.gauss(0, 8))))
        rows.append(
            f"{i},{r.choice(['Hill', 'River', 'Park'])},{r.choice(['A', 'B', 'C'])},{hours},"
            f"{r.randint(0, 9)},{maths},{reading}"
        )
    return "\n".join(rows) + "\n"


_BUILD = {"sales": _sales, "customers": _customers, "students": _students}


def sample_bytes(sample_id: object) -> tuple[Sample, bytes] | None:
    """The sample with this id and its CSV bytes, or None when the id is not one of the fixed ones."""
    for s in SAMPLES:
        if s.id == sample_id:
            return s, _BUILD[s.id]().encode("utf-8")
    return None
