from moloni_tools import moloni_connect

token = moloni_connect()
print("Connected:", bool(token))
print("Token preview:", token[:8] + "...")

