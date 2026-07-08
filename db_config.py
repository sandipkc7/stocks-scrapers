import configparser
import os

config = configparser.ConfigParser()

# Looks for config.ini in the same directory as this file
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

if not os.path.exists(config_path):
    raise FileNotFoundError(
        f"config.ini not found at {config_path}\n"
        f"Copy config.ini.example to config.ini and fill in your database credentials."
    )

config.read(config_path)

DB_CONFIG = {
    'host':     config['database']['host'],
    'database': config['database']['dbname'],
    'user':     config['database']['user'],
    'password': config['database']['password'],
    'port':     config.get('database', 'port', fallback='5432'),
}
