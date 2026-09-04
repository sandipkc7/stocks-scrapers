import configparser
import os

config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(__file__), '..', 'config.ini')

if not os.path.exists(config_path):
    raise FileNotFoundError(f"Database configuration file not found at {config_path}")

config.read(config_path)

DB_CONFIG = {
    'host': config['database']['host'],
    'database': config['database']['dbname'],
    'user': config['database']['user'],
    'password': config['database']['password']
}
