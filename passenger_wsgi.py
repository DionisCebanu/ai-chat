# passenger_wsgi.py
import os, sys
BASE_DIR = os.path.dirname(__file__)
sys.path.insert(0, BASE_DIR)

# Import your Flask app object from app.py
from app import app as application
