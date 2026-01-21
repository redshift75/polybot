from py_clob_client.client import ClobClient

host: str = "https://clob.polymarket.com"
key: str = "0x497aed54989c72d0ceb1ecd61278dc145d8dd76532ee7d9034ecff0ea368e74d" #This is your Private Key. If using email login export from https://reveal.magic.link/polymarket otherwise export from your Web3 Application
chain_id: int = 137 #No need to adjust this
POLYMARKET_PROXY_ADDRESS: str = '' #This is the address you deposit/send USDC to to FUND your Polymarket account.

#Select from the following 3 initialization options to matches your login method, and remove any unused lines so only one client is initialized.

### Initialization of a client using a Polymarket Proxy associated with an Email/Magic account. If you login with your email use this example.
client = ClobClient(host, key=key, chain_id=chain_id, signature_type=1, funder=POLYMARKET_PROXY_ADDRESS)

print( client.derive_api_key() )