import os
from getpass import getpass
from dotenv import load_dotenv

load_dotenv("/home2/lauhannq/aiengine.freshcrackers.cz/.env")

from app import fresh_account

print("CONFIGURED:", fresh_account.configured())
print("URL:", fresh_account.base_url())
print("APP ID:", fresh_account.app_id())
print("SECRET SET:", bool(fresh_account.secret()))

email = input("E-mail: ").strip().lower()
password = getpass("Heslo Fresh Crackers: ")

try:
    remote = fresh_account.password_login(email, password)
    print("LOGIN: OK")
    print("REMOTE EMAIL:", remote.get("email"))
    print("VERIFIED:", remote.get("verified"))
    print("USER ID:", remote.get("user_id") or remote.get("id"))
except fresh_account.FreshAccountError as e:
    print("LOGIN: ERROR")
    print("HTTP STATUS:", e.status)
    print("CODE:", e.code)
    print("MESSAGE:", str(e))
