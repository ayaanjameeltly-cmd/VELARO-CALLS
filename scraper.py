import re
import json
import subprocess
import time
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

_CURL_HEADERS = [
    '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    '-H', 'Accept-Language: en-US,en;q=0.5',
]

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
PHONE_RE = re.compile(r'(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)')
LINKEDIN_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?')
VCFLINK_RE = re.compile(r'\.vcf(\?[^"\']*)?$', re.I)

TEAM_PATH_KEYS = {
    'attorney', 'attorneys', 'lawyer', 'lawyers', 'team', 'our-team', 'ourteam',
    'leadership', 'partner', 'partners', 'people', 'staff', 'professionals',
    'meet', 'bio', 'biography', 'bios', 'about', 'our-firm', 'ourfirm',
    'practice-group', 'associates', 'counsel', 'members'
}

JUNK_EMAIL_DOMAINS = {
    'example.com', 'domain.com', 'email.com', 'yourdomain.com', 'company.com',
    'sentry.io', 'wixpress.com', 'squarespace.com', 'wordpress.com',
    'google.com', 'facebook.com', 'twitter.com', 'instagram.com',
}

PRACTICE_KEYWORDS = [
    'personal injury', 'car accident', 'truck accident', 'wrongful death',
    'slip and fall', 'motorcycle', 'medical malpractice', 'workers comp',
    'product liability', 'premises liability', 'brain injury', 'spinal injury',
    'dog bite', 'nursing home', 'drunk driving', 'pedestrian', 'bicycle',
]


def _fetch(url, timeout=20):
    """Fetch a URL using curl (avoids LibreSSL TLS issues on macOS system Python)."""
    cmd = [
        'curl', '-s', '-L', '-k',
        '--max-time', str(timeout),
        '--write-out', '\n__FINAL_URL__:%{url_effective}',
        '--compressed',
    ] + _CURL_HEADERS + [url]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 8)
    # curl exit codes: 0=ok, 28=timeout, 35/51/60=SSL (ignored with -k)
    if result.returncode == 28:
        raise RuntimeError(f"Timed out fetching {url}")
    if result.returncode not in (0, 35, 51, 60):
        raise RuntimeError(f"curl error {result.returncode}: {result.stderr[:120]}")

    stdout = result.stdout
    final_url = url
    if '\n__FINAL_URL__:' in stdout:
        parts = stdout.rsplit('\n__FINAL_URL__:', 1)
        stdout = parts[0]
        final_url = parts[1].strip()

    soup = BeautifulSoup(stdout, 'html.parser')
    return soup, final_url


def _same_domain(url, base):
    return urlparse(url).netloc == urlparse(base).netloc


_PERSON_NAME_RE = re.compile(
    r"^[A-Z][a-zA-Z'\-\.]{1,20}"          # First word: capitalised
    r"(\s+[A-Z][a-zA-Z'\-\.]{0,20}){1,4}"  # 1–4 more capitalised words
    r"(\s+(Jr\.?|Sr\.?|II|III|IV|Esq\.?))?$"  # Optional suffix
)


def _looks_like_person(name):
    """Return True if name plausibly looks like a human name."""
    if not name or len(name) < 4 or len(name) > 60:
        return False
    if any(ch.isdigit() for ch in name):
        return False
    # Reject if it has too many words (article/title)
    words = name.split()
    if len(words) > 6:
        return False
    return bool(_PERSON_NAME_RE.match(name.strip()))


def _clean_phone(raw):
    if not raw:
        return ''
    digits = re.sub(r'[^\d]', '', raw)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return raw.strip()


def _filter_email(email):
    if not email:
        return ''
    domain = email.split('@')[-1].lower()
    if domain in JUNK_EMAIL_DOMAINS:
        return ''
    # Filter image/asset filenames mismatched as emails
    if re.search(r'\.(png|jpg|gif|svg|css|js|woff)$', email, re.I):
        return ''
    return email.lower().strip()


