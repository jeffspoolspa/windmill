from bs4 import BeautifulSoup
from html import unescape
import re
import psycopg2
from weasyprint import HTML

def clean_text(s: str | None) -> str | None:
    if not s:
        return None
    s = unescape(s)
    s = s.replace('\xa0', ' ')
    s = s.replace('\t', ' ')
    s = s.replace('\r', ' ')
    s = s.replace('\n', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'\\[nrt]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s or None

def get_header_content_fields(field_list: list, html_file: str):
    soup = BeautifulSoup(html_file, 'html.parser')
    results = {}
    all_rows = soup.find_all('tr')
    for i, row in enumerate(all_rows):
        cells = row.find_all('td')
        if cells:
            header = cells[0].get_text(strip=True)
            for field in field_list:
                if header == field:
                    if i + 1 < len(all_rows):
                        content = all_rows[i + 1].find('td').get_text()
                        results[field] = clean_text(content)
    return results

def label_value_same_row(field_list: list, html_file: str):
    soup = BeautifulSoup(html_file, 'html.parser')
    results = {}
    all_rows = soup.find_all('tr')
    for i, row in enumerate(all_rows):
        cells = row.find_all('td')
        for j, cell in enumerate(cells):
            cell = clean_text(cell.get_text(strip=True))
            if cell in field_list:
                results[cell] = cells[j+1].get_text()
    return results

def column_based_table(field_list: list, html_file: str):
    soup = BeautifulSoup(html_file, 'html.parser')
    results = {}
    all_rows = soup.find_all('tr')
    for i, row in enumerate(all_rows):
        cells = row.find_all('td')
        cell_texts = [cell.get_text(strip=True) for cell in cells]
        if field_list[0] in cell_texts:
                header_to_index = {text: j for j, text in enumerate(cell_texts)}
                if i + 1 < len(all_rows):
                    data_cells = all_rows[i + 1].find_all('td')
                    results = {field: clean_text(data_cells[header_to_index[field]].get_text()) 
                          for field in field_list 
                          if field in header_to_index and header_to_index[field] < len(data_cells)}
    return results

def get_acceptance_link(html_file: str):
    soup = BeautifulSoup(html_file, 'html.parser')
    link = soup.find('a', href=lambda x: 'woAccept.cfm' in x if x else False)
    href = link.get('href')
    return {"Acceptance Link": href}

def main(x):
    html_file = x['html_body']
    content_header_field_list = ["Work Description", "Customer Instructions"]
    same_row_field_list = ['Subtotal']
    column_based_fields = ['Scheduled For', 'Assigned To']
    
    results = get_header_content_fields(content_header_field_list, html_file)
    results.update(label_value_same_row(same_row_field_list, html_file))
    results.update(column_based_table(column_based_fields, html_file))
    results.update(get_acceptance_link(html_file))
    results.update({"html": html_file})
    
    headers = x.get('headers', {})
    to_list = headers.get('To', [])
    cc_list = headers.get('Cc', [])
    
    results['To'] = to_list[0]['address'] if to_list else None
    results['Cc'] = ', '.join([email['address'] for email in cc_list]) if cc_list else None
    
    soup = BeautifulSoup(html_file, 'html.parser')
    header = soup.find("h3")
    if header:
        wo_number = re.search(r"\d+", header.get_text()).group(0)
        results["Work Order #"] = wo_number
    
    return results