import locust
import sh
import uuid
import json
import os
import re
import tempfile
import logging
import queue
from locust import events
from locust.runners import MasterRunner, WorkerRunner
import time
import base64
import random

# CosmPy imports
from cosmpy.aerial.client import LedgerClient, NetworkConfig
from cosmpy.aerial.wallet import LocalWallet
from cosmpy.aerial.tx import Transaction, SigningCfg
from cosmpy.aerial.client.bank import create_bank_send_msg
from cosmpy.crypto.address import Address
from cosmpy.mnemonic import generate_mnemonic

# Global variable to store temp directory path
TEMP_DIR = None

# Hardcoded faucet mnemonic
FAUCET_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art"

INITIAL_AMOUNT = 10000000  # in uatom
SEND_AMOUNT = 10  # in uatom
MAIN_HOST = "http://perf-val1.stg.earthball.xyz:1317"
HOSTS = [
    "http://perf-val1.stg.earthball.xyz:1317",
    "http://perf-val2.stg.earthball.xyz:1317",
    # "http://perf-val3.stg.earthball.xyz:1317", This host intentionally kept idle
    "http://perf-val4.stg.earthball.xyz:1317",
]
GAS_PRICES = 0.001  # in uatom
CHAIN_ID = "perf-testnet"

# Create a custom NetworkConfig
def create_custom_network():
    network = NetworkConfig(
        chain_id=CHAIN_ID,
        url="rest+"+MAIN_HOST,
        fee_minimum_gas_price=GAS_PRICES,
        fee_denomination="uatom",
        staking_denomination="uatom",
    )
    return network

# Initialize faucet wallet and network
faucet_wallet = LocalWallet.from_mnemonic(FAUCET_MNEMONIC, prefix="cosmos")
faucet_address = str(faucet_wallet.address())
faucet_network = create_custom_network()
faucet_ledger = LedgerClient(faucet_network)

@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """Called when locust is initializing"""
    # Register message handlers based on runner type
    if isinstance(environment.runner, WorkerRunner):
        environment.runner.register_message("wallets", on_worker_receive_wallets)

# Create a temporary directory when the test starts
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    global TEMP_DIR
    TEMP_DIR = tempfile.mkdtemp(prefix="locust_cosmos_")
    logging.info(f"Created temporary directory for test files: {TEMP_DIR}")

    if isinstance(environment.runner, MasterRunner):
        print("Master node initializing wallets...")

        # 1. Calculate total number of users across all workers
        total_users = environment.runner.target_user_count

        # 2. Generate all wallets - store mnemonic and address
        global_wallets = []

        for i in range(total_users):
            # Generate a mnemonic
            mnemonic = generate_mnemonic()
            
            # Create wallet from mnemonic to get the address
            wallet = LocalWallet.from_mnemonic(mnemonic, prefix="cosmos")
            
            # Store only the mnemonic and address
            global_wallets.append({
                'mnemonic': mnemonic,
                'address': str(wallet.address())
            })

        print(f"Created {len(global_wallets)} wallets")

        # 3. Fund all wallets sequentially from a single faucet
        fund_all_wallets(global_wallets)

        # 4. Distribute wallets to workers
        if environment.runner.worker_count > 0:
            # Calculate how many wallets per worker
            workers = list(environment.runner.clients.keys())
            workers_count = len(workers)

            print(f"Distributing wallets to {workers_count} workers")

            # Divide wallets among workers
            for i, worker_id in enumerate(workers):
                # Calculate slice for this worker
                start_idx = (i * total_users) // workers_count
                end_idx = ((i + 1) * total_users) // workers_count
                
                # Extract this worker's wallets
                worker_wallets = global_wallets[start_idx:end_idx]
                
                print(f"Sending {len(worker_wallets)} wallets to worker {worker_id}")
                # Send the wallets to this specific worker
                environment.runner.send_message("wallets", worker_wallets, worker_id)

