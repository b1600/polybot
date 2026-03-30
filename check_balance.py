import os
from dotenv import load_dotenv
from executor import init_client, get_usdc_balance
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

def main():
    # Load environment variables
    load_dotenv()
    
    print("--- Polymarket Balance Checker ---")
    
    try:
        # Initialize client
        client = init_client()
        
        # Verify the address derived from the private key
        address = client.get_address()
        print(f"Checking account: {address}")
        
        # Get CLOB Balance (available for trading)
        # This will raise an exception if credentials are 401 Unauthorized
        balance = get_usdc_balance(client)
        
        # Print results
        print(f"CLOB Trading Balance: ${balance:.2f} USDC")
        
        # Additional details
        funder = os.getenv("POLY_FUNDER_ADDRESS")
        if funder:
            print(f"Proxy Address (Funder): {funder}")
            
    except Exception as e:
        if "401" in str(e):
            print("\n[!] Authentication Error (401 Unauthorized)")
            print("Your POLY_API_KEY, SECRET, or PASSPHRASE in .env do not match your wallet/proxy configuration.")
            print("Try running 'python3 setup_creds.py' to generate valid credentials for your account.")
        else:
            print(f"\nError checking balance: {e}")

if __name__ == "__main__":
    main()