def _extract_firm_info(soup, url):
    company = ''
    location = ''
    practice_areas = []

    # Company name: JSON-LD first, then title tag
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '')
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get('@type') in ('LegalService', 'LocalBusiness', 'Organization', 'LawFirm', 'Attorney'):
                    company = item.get('name', company)
                    addr = item.get('address', {})
                    if isinstance(addr, dict):
                        city = addr.get('addressLocality', '')
                        state = addr.get('addressRegion', '')
                        location = ', '.join(filter(None, [city, state]))
        except Exception:
            pass

    if not company:
        title_tag = soup.find('title')
        if title_tag:
            raw = title_tag.get_text()
            # Take the part before | — – separators
            company = re.split(r'[|\-–—]', raw)[0].strip()

    # Practice areas: scan nav and headings for PI keywords
    text_lower = soup.get_text().lower()
    for kw in PRACTICE_KEYWORDS:
        if kw in text_lower:
            practice_areas.append(kw.title())
    practice_areas = list(dict.fromkeys(practice_areas))[:6]

    # Location fallback: look for address schema markup
    if not location:
        addr_el = soup.find(attrs={'itemprop': 'addressLocality'})
        state_el = soup.find(attrs={'itemprop': 'addressRegion'})
        if addr_el or state_el:
            location = ', '.join(filter(None, [
                addr_el.get_text(strip=True) if addr_el else '',
                state_el.get_text(strip=True) if state_el else '',
            ]))

    return company, location, ', '.join(practice_areas)


def _find_team_pages(base_url, soup):
    found = []
    seen = set()

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith('mailto:') or href.startswith('tel:') or href.startswith('#'):
            continue
        abs_url = urljoin(base_url, href).split('#')[0].split('?')[0]
        if abs_url in seen:
            continue
        if not _same_domain(abs_url, base_url):
            continue

        path = urlparse(abs_url).path.lower().strip('/')
        link_text = a.get_text(strip=True).lower()
        path_parts = set(re.split(r'[/\-_]', path))

        if path_parts & TEAM_PATH_KEYS or any(k in link_text for k in TEAM_PATH_KEYS):
            seen.add(abs_url)
            found.append(abs_url)

    # Deduplicate and prioritise (shorter paths = higher level pages)
    found.sort(key=lambda u: len(urlparse(u).path))
    return found[:8]


