# import wmill
import re

def main(parsed_email):
    subject = parsed_email.get("headers").get("Subject")
    from_email = parsed_email['headers']['From'][0]['address']
    wo_number = re.search(r"\d+", subject).group(0)
    return {"subject": subject, "from": from_email, "wo_number": wo_number}