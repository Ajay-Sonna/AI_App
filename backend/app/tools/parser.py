# backend/app/tools/parser.py

from bs4 import BeautifulSoup
from urllib.parse import urljoin

def parse_html_table(html):
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    data = []
    for table in tables[:1]:  # just first table for demo
        rows = table.find_all("tr")
        for row in rows:
            cols = [col.text.strip() for col in row.find_all(["td", "th"])]
            data.append(cols)

    return data


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    items = soup.find_all("li")

    return [item.text.strip() for item in items[:10]]


def parse_div_table(html):
    soup = BeautifulSoup(html, "html.parser")
    divs = soup.find_all("div")

    rows = []
    for div in divs[:20]:
        text = div.text.strip()
        if len(text.split()) > 3:
            rows.append(text)

    return rows


def extract_file_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a")

    files = []
    for link in links:
        href = link.get("href")
        if href and any(ext in href for ext in [".xls", ".xlsx", ".pdf"]):
            full_url = urljoin(base_url, href)
            files.append(full_url)

    return files