def _extract_contacts(page_url, soup, company, location, practice_areas):
    contacts = []
    seen_names = set()

    # ── Strategy 1: JSON-LD schema.org/Person ─────────────────────────────
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '')
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                persons = []
                if item.get('@type') in ('Person', 'Attorney'):
                    persons = [item]
                elif item.get('@type') in ('LegalService', 'LocalBusiness', 'Organization'):
                    persons = item.get('employee', []) + item.get('member', []) + item.get('founder', [])
                for p in persons:
                    if not isinstance(p, dict):
                        continue
                    name = p.get('name', '').strip()
                    if not name or name in seen_names:
                        continue
                    seen_names.add(name)
                    email_raw = p.get('email', '') or ''
                    if email_raw.startswith('mailto:'):
                        email_raw = email_raw[7:]
                    phone_raw = p.get('telephone', '') or ''
                    linkedin = ''
                    for sa in p.get('sameAs', []):
                        if 'linkedin.com/in/' in str(sa):
                            linkedin = sa
                            break
                    contacts.append({
                        'name': name,
                        'title': p.get('jobTitle', '').strip(),
                        'company': p.get('worksFor', {}).get('name', company) if isinstance(p.get('worksFor'), dict) else company,
                        'email': _filter_email(email_raw),
                        'phone': _clean_phone(phone_raw),
                        'linkedin': linkedin.strip(),
                        'source_url': page_url,
                        'vcard': False,
                        'vcard_url': '',
                        'location': location,
                        'practice_areas': practice_areas,
                    })
        except Exception:
            pass

    # ── Strategy 2: Common attorney card HTML patterns ────────────────────
    card_classes = re.compile(
        r'attorney|lawyer|team[\-_]?member|bio[\-_]?card|person[\-_]?card|'
        r'staff[\-_]?member|professional[\-_]?card|profile[\-_]?card|partner[\-_]?card',
        re.I
    )
    # Also try schema.org microdata
    cards = (
        soup.find_all(attrs={'itemtype': re.compile(r'schema\.org/Person', re.I)}) +
        [el for el in soup.find_all(['div', 'article', 'li', 'section'])
         if el.get('class') and card_classes.search(' '.join(el.get('class', [])))]
    )

    for card in cards:
        name_el = (card.find(['h1', 'h2', 'h3', 'h4']) or
                   card.find(class_=re.compile(r'name', re.I)) or
                   card.find(attrs={'itemprop': 'name'}))
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not _looks_like_person(name):
            continue
        if name in seen_names:
            continue
        seen_names.add(name)

        # Title
        title = ''
        for cls in ['title', 'position', 'role', 'job-title', 'jobtitle']:
            el = card.find(class_=re.compile(cls, re.I)) or card.find(attrs={'itemprop': 'jobTitle'})
            if el:
                title = el.get_text(strip=True)
                break
        if not title:
            sib = name_el.find_next_sibling(['p', 'div', 'span'])
            if sib:
                candidate = sib.get_text(strip=True)
                if candidate and len(candidate) < 80 and not EMAIL_RE.search(candidate):
                    title = candidate

        card_text = card.get_text()
        card_html = str(card)
        emails = [_filter_email(e) for e in EMAIL_RE.findall(card_text)]
        emails = [e for e in emails if e]
        phones = [_clean_phone(p) for p in PHONE_RE.findall(card_text) if _clean_phone(p)]
        li_match = LINKEDIN_RE.search(card_html)
        vcard_tag = card.find('a', href=VCFLINK_RE)

        contacts.append({
            'name': name,
            'title': title,
            'company': company,
            'email': emails[0] if emails else '',
            'phone': phones[0] if phones else '',
            'linkedin': li_match.group(0).rstrip('/') if li_match else '',
            'source_url': page_url,
            'vcard': bool(vcard_tag),
            'vcard_url': urljoin(page_url, vcard_tag['href']) if vcard_tag else '',
            'location': location,
            'practice_areas': practice_areas,
        })

    # ── Strategy 3: vCard links not inside a card element ─────────────────
    for a in soup.find_all('a', href=VCFLINK_RE):
        vcf_url = urljoin(page_url, a['href'])
        parent = a.find_parent(['div', 'section', 'li', 'article', 'td'])
        heading = parent.find(['h1', 'h2', 'h3', 'h4']) if parent else None
        name = (heading or a).get_text(strip=True)
        if not _looks_like_person(name) or name in seen_names:
            continue
        seen_names.add(name)
        contacts.append({
            'name': name,
            'title': '',
            'company': company,
            'email': '',
            'phone': '',
            'linkedin': '',
            'source_url': page_url,
            'vcard': True,
            'vcard_url': vcf_url,
            'location': location,
            'practice_areas': practice_areas,
        })

    # ── Strategy 4: Fallback — scrape page-level emails/phones ────────────
    if not contacts:
        page_text = soup.get_text()
        emails = list(dict.fromkeys(_filter_email(e) for e in EMAIL_RE.findall(page_text) if _filter_email(e)))
        phones = list(dict.fromkeys(_clean_phone(p) for p in PHONE_RE.findall(page_text) if _clean_phone(p)))
        li_match = LINKEDIN_RE.search(page_text)
        if emails or phones:
            contacts.append({
                'name': company or 'Unknown Contact',
                'title': '',
                'company': company,
                'email': emails[0] if emails else '',
                'phone': phones[0] if phones else '',
                'linkedin': li_match.group(0).rstrip('/') if li_match else '',
                'source_url': page_url,
                'vcard': False,
                'vcard_url': '',
                'location': location,
                'practice_areas': practice_areas,
            })

    return contacts


def _deduplicate(contacts):
    seen_email = set()
    seen_name_company = set()
    result = []
    for c in contacts:
        email = c.get('email', '').lower().strip()
        name_key = re.sub(r'\W', '', c.get('name', '').lower()) + '|' + re.sub(r'\W', '', c.get('company', '').lower())
        if email and email in seen_email:
            continue
        if not email and name_key in seen_name_company:
            continue
        if email:
            seen_email.add(email)
        seen_name_company.add(name_key)
        result.append(c)
    return result


def scrape_website(base_url):
    """
    Crawl a law firm website and return a list of contact dicts.
    Raises on fatal fetch errors; logs per-page errors silently.
    """
    if not base_url.startswith('http'):
        base_url = 'https://' + base_url

    # Fetch homepage
    home_soup, final_base = _fetch(base_url)
    company, location, practice_areas = _extract_firm_info(home_soup, final_base)

    # Discover team pages
    team_pages = _find_team_pages(final_base, home_soup)

    all_contacts = []
    visited = set()

    # Always include the homepage itself for fallback contact info
    pages_to_visit = [final_base] + team_pages

    for page_url in pages_to_visit:
        if page_url in visited:
            continue
        visited.add(page_url)

        try:
            if page_url == final_base:
                soup = home_soup
            else:
                soup, _ = _fetch(page_url)
                time.sleep(0.4)

            contacts = _extract_contacts(page_url, soup, company, location, practice_areas)
            all_contacts.extend(contacts)
        except Exception:
            pass  # Individual page failures are non-fatal

    return _deduplicate(all_contacts)
