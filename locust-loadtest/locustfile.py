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
import bech32
import threading
from collections import deque
from gevent import spawn, sleep as gsleep
from google.protobuf.internal.encoder import _VarintBytes
from google.protobuf.internal.wire_format import WIRETYPE_LENGTH_DELIMITED

# CosmPy imports
from cosmpy.aerial.client import LedgerClient, NetworkConfig
from cosmpy.aerial.wallet import LocalWallet
from cosmpy.aerial.tx import Transaction, SigningCfg, TxFee
from cosmpy.aerial.client.bank import create_bank_send_msg
from cosmpy.crypto.address import Address
from cosmpy.mnemonic import generate_mnemonic

# Global variable to store temp directory path
TEMP_DIR = None

# Global gas tracking
total_gas_used = 0
total_gas_lock = threading.Lock()
confirmed_tx_count = 0
failed_tx_count = 0

# Hardcoded faucet mnemonic
FAUCET_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art"

# Chain configuration constants
DENOM = "uatom"
SEND_AMOUNT = 10

# TokenFactory configuration
MSGS_PER_TX = 500        # Number of mint-to messages per transaction
MINT_AMOUNT = 1000      # Amount to mint per recipient
SUBDENOM_PREFIX = "air" # Subdenom prefix (short to save gas)
DENOM_CREATION_FEE = 100000000  # 100 ATOM in uatom (adjust per chain)

# Funding amount: creation fee + gas for many txs
# For BankSendUser: 10 ATOM is enough
# For TokenFactoryMintUser: need creation fee + gas
INITIAL_AMOUNT = DENOM_CREATION_FEE + 50000000  # ~150 ATOM total
HOSTS = [
    "https://rest.gaia-devnet.polypore.xyz",
]
MAIN_HOST = HOSTS[0]
GAS_PRICES = 0.005
CHAIN_ID = "gaia-devnet"
# Create a custom NetworkConfig
def create_custom_network():
    network = NetworkConfig(
        chain_id=CHAIN_ID,
        url="rest+"+MAIN_HOST,
        fee_minimum_gas_price=GAS_PRICES,
        fee_denomination=DENOM,
        staking_denomination=DENOM,
    )
    return network

# ============================================
# TokenFactory Protobuf Message Construction
# ============================================

def encode_string(field_num, value):
    """Encode a string field in protobuf wire format"""
    tag = (field_num << 3) | WIRETYPE_LENGTH_DELIMITED
    encoded = value.encode('utf-8')
    return _VarintBytes(tag) + _VarintBytes(len(encoded)) + encoded

def create_coin_bytes(denom, amount):
    """Create Coin protobuf bytes: denom=field1, amount=field2"""
    return encode_string(1, denom) + encode_string(2, str(amount))

def generate_random_address():
    """Generate a valid random cosmos bech32 address"""
    random_bytes = os.urandom(20)
    converted = bech32.convertbits(random_bytes, 8, 5)
    return bech32.bech32_encode("cosmos", converted)


def query_tx_gas_used(txhash, http_client):
    """Query a tx and return gas_used, or None if not found/failed"""
    try:
        response = http_client.get(
            f"{MAIN_HOST}/cosmos/tx/v1beta1/txs/{txhash}",
            timeout=10.0
        )
        if response.status_code == 200:
            data = response.json()
            tx_response = data.get("tx_response", {})
            if tx_response.get("code", -1) == 0:
                return int(tx_response.get("gas_used", 0))
        return None
    except Exception:
        return None


class RawProtoMessage:
    """A wrapper that makes raw protobuf bytes look like a message for cosmpy's Pack()"""
    def __init__(self, type_url, serialized_bytes):
        # Create a fake DESCRIPTOR with the full_name cosmpy expects
        self.DESCRIPTOR = type('Descriptor', (), {'full_name': type_url.lstrip('/')})()
        self._serialized = serialized_bytes

    def SerializeToString(self, deterministic=False):
        return self._serialized


def create_mint_msg(sender, denom, amount, mint_to_address):
    """Create MsgMint for Transaction.add_message()

    MsgMint fields:
      - sender (string, field 1)
      - amount (Coin, field 2): {denom: field1, amount: field2}
      - mintToAddress (string, field 3)
    """
    coin_bytes = create_coin_bytes(denom, amount)
    # Build embedded Coin message with proper length prefix
    coin_tag = (2 << 3) | WIRETYPE_LENGTH_DELIMITED  # field 2, length-delimited
    msg_bytes = (
        encode_string(1, sender) +
        _VarintBytes(coin_tag) + _VarintBytes(len(coin_bytes)) + coin_bytes +
        encode_string(3, mint_to_address)
    )
    return RawProtoMessage("osmosis.tokenfactory.v1beta1.MsgMint", msg_bytes)


