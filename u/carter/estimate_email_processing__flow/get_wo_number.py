# import wmill
import re

def main(x: str):
    results = {}
    wo_number = re.search(r"\d+", x).group(0)
    results["Work Order #"] = wo_number
    return results