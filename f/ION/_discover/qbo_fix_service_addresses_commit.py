# requirements:
# wmill
# requests

from f.ION._discover.qbo_fix_service_addresses import main as fix

def main():
    return fix(dry_run=False)