def create_denom_msg(sender, subdenom):
    """Create MsgCreateDenom for Transaction.add_message()

    MsgCreateDenom fields:
      - sender (string, field 1)
      - subdenom (string, field 2)
    """
    msg_bytes = encode_string(1, sender) + encode_string(2, subdenom)
    return RawProtoMessage("osmosis.tokenfactory.v1beta1.MsgCreateDenom", msg_bytes)

# ============================================

# Initialize faucet wallet and network
faucet_wallet = LocalWallet.from_mnemonic(FAUCET_MNEMONIC, prefix="cosmos")
faucet_address = str(faucet_wallet.address())
faucet_network = create_custom_network()
faucet_ledger = LedgerClient(faucet_network)

@events.init_command_line_parser.add_listener
def on_locust_init_parser(parser):
    """Add custom arguments that appear in the web UI"""
    parser.add_argument(
        "--msgs-per-tx",
        type=int,
        default=MSGS_PER_TX,
        help="Number of mint-to messages per transaction"
    )
    parser.add_argument(
        "--mint-amount",
        type=int,
        default=MINT_AMOUNT,
        help="Amount to mint per recipient"
    )
    parser.add_argument(
        "--tx-interval",
        type=float,
        default=5.0,
        help="Seconds between transactions per user"
    )

TX_INTERVAL = 5.0  # Default, overridden by --tx-interval

@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """Called when locust is initializing"""
    # Register message handlers based on runner type
    if isinstance(environment.runner, WorkerRunner):
        environment.runner.register_message("wallets", on_worker_receive_wallets)

# Create a temporary directory when the test starts
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    global TEMP_DIR, MSGS_PER_TX, MINT_AMOUNT, TX_INTERVAL

    # Apply custom arguments from web UI / command line
    if environment.parsed_options:
        MSGS_PER_TX = environment.parsed_options.msgs_per_tx
        MINT_AMOUNT = environment.parsed_options.mint_amount
        TX_INTERVAL = environment.parsed_options.tx_interval
        print(f"Config: msgs_per_tx={MSGS_PER_TX}, mint_amount={MINT_AMOUNT}, tx_interval={TX_INTERVAL}s")

    TEMP_DIR = tempfile.mkdtemp(prefix="locust_cosmos_")
    logging.info(f"Created temporary directory for test files: {TEMP_DIR}")

    # Skip wallet setup for workers - they receive wallets from master
    if isinstance(environment.runner, WorkerRunner):
        return

    # For standalone mode or master mode, generate and fund wallets
    is_master = isinstance(environment.runner, MasterRunner)
    mode_name = "Master node" if is_master else "Standalone mode"
    print(f"{mode_name} initializing wallets...")

    # 1. Calculate total number of users
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

    # 4. Distribute wallets
    if is_master and environment.runner.worker_count > 0:
        # Distributed mode: send wallets to workers
        workers = list(environment.runner.clients.keys())
        workers_count = len(workers)

        print(f"Distributing wallets to {workers_count} workers")

        for i, worker_id in enumerate(workers):
            start_idx = (i * total_users) // workers_count
            end_idx = ((i + 1) * total_users) // workers_count

            worker_wallets = global_wallets[start_idx:end_idx]

            print(f"Sending {len(worker_wallets)} wallets to worker {worker_id}")
            environment.runner.send_message("wallets", worker_wallets, worker_id)
    else:
        # Standalone mode: put wallets directly into the queue
        print(f"Adding {len(global_wallets)} wallets to local queue")
        for wallet in global_wallets:
            user_credentials_queue.put(wallet)

def fund_all_wallets(wallets):
    """Fund all wallets using batched transactions"""
    print(f"Starting to fund {len(wallets)} wallets from faucet {faucet_address}")
    
    # Get faucet account info for sequence tracking
    faucet_account = faucet_ledger.query_account(faucet_address)
    account_number = faucet_account.number
    sequence = faucet_account.sequence
    
    # Process wallets in batches
    msgs_per_tx = 200  # Number of send messages per transaction
    
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
        fee = f"{int(gas_limit * faucet_network.fee_minimum_gas_price)}{DENOM}"        
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
            DENOM,
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
        fee=TxFee(amount=fee, gas_limit=gas_limit)
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

    # Report gas stats
    print(f"\n{'='*50}")
    print(f"GAS USAGE STATS")
    print(f"{'='*50}")
    print(f"Total gas used: {total_gas_used:,}")
    print(f"Confirmed txs: {confirmed_tx_count}")
    print(f"Failed/missing txs: {failed_tx_count}")
    if confirmed_tx_count > 0:
        print(f"Avg gas per tx: {total_gas_used // confirmed_tx_count:,}")
    print(f"{'='*50}\n")

