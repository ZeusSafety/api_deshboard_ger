import functions_framework
import pymysql
import json
import re
import requests
import os
import io
import logging
from datetime import datetime
from google.cloud import storage
import requests
from datetime import datetime, timedelta, timezone

# Definir la zona horaria de Perú (UTC-5)
TZ_PERU = timezone(timedelta(hours=-5))

def get_now_peru():
    return datetime.now(TZ_PERU).strftime('%Y-%m-%d %H:%M:%S')

# Conexión a MySQL
def get_connection():
    try:
        conn = pymysql.connect(
            user="zeussafety-2024",
            password="ZeusSafety2025",
            db="Zeus_Safety_Data_Integration",
            unix_socket="/cloudsql/stable-smithy-435414-m6:us-central1:zeussafety-2024",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10 # Añadimos un timeout
        )
        return conn
    except Exception as e:
        logging.error(f"Error crítico al conectar a la base de datos: {e}")
        return None
