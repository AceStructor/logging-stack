import os
from dotenv import load_dotenv

load_dotenv()

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER")
MATRIX_USER = os.getenv("MATRIX_USER")
MATRIX_PASSWORD = os.getenv("MATRIX_PASSWORD")
MATRIX_ROOM_ID = os.getenv("MATRIX_ROOM_ID")

TOKEN_FILE = os.getenv("MATRIX_TOKEN_FILE", "/app/matrix_session/matrix_session.json")

LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
