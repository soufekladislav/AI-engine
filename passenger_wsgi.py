"""cPanel Passenger: independent Fresh AI Engine entry point."""
import os
import sys
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
from flask_app import app as application
