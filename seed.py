#!/usr/bin/env python3
"""Create a demo staff user and a few patients. Safe to run repeatedly."""
import database as db

db.init_db()

DEMO_PHONE = "0900000001"
if not db.get_user_by_login(DEMO_PHONE):
    db.create_staff(
        phone_number=DEMO_PHONE,
        email="doctor@demo.local",
        password="password",
        full_name="Dr. Demo",
        specialization="Sản khoa",
        degree="BS.CKI",
        role=1,
    )
    print(f"created staff user {DEMO_PHONE} / password")
else:
    print("staff user already exists")

# (id, full_name, gender, date_of_birth, mrn, citizen_id, address, notes)
DEMO = [
    ("P001", "Jane Doe", "F", "1994-05-02", "MRN-0001", "079094000001",
     "12 Lê Lợi, Q1, TP.HCM", "32 weeks gestation"),
    ("P002", "Maria Santos", "F", "1991-11-18", "MRN-0002", "079091000002",
     "45 Trần Hưng Đạo, Q5, TP.HCM", "28 weeks • twin pregnancy"),
    ("P003", "Aisha Khan", "F", "1996-02-09", "MRN-0003", "079096000003",
     "8 Nguyễn Huệ, Q1, TP.HCM", "36 weeks • routine monitoring"),
]
for pid, full_name, gender, dob, mrn, citizen_id, address, notes in DEMO:
    db.upsert_patient(pid, full_name=full_name, gender=gender,
                      date_of_birth=dob, mrn=mrn, citizen_id=citizen_id,
                      address=address, notes=notes)
print(f"seeded {len(DEMO)} patients: " + ", ".join(p[0] for p in DEMO))
print(f"\nLogin at http://localhost:8000/login  ({DEMO_PHONE} / password)")