def fund_all_wallets(wallets):
    """Fund all wallets using batched transactions"""
    print(f"Starting to fund {len(wallets)} wallets from faucet {faucet_address}")
    
    # Get faucet account info for sequence tracking
    faucet_account = faucet_ledger.query_account(faucet_address)
    account_number = faucet_account.number
    sequence = faucet_account.sequence
    
    # Process wallets in batches
    msgs_per_tx = 100  # Number of send messages per transaction
    
    for i in range(0, len(wallets), msgs_per_tx):
        batch = wallets[i:i + msgs_per_tx]
        batch_num = i//msgs_per_tx + 1
        total_batches = (len(wallets)-1)//msgs_per_tx + 1
        
        print(f"Creating batch tx {batch_num}/{total_batches} ({len(batch)} wallets in this batch)")
        
        # Create and send the batch transaction
        sequence = process_funding_batch(batch, sequence, account_number)
    
    print(f"Finished funding {len(wallets)} wallets")

def process_funding_batch(batch, sequence, account_number, max_retries=3):
    """Process a batch of wallets to fund them with retry logic for sequence errors"""
    import requests
    http_client = requests.Session()
    
    retry_count = 0
    while retry_count <= max_retries:
        # Create transaction with multiple messages
        tx = create_funding_transaction(batch)
        
        # Calculate gas and fee
        gas_limit = calculate_gas_for_batch(len(batch))
        fee = f"{int(gas_limit * faucet_network.fee_minimum_gas_price)}uatom"
        
        # Sign the transaction
        tx = sign_transaction(tx, sequence, account_number, gas_limit, fee)
        
        # Broadcast the transaction
        tx_hash, new_sequence, is_sequence_error = broadcast_funding_transaction(tx, http_client, sequence)
        
        # Check if we succeeded
        if tx_hash:
            print(f"Successfully funded batch of {len(batch)} wallets")
            return new_sequence
        
        # If we got a sequence error, retry with the new sequence
        if is_sequence_error:
            retry_count += 1
            sequence = new_sequence
            print(f"Retrying batch with updated sequence {sequence} (attempt {retry_count}/{max_retries})")
            continue
        
        # If we got any other error, return the new sequence without retry
        return new_sequence
    
    # If we've exhausted our retries, force query for the latest sequence
    print(f"Failed to fund batch after {max_retries} retries, querying for correct sequence")
    return get_corrected_sequence(sequence)

def create_funding_transaction(batch):
    """Create a transaction with multiple fund messages"""
    tx = Transaction()
    
    for wallet in batch:
        recipient_address = Address(wallet["address"], prefix="cosmos")
        msg = create_bank_send_msg(
            faucet_wallet.address(), 
            recipient_address,
            INITIAL_AMOUNT,
            "uatom"
        )
        tx.add_message(msg)
    
    return tx

def calculate_gas_for_batch(batch_size):
    """Calculate gas limit based on batch size"""
    base_gas = 200000
    per_msg_gas = 60000
    return base_gas + (per_msg_gas * batch_size)


def sign_transaction(tx, sequence, account_number, gas_limit, fee):
    """Sign a transaction with the faucet wallet"""
    tx.seal(
        SigningCfg.direct(faucet_wallet.public_key(), sequence),
        fee=fee,
        gas_limit=gas_limit
    )
    tx.sign(faucet_wallet.signer(), faucet_network.chain_id, account_number)
    tx.complete()
    return tx


