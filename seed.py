#!/usr/bin/env python3
"""Create a demo doctor and a few patients. Safe to run repeatedly."""
import database as db

db.init_db()

if not db.get_doctor_by_username("doctor"):
    db.create_doctor("doctor", "password", name="Dr. Demo")
    print("created doctor / password")
else:
    print("doctor already exists")

DEMO = [
    ("P001", "Jane Doe", "MRN-0001", "F", "1994-05-02", "32 weeks gestation"),
    ("P002", "Maria Santos", "MRN-0002", "F", "1991-11-18", "28 weeks • twin pregnancy"),
    ("P003", "Aisha Khan", "MRN-0003", "F", "1996-02-09", "36 weeks • routine monitoring"),
]
for pid, name, mrn, sex, dob, notes in DEMO:
    db.upsert_patient(pid, name=name, mrn=mrn, sex=sex, dob=dob, notes=notes)
print(f"seeded {len(DEMO)} patients: " + ", ".join(p[0] for p in DEMO))
print("\nLogin at http://localhost:8000/login  (doctor / password)")
