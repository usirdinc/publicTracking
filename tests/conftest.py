import os, sys, tempfile
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "t.sqlite3")
os.environ["DATA_KEY"] = Fernet.generate_key().decode()
os.environ["SECRET_KEY"] = "test-secret-key-test-secret-key-123"
os.environ["SQUARE_WEBHOOK_SIGNATURE_KEY"] = "sigkey"
os.environ["BASE_URL"] = "http://testserver"
os.environ["ADMIN_EMAILS"] = "admin@example.com"
os.environ["BETA_CAP"] = "200"