def broadcast_funding_transaction(tx, http_client, sequence):
    """Broadcast a funding transaction and handle the response
    
    Returns:
        tuple: (tx_hash, new_sequence, is_sequence_error)
            - tx_hash: Transaction hash if successful, None otherwise
            - new_sequence: Updated sequence number
            - is_sequence_error: True if error was a sequence mismatch
    """
    try:
        # Convert to base64 encoded protobuf
        tx_bytes = base64.b64encode(tx.tx.SerializeToString()).decode('utf-8')
        
        # Send transaction
        response = http_client.post(
            f"{MAIN_HOST}/cosmos/tx/v1beta1/txs",
            json={
                "tx_bytes": tx_bytes,
                "mode": "BROADCAST_MODE_SYNC"
            },
            timeout=15.0
        )
        
        if response.status_code != 200:
            logging.error(f"Request failed with status code {response.status_code}: {response.text}")
            return None, get_corrected_sequence(sequence), False
        
        response_data = response.json()
        tx_response = response_data.get("tx_response", {})
        code = tx_response.get("code", -1)
        
        if code != 0:
            # Handle errors
            error_msg = tx_response.get("raw_log", "") or "(no raw_log)"
            logging.error(f"Failed to fund batch: {error_msg}")
            
            # Check if this is a sequence error
            if "account sequence mismatch" in error_msg:
                new_sequence = handle_sequence_error(error_msg, sequence)
                return None, new_sequence, True
            
            return None, get_corrected_sequence(sequence), False
        
        # Transaction submitted successfully
        tx_hash = tx_response.get("txhash")
        if not tx_hash:
            logging.error("Transaction submitted but no txhash in response")
            return None, sequence + 1, False
        
        # Wait for confirmation
        if wait_for_transaction_confirmation(tx_hash, http_client):
            return tx_hash, sequence + 1, False
        else:
            logging.error(f"Transaction {tx_hash} not confirmed after waiting. Response: {response_data}")
            return None, get_corrected_sequence(sequence), False
            
    except Exception as e:
        logging.error(f"Error broadcasting transaction: {str(e)}")
        return None, get_corrected_sequence(sequence), False