def get_account_info(client, address):
    """Query account info using cosmpy"""
    try:
        account = client.query_account(address)
        return account.number, account.sequence
    except Exception as e:
        logging.error(f"Error fetching account info: {str(e)}")
        return 0, 0

# Disabled: renamed with underscore prefix to use TokenFactoryMintUser instead
class _BankSendUser(locust.HttpUser):
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
        self.amount = SEND_AMOUNT
        self.denom = DENOM

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
        fee = f"{int(gas_limit * self.network.fee_minimum_gas_price)}{DENOM}"
        # Seal and sign transaction offline
        tx.seal(
            SigningCfg.direct(self.wallet.public_key(), self.sequence),
            fee=TxFee(amount=fee, gas_limit=gas_limit)
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


class TokenFactoryMintUser(locust.HttpUser):
    """Load test tokenfactory mint-to transactions (airdrop simulation)"""
    host = MAIN_HOST

    def wait_time(self):
        """Use configurable TX_INTERVAL for constant pacing"""
        if not hasattr(self, '_last_task_time'):
            self._last_task_time = time.time()
            return 0
        elapsed = time.time() - self._last_task_time
        self._last_task_time = time.time()
        return max(0, TX_INTERVAL - elapsed)

    def on_start(self):
        # Get admin wallet info from queue
        wallet_info = user_credentials_queue.get()

        # Initialize network
        self.network = create_custom_network()

        # Create ledger client for queries
        self.ledger = LedgerClient(self.network)

        # Create wallet from mnemonic
        self.wallet = LocalWallet.from_mnemonic(wallet_info["mnemonic"], prefix="cosmos")
        self.address = str(self.wallet.address())

        # Get initial account info
        try:
            account = self.ledger.query_account(self.address)
        except Exception as e:
            logging.critical(f"Failed to get account info for {self.address}: {str(e)}")
            self.environment.runner.quit()
            return
        self.account_number = account.number
        self.sequence = account.sequence

        # Create unique denom for this user
        self.subdenom = f"{SUBDENOM_PREFIX}{uuid.uuid4().hex[:8]}"
        self._create_denom()
        self.denom = f"factory/{self.address}/{self.subdenom}"
        logging.info(f"User initialized with denom: {self.denom}")

        # Initialize gas collection
        self.pending_txs = deque()
        self._stop_collector = False
        self._gas_collector = spawn(self._collect_gas_stats)

    def _create_denom(self):
        """One-time denom creation during setup"""
        print(f"Creating denom: {self.subdenom} for {self.address}")

        tx = Transaction()
        tx.add_message(create_denom_msg(self.address, self.subdenom))

        # Denom creation needs much higher gas (tokenfactory consumes ~2M gas for denom creation)
        gas_limit = 2500000
        fee = f"{int(gas_limit * self.network.fee_minimum_gas_price)}{DENOM}"

        tx.seal(
            SigningCfg.direct(self.wallet.public_key(), self.sequence),
            fee=TxFee(amount=fee, gas_limit=gas_limit)
        )
        tx.sign(self.wallet.signer(), self.network.chain_id, self.account_number)
        tx.complete()

        tx_bytes = base64.b64encode(tx.tx.SerializeToString()).decode('utf-8')

        import requests
        http_client = requests.Session()

        response = http_client.post(
            f"{MAIN_HOST}/cosmos/tx/v1beta1/txs",
            json={
                "tx_bytes": tx_bytes,
                "mode": "BROADCAST_MODE_SYNC"
            },
            timeout=15.0
        )

        if response.status_code != 200:
            print(f"ERROR: Failed to create denom: {response.status_code} - {response.text}")
            return

        response_data = response.json()
        tx_response = response_data.get("tx_response", {})
        code = tx_response.get("code", -1)

        if code != 0:
            error_msg = tx_response.get("raw_log", "") or "(no raw_log)"
            print(f"ERROR: Failed to create denom: {error_msg}")
            return

        tx_hash = tx_response.get("txhash")
        print(f"Denom creation tx submitted: {tx_hash}")

        # Wait for tx to be included in a block
        time.sleep(10)

        # Verify tx succeeded on-chain
        verify_response = http_client.get(
            f"{MAIN_HOST}/cosmos/tx/v1beta1/txs/{tx_hash}",
            timeout=10.0
        )
        if verify_response.status_code == 200:
            verify_data = verify_response.json()
            on_chain_code = verify_data.get("tx_response", {}).get("code", -1)
            if on_chain_code != 0:
                raw_log = verify_data.get("tx_response", {}).get("raw_log", "")
                raise RuntimeError(f"Denom creation tx failed on-chain: {raw_log}")
            print(f"Denom creation confirmed on-chain: {tx_hash}")
        else:
            raise RuntimeError(f"Could not verify denom creation tx: {verify_response.status_code}")

        self.sequence += 1

    def _parse_sequence_from_error(self, error_message):
        """Extract the expected sequence number from an error message."""
        if 'account sequence mismatch' in error_message:
            seq_match = re.search(r'expected (\d+), got (\d+)', error_message)
            if seq_match:
                return int(seq_match.group(1))
        return None

    def _collect_gas_stats(self):
        """Background greenlet to query tx results and collect gas stats"""
        import requests
        http_client = requests.Session()

        while not self._stop_collector:
            if self.pending_txs:
                txhash = self.pending_txs.popleft()
                gsleep(6)  # Wait for block inclusion

                gas = query_tx_gas_used(txhash, http_client)

                global total_gas_used, confirmed_tx_count, failed_tx_count
                with total_gas_lock:
                    if gas is not None:
                        total_gas_used += gas
                        confirmed_tx_count += 1
                    else:
                        failed_tx_count += 1
            else:
                gsleep(1)  # Idle when no pending txs

    def on_stop(self):
        """Stop the gas collector greenlet"""
        self._stop_collector = True
        if hasattr(self, '_gas_collector'):
            self._gas_collector.kill()

    @locust.task
    def mint_batch(self):
        """Send batched mint-to transaction to random addresses"""
        tx = Transaction()

        # Add MSGS_PER_TX mint messages to random addresses
        for _ in range(MSGS_PER_TX):
            recipient = generate_random_address()
            msg = create_mint_msg(self.address, self.denom, MINT_AMOUNT, recipient)
            tx.add_message(msg)

        # Calculate gas (base + per-msg)
        gas_limit = 200000 + (MSGS_PER_TX * 60000)
        fee = f"{int(gas_limit * self.network.fee_minimum_gas_price)}{DENOM}"

        # Seal and sign transaction
        tx.seal(
            SigningCfg.direct(self.wallet.public_key(), self.sequence),
            fee=TxFee(amount=fee, gas_limit=gas_limit)
        )
        tx.sign(self.wallet.signer(), self.network.chain_id, self.account_number)
        tx.complete()

        # Convert to base64 encoded protobuf
        tx_bytes = base64.b64encode(tx.tx.SerializeToString()).decode('utf-8')

        # Select random host for load distribution
        host = random.choice(HOSTS)

        # Use Locust client to broadcast
        with self.client.post(
            host + "/cosmos/tx/v1beta1/txs",
            json={
                "tx_bytes": tx_bytes,
                "mode": "BROADCAST_MODE_SYNC"
            },
            name="/cosmos/tx/v1beta1/txs [mint-batch]",
            timeout=15.0,
            catch_response=True
        ) as response:
            if response.status_code == 200:
                response_data = response.json()
                tx_response = response_data.get("tx_response", {})

                code = tx_response.get("code", -1)
                if code == 0:
                    # Success! Queue txhash for gas collection
                    txhash = tx_response.get("txhash")
                    if txhash:
                        self.pending_txs.append(txhash)
                    self.sequence += 1
                    response.success()
                else:
                    # Handle errors
                    error_msg = tx_response.get("raw_log", "") or "(no raw_log)"
                    if "account sequence mismatch" in error_msg:
                        expected_seq = self._parse_sequence_from_error(error_msg)
                        if expected_seq is not None:
                            self.sequence = expected_seq
                        else:
                            _, self.sequence = get_account_info(self.ledger, self.address)
                        response.failure("Sequence mismatch")
                    elif "insufficient funds" in error_msg.lower():
                        response.failure("Insufficient funds for gas")
                    elif "unauthorized" in error_msg.lower():
                        response.failure(f"Unauthorized: {error_msg}")
                    else:
                        response.failure(f"Transaction failed with code {code}: {error_msg}")
            else:
                text = response.text or "(no response)"
                response.failure(f"Request failed with status code {response.status_code}: {text}")
