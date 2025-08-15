import os
import time
import uuid
import sqlite3
import re
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from dotenv import load_dotenv
from dateutil.relativedelta import relativedelta
from dateutil import parser as date_parser
from datetime import datetime, timedelta
# Load .env file
load_dotenv()
# -----------------------------
# Config (safe for GitHub)