def wait_for_transaction_confirmation(tx_hash, http_client):
    """Wait for a transaction to be confirmed"""
    print(f"Transaction submitted with hash {tx_hash}, waiting for confirmation...")
    time.sleep(6)  # Typical block time
    
    max_retries = 5
    for retry in range(max_retries):
        try:
            response = http_client.get(
                f"{MAIN_HOST}/cosmos/tx/v1beta1/txs/{tx_hash}",
                timeout=10.0
            )
            
            if response.status_code == 200:
                tx_info = response.json()
                tx_status = tx_info.get("tx_response", {})
                
                if tx_status.get("code", -1) == 0:
                    print(f"Transaction {tx_hash} confirmed in block {tx_status.get('height')}")
                    return True
                else:
                    error_msg = tx_status.get("raw_log", "")
                    logging.error(f"Transaction {tx_hash} failed after inclusion: {error_msg}")
                    return False
                    
            elif response.status_code == 404:
                print(f"Transaction {tx_hash} not yet confirmed, waiting... (attempt {retry+1}/{max_retries})")
                time.sleep(3)
                continue
            else:
                logging.error(f"Failed to query tx {tx_hash}: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logging.error(f"Error querying transaction {tx_hash}: {str(e)}")
            time.sleep(2)
    
    logging.error(f"Failed to confirm transaction {tx_hash} after {max_retries} attempts")
    return False


def handle_sequence_error(error_msg, sequence):
    """Extract the correct sequence from a sequence mismatch error"""
    seq_match = re.search(r'expected (\d+), got (\d+)', error_msg)
    if seq_match:
        expected_seq = int(seq_match.group(1))
        logging.info(f"Sequence mismatch detected: expected {expected_seq}, got {sequence}")
        return expected_seq
 
    # If we can't parse the error, get the current sequence from the account
    logging.warning(f"Could not parse sequence from error, querying account")
    return get_corrected_sequence(sequence)


def get_corrected_sequence(sequence):
    """Query account for correct sequence"""
    time.sleep(2)
    faucet_account = faucet_ledger.query_account(faucet_address)
    return faucet_account.sequence

user_credentials_queue = queue.Queue()

def on_worker_receive_wallets(environment, msg):
    """Handles messages received from master"""
    global user_credentials_queue

    print(f"Worker received {len(msg.data)} wallets from master")
    for wallet in msg.data:
        user_credentials_queue.put(wallet)

# Clean up the temporary directory when the test ends
@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    global TEMP_DIR
    if TEMP_DIR and os.path.exists(TEMP_DIR):
        for file in os.listdir(TEMP_DIR):
            try:
                os.remove(os.path.join(TEMP_DIR, file))
            except:
                pass
        try:
            os.rmdir(TEMP_DIR)
            logging.info(f"Removed temporary directory: {TEMP_DIR}")
        except:
            logging.info(f"Failed to remove temporary directory: {TEMP_DIR}")

def get_account_info(client, address):
    """Query account info using cosmpy"""
    try:
        account = client.query_account(address)
        return account.number, account.sequence
    except Exception as e:
        logging.error(f"Error fetching account info: {str(e)}")
        return 0, 0

class BankSendUser(locust.HttpUser):
    host = MAIN_HOST
    wait_time = locust.constant_pacing(5) # One tx every how many seconds

    def on_start(self):
        # Get wallet info from queue
        wallet_info = user_credentials_queue.get()
 
        # Initialize network
        self.network = create_custom_network()
 
        # Create ledger client - only used for queries, not for sending txs
        self.ledger = LedgerClient(self.network)
 
        # Create wallet from mnemonic
        self.wallet = LocalWallet.from_mnemonic(wallet_info["mnemonic"], prefix="cosmos")
 
        # Store address
        self.address = self.wallet.address()
        self.to_address = Address(faucet_address, prefix="cosmos")
 
        # Get initial account info
        try:
            account = self.ledger.query_account(self.address)
        except Exception as e:
            logging.critical(f"Failed to get account info: {str(e)}")
            self.environment.runner.quit()
        self.account_number = account.number
        self.sequence = account.sequence
        
        # Store amount to send
        self.amount = SEND_AMOUNT  # uatom
        self.denom = "uatom"
        
        self.msg = create_bank_send_msg(
                self.address,
                self.to_address,
                self.amount,
                self.denom
        )

    def _parse_sequence_from_error(self, error_message):
        """Extract the expected sequence number from an error message."""
        if 'account sequence mismatch' in error_message:
            seq_match = re.search(r'expected (\d+), got (\d+)', error_message)
            if seq_match:
                expected_seq = int(seq_match.group(1))
                return expected_seq
        return None

    @locust.task
    def send_money(self):
        # Create transaction
        tx = Transaction()
        
        # Add bank send message
        tx.add_message(self.msg)
        
        # Calculate gas and fee offline
        gas_limit = 4000000  # Use a safe default
        fee = f"{int(gas_limit * self.network.fee_minimum_gas_price)}{self.denom}"
        
        # Seal and sign transaction offline
        tx.seal(
            SigningCfg.direct(self.wallet.public_key(), self.sequence),
            fee=fee,
            gas_limit=gas_limit
        )
        tx.sign(self.wallet.signer(), self.network.chain_id, self.account_number)
        tx.complete()
        
        # Convert to base64 encoded protobuf
        tx_bytes = base64.b64encode(tx.tx.SerializeToString()).decode('utf-8')
 
        # Select random host
        host = random.choice(HOSTS)

        # Use Locust client to broadcast
        with self.client.post(
            host+"/cosmos/tx/v1beta1/txs",
            json={
                "tx_bytes": tx_bytes,
                "mode": "BROADCAST_MODE_SYNC"
            },
            name="/cosmos/tx/v1beta1/txs",
            timeout=10.0,
            catch_response=True
        ) as response:
            if response.status_code == 200:
                response_data = response.json()
                tx_response = response_data.get("tx_response", {})

                code = tx_response.get("code", -1)
                if code == 0:
                    # Success! Increment sequence
                    self.sequence += 1
                    response.success()
                else:
                    # Handle sequence mismatch errors
                    error_msg = tx_response.get("raw_log", "") or "(no raw_log)"
                    if "account sequence mismatch" in error_msg:
                        expected_seq = self._parse_sequence_from_error(error_msg)
                        if expected_seq is not None:
                            self.sequence = expected_seq
                        else:
                            # Fallback: query account for correct sequence
                            _, self.sequence = get_account_info(self.ledger, self.address)
                        
                        response.failure(f"Sequence mismatch")
                    elif "error checking fee" in error_msg:
                        # Handle fee error
                        response.failure(f"Out of gas")
                    else:
                        response.failure(f"Transaction failed with code {code}: {error_msg}")
            else:
                text = response.text or "(no response)"
                response.failure(f"Request failed with status code {response.status_code}: {text}")
