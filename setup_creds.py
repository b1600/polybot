# setup_creds.py — run once to generate API credentials
from py_clob_client.client import ClobClient
from dotenv import load_dotenv
import os

load_dotenv()

client = ClobClient(
    host=os.getenv("CLOB_HOST"),
    key=os.getenv("POLY_PRIVATE_KEY"),
    chain_id=137,
    signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
    funder=os.getenv("POLY_FUNDER_ADDRESS"),
)

creds = client.create_or_derive_api_creds()
print("Add these to your .env file:")
print(f"POLY_API_KEY={creds.api_key}")
print(f"POLY_API_SECRET={creds.api_secret}")
print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
