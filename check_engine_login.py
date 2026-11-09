from getpass import getpass
from sqlalchemy import select
from werkzeug.security import check_password_hash

from app.db import SessionLocal
from app.models import CustomerAccount, AccountEmailVerification, ExternalIdentity

email = input("E-mail: ").strip().lower()
password = getpass("Heslo AI Engine: ")

with SessionLocal() as db:
    a = db.scalar(
        select(CustomerAccount).where(
            CustomerAccount.email == email,
            CustomerAccount.active.is_(True)
        )
    )

    if not a:
        print("ACCOUNT: NOT FOUND")
        raise SystemExit

    verification = db.get(AccountEmailVerification, a.id)
    identity = db.get(ExternalIdentity, a.id)

    h = a.password_hash or ""

    print("ACCOUNT: FOUND")
    print("ACTIVE:", a.active)
    print("VERIFIED:", bool(verification and verification.verified_at))
    print("FRESH LINKED:", bool(identity))
    print("FRESH PROVIDER:", getattr(identity, "provider", None))

    if h.startswith("external:"):
        print("HASH TYPE: external")
        print("PASSWORD OK: N/A")
    else:
        print("HASH TYPE:", h.split("$", 1)[0][:40])
        print("PASSWORD OK:", check_password_hash(h, password))
