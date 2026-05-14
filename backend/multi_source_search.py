"""
JobRadar - Multi-Source Job Aggregator
Searches across 9 job sources to find the best matching jobs
"""

import os
import re
import json
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

# Load .env from the same directory as this file
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ===== CONFIGURABLE PARAMETERS =====
COUNTRY = ''                # Empty = ALL Europe, or: de, gb, fr, nl, ch, at, in, us
JOB_TITLE = 'AI Engineer'
LOCATION = ''               # Empty = all locations
DAYS_OLD = 7
EXPERIENCE_LEVEL = ''       # entry, junior, mid, senior, lead (empty = all)
MAX_RESULTS_PER_SOURCE = 20
# ===================================

# European countries supported by Adzuna
EUROPEAN_COUNTRIES = {
    'de': 'Germany',
    'gb': 'United Kingdom',
    'fr': 'France',
    'nl': 'Netherlands',
    'ch': 'Switzerland',
    'at': 'Austria',
    'it': 'Italy',
    'es': 'Spain',
    'pl': 'Poland',
}

# Location keywords per country — used to post-filter non-Adzuna sources
COUNTRY_KEYWORDS = {
    'de': ['germany', 'deutschland', 'berlin', 'munich', 'münchen', 'frankfurt',
           'hamburg', 'cologne', 'köln', 'düsseldorf', 'stuttgart', 'leipzig',
           'dortmund', 'essen', 'bremen', 'hannover', 'nuremberg', 'nürnberg',
           'bonn', 'karlsruhe', 'augsburg', 'wiesbaden', 'bielefeld'],
    'gb': ['united kingdom', ' uk,', 'england', 'london', 'manchester',
           'birmingham', 'leeds', 'glasgow', 'edinburgh', 'bristol',
           'sheffield', 'liverpool', 'cardiff', 'scotland', 'wales'],
    'fr': ['france', 'paris', 'lyon', 'marseille', 'toulouse', 'nice',
           'nantes', 'bordeaux', 'strasbourg', 'lille', 'rennes'],
    'nl': ['netherlands', 'holland', 'amsterdam', 'rotterdam', 'the hague',
           'den haag', 'utrecht', 'eindhoven', 'tilburg', 'groningen'],
    'ch': ['switzerland', 'schweiz', 'zurich', 'zürich', 'geneva', 'genf',
           'bern', 'lausanne', 'basel', 'winterthur'],
    'at': ['austria', 'österreich', 'vienna', 'wien', 'graz', 'linz', 'salzburg'],
    'it': ['italy', 'italia', 'rome', 'roma', 'milan', 'milano', 'turin',
           'torino', 'naples', 'napoli', 'florence', 'firenze'],
    'es': ['spain', 'españa', 'madrid', 'barcelona', 'valencia',
           'seville', 'sevilla', 'bilbao', 'málaga'],
    'pl': ['poland', 'polska', 'warsaw', 'warszawa', 'krakow', 'kraków',
           'wrocław', 'gdańsk', 'poznań'],
}

REMOTE_KEYWORDS = ['remote', 'worldwide', 'anywhere', 'global', 'work from home',
                   'distributed', 'fully remote', 'home office']


def _is_remote_job(job: dict) -> bool:
    loc = (job.get('location') or '').lower()
    country = (job.get('country') or '').lower()
    return country == 'remote' or any(kw in loc for kw in REMOTE_KEYWORDS)


def _location_matches_country(location: str, country_code: str) -> bool:
    """True if location text suggests the job is in the given country."""
    if not country_code:
        return True
    loc = location.lower()
    return any(kw in loc for kw in COUNTRY_KEYWORDS.get(country_code, []))


# ========================================
# SOURCE 1: ADZUNA
# ========================================
def search_adzuna(title, country, location, days_old, max_results):
    """Adzuna API - Single country"""
    app_id = os.getenv('ADZUNA_APP_ID')
    app_key = os.getenv('ADZUNA_APP_KEY')
    
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    params = {
        'app_id': app_id,
        'app_key': app_key,
        'what': title,
        'results_per_page': max_results,
        'max_days_old': days_old
    }
    
    if location:
        params['where'] = location
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        jobs = []
        for job in data.get('results', []):
            jobs.append({
                'title': job.get('title'),
                'company': job.get('company', {}).get('display_name', 'N/A'),
                'location': job.get('location', {}).get('display_name', 'N/A'),
                'url': job.get('redirect_url'),
                'posted': job.get('created'),
                'description': job.get('description', ''),
                'source': f'Adzuna ({EUROPEAN_COUNTRIES.get(country, country.upper())})',
                'country': country,
                'salary_min': job.get('salary_min'),
                'salary_max': job.get('salary_max')
            })
        return jobs
    except Exception as e:
        return []


def search_adzuna_all_europe(title, location, days_old, max_results):
    """Adzuna - All European countries"""
    all_jobs = []
    for country_code in EUROPEAN_COUNTRIES:
        jobs = search_adzuna(title, country_code, location, days_old, max_results)
        all_jobs.extend(jobs)
    return all_jobs


# ========================================
# SOURCE 2: REMOTIVE
# ========================================
def search_remotive(title, max_results, days_old=None):
    """Remotive API - Remote jobs"""
    url = "https://remotive.com/api/remote-jobs"

    cutoff = None
    if days_old:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_old)

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        jobs = []
        for job in data.get('jobs', []):
            pub = job.get('publication_date', '')
            if cutoff and pub:
                dt = _parse_posted_date(pub)
                if dt is not None and dt < cutoff:
                    continue   # skip jobs older than the requested range

            jt = job.get('title', '')
            jd = job.get('description', '')
            if matches_query(jt, jd, title):
                jobs.append({
                    'title': jt,
                    'company': job.get('company_name'),
                    'location': 'Remote',
                    'url': job.get('url'),
                    'posted': pub,
                    'description': jd,
                    'source': 'Remotive',
                    'country': 'remote',
                    'salary_min': None,
                    'salary_max': None
                })
                if len(jobs) >= max_results:
                    break
        return jobs
    except Exception:
        return []


# ========================================
# SOURCE 3: ARBEITNOW
# ========================================
def search_arbeitnow(title, location, max_results):
    """Arbeitnow API - Europe + Remote"""
    url = "https://www.arbeitnow.com/api/job-board-api"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        jobs = []
        for job in data.get('data', []):
            job_location = job.get('location', '').lower()
            location_match = not location or location.lower() in job_location

            if matches_query(job.get('title', ''), job.get('description', ''), title) and location_match:
                jobs.append({
                    'title': job.get('title'),
                    'company': job.get('company_name'),
                    'location': job.get('location', 'Europe/Remote'),
                    'url': job.get('url'),
                    'posted': datetime.fromtimestamp(job.get('created_at', 0)).strftime('%Y-%m-%d') if job.get('created_at') else 'N/A',
                    'description': job.get('description', ''),
                    'source': 'Arbeitnow',
                    'country': 'europe',
                    'salary_min': None,
                    'salary_max': None
                })
                if len(jobs) >= max_results:
                    break
        return jobs
    except Exception as e:
        return []


# ========================================
# SOURCE 4: REMOTEOK
# ========================================
def search_remoteok(title, max_results):
    """RemoteOK API - Remote jobs"""
    url = "https://remoteok.com/api"
    headers = {'User-Agent': 'JobRadar/1.0'}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        jobs_data = data[1:] if len(data) > 1 else []
        
        jobs = []
        for job in jobs_data:
            jt = job.get('position', '')
            jd = job.get('description', '')
            if matches_query(jt, jd, title):
                jobs.append({
                    'title': jt,
                    'company': job.get('company'),
                    'location': job.get('location', 'Remote'),
                    'url': job.get('url', ''),
                    'posted': job.get('date', ''),
                    'description': jd,
                    'source': 'RemoteOK',
                    'country': 'remote',
                    'salary_min': job.get('salary_min'),
                    'salary_max': job.get('salary_max')
                })
                if len(jobs) >= max_results:
                    break
        return jobs
    except Exception as e:
        return []


# ========================================
# SOURCE 5: JOBICY
# ========================================
def search_jobicy(title, max_results):
    """Jobicy API - Remote jobs aggregator"""
    url = "https://jobicy.com/api/v2/remote-jobs"
    
    params = {
        'count': max_results,
        'tag': title
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        jobs = []
        for job in data.get('jobs', []):
            jobs.append({
                'title': job.get('jobTitle'),
                'company': job.get('companyName'),
                'location': job.get('jobGeo', 'Remote'),
                'url': job.get('url', ''),
                'posted': job.get('pubDate', ''),
                'description': job.get('jobDescription', ''),
                'source': 'Jobicy',
                'country': 'remote',
                'salary_min': None,
                'salary_max': None
            })
        return jobs
    except Exception as e:
        return []


# ========================================
# SOURCE 6: HACKER NEWS WHO'S HIRING
# ========================================
def search_hackernews(title, max_results):
    """Hacker News - Who's Hiring threads"""
    try:
        # Get latest "Who's Hiring" thread
        url = "https://hn.algolia.com/api/v1/search"
        params = {
            'query': 'Ask HN: Who is hiring',
            'tags': 'story',
            'hitsPerPage': 1
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if not data.get('hits'):
            return []
        
        story_id = data['hits'][0]['objectID']
        
        # Get comments (job postings)
        params = {
            'tags': f'comment,story_{story_id}',
            'hitsPerPage': 100
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        jobs = []
        for hit in data.get('hits', []):
            comment = hit.get('comment_text', '')
            if matches_query('', comment, title):
                # Clean HTML and get first line
                clean_comment = re.sub(r'<[^>]+>', '', comment)
                first_line = clean_comment.split('\n')[0][:120]
                
                jobs.append({
                    'title': first_line,
                    'company': 'See HN comment',
                    'location': 'Various',
                    'url': f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    'posted': hit.get('created_at', ''),
                    'description': clean_comment[:500],
                    'source': 'Hacker News',
                    'country': 'global',
                    'salary_min': None,
                    'salary_max': None
                })
                if len(jobs) >= max_results:
                    break
        return jobs
    except Exception as e:
        return []


# ========================================
# SOURCE 7: WEWORKREMOTELY
# ========================================
def search_weworkremotely(title, max_results):
    """WeWorkRemotely - Remote jobs RSS"""
    url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        content = response.text
        
        items = re.findall(r'<item>(.*?)</item>', content, re.DOTALL)
        
        jobs = []
        for item in items[:50]:
            title_match = re.search(r'<title>(.*?)</title>', item, re.DOTALL)
            link_match = re.search(r'<link>(.*?)</link>', item, re.DOTALL)
            
            if title_match and link_match:
                full_title = title_match.group(1).replace('<![CDATA[', '').replace(']]>', '').strip()
                link = link_match.group(1).strip()
                pub_match = re.search(r'<pubDate>(.*?)</pubDate>', item, re.DOTALL)
                posted = pub_match.group(1).strip() if pub_match else ''

                if matches_query(full_title, '', title):
                    if ':' in full_title:
                        parts = full_title.split(':', 1)
                        company = parts[0].strip()
                        job_title = parts[1].strip()
                    else:
                        company = 'N/A'
                        job_title = full_title

                    jobs.append({
                        'title': job_title,
                        'company': company,
                        'location': 'Remote',
                        'url': link,
                        'posted': posted,
                        'description': '',
                        'source': 'WeWorkRemotely',
                        'country': 'remote',
                        'salary_min': None,
                        'salary_max': None
                    })
                    if len(jobs) >= max_results:
                        break
        return jobs
    except Exception as e:
        return []


# ========================================
# SOURCE 8: GRAPHQL JOBS
# ========================================
def search_graphql_jobs(title, max_results):
    """GraphQL Jobs - Tech jobs"""
    url = "https://graphql-jobs.vercel.app/api"
    
    query = """
    query {
        jobs {
            title
            slug
            publishedAt
            company {
                name
            }
            cities {
                name
            }
        }
    }
    """
    
    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(url, json={'query': query}, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        jobs = []
        for job in data.get('data', {}).get('jobs', []):
            if matches_query(job.get('title', ''), '', title):
                cities = ', '.join([c.get('name', '') for c in job.get('cities', [])]) or 'Remote'
                jobs.append({
                    'title': job.get('title'),
                    'company': job.get('company', {}).get('name'),
                    'location': cities,
                    'url': f"https://graphql.jobs/jobs/{job.get('slug')}",
                    'posted': job.get('publishedAt', ''),
                    'description': '',
                    'source': 'GraphQL Jobs',
                    'country': 'global',
                    'salary_min': None,
                    'salary_max': None
                })
                if len(jobs) >= max_results:
                    break
        return jobs
    except Exception as e:
        return []


# ========================================
# SOURCE 9: WORKING NOMADS
# ========================================
def search_workingnomads(title, max_results):
    """Working Nomads - Remote jobs"""
    url = "https://www.workingnomads.com/api/exposed_jobs/"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        jobs = []
        for job in data:
            if matches_query(job.get('title', ''), job.get('description', ''), title):
                jobs.append({
                    'title': job.get('title'),
                    'company': job.get('company_name'),
                    'location': job.get('location', 'Remote'),
                    'url': job.get('url', ''),
                    'posted': job.get('pub_date', ''),
                    'description': job.get('description', ''),
                    'source': 'Working Nomads',
                    'country': 'remote',
                    'salary_min': None,
                    'salary_max': None
                })
                if len(jobs) >= max_results:
                    break
        return jobs
    except Exception as e:
        return []


# ========================================
# UTILITY FUNCTIONS
# ========================================
def matches_query(title: str, description: str, query: str) -> bool:
    """Return True only if the job title contains all search keywords.

    Uses word-boundary regex so 'ai' never matches inside 'email'/'detail'/etc.
    Falls back to checking description only if the query has 3+ words and all
    but one keyword are already in the title.
    """
    if not query.strip():
        return True

    title_lower = title.lower()
    query_lower = query.lower().strip()
    keywords = [w for w in query_lower.split() if len(w) > 1]
    if not keywords:
        return True

    def word_in(text, word):
        return bool(re.search(r'\b' + re.escape(word) + r'\b', text))

    # Rule 1: exact phrase in title
    if query_lower in title_lower:
        return True

    # Rule 2: every keyword in title
    if all(word_in(title_lower, kw) for kw in keywords):
        return True

    # Rule 3: 3+ word queries — all-but-one in title, remainder in description
    if len(keywords) >= 3:
        title_hits = sum(1 for kw in keywords if word_in(title_lower, kw))
        if title_hits >= len(keywords) - 1:
            combined = title_lower + ' ' + description.lower()
            return all(word_in(combined, kw) for kw in keywords)

    return False


def _classify_by_yoe(jobs, target_level):
    """Filter ambiguous jobs using years-of-experience patterns in the description.

    YOE ranges per level:
      entry  0-2 yrs  |  junior 0-3  |  mid 2-6  |  senior 5+  |  lead 7+

    Jobs with no detectable YOE requirement are kept (unlabelled jobs can fit any level).
    Jobs whose minimum YOE requirement falls outside the target range are dropped.
    """
    yoe_ranges = {
        'entry':  (0, 2),
        'junior': (0, 3),
        'mid':    (2, 6),
        'senior': (5, 99),
        'lead':   (7, 99),
    }
    low, high = yoe_ranges.get(target_level, (0, 99))

    result = []
    for job in jobs:
        desc = re.sub(r'<[^>]+>', '', job.get('description', '')).lower()

        # Pattern 1: "3+ years of experience", "2-5 years experience"
        found = re.findall(
            r'(\d+)\s*\+?\s*(?:to\s*\d+\s*)?years?\s+(?:of\s+)?(?:\w+\s+){0,2}experience',
            desc
        )
        # Pattern 2: "minimum 5 years", "at least 3 years", "requires 4 years"
        if not found:
            found = re.findall(
                r'(?:minimum|requires?\s+(?:at\s+least\s+)?|at\s+least\s+)(\d+)\s*years?',
                desc
            )
        # Pattern 3: bare "X years" close to experience/exp keywords
        if not found:
            found = re.findall(r'(\d{1,2})\s*\+\s*years?', desc)

        if not found:
            result.append(job)  # no YOE signal → keep (benefit of the doubt)
            continue

        yoe_values = [int(x) for x in found if x.isdigit()]
        if not yoe_values:
            result.append(job)
            continue

        min_yoe = min(yoe_values)
        if low <= min_yoe <= high:
            result.append(job)
        # else: YOE found but outside target range → drop

    return result


def filter_by_experience(jobs, experience_level):
    """Filter jobs by experience level.

    Step 1 — title has a conflicting-level keyword  → hard skip (no LLM needed).
    Step 2 — title has a matching-level keyword      → keep immediately.
    Step 3 — title is level-neutral, JD has signal   → keep immediately.
    Step 4 — still ambiguous (no signal anywhere)    → send to Groq LLaMA to decide.
    """
    if not experience_level:
        return jobs

    level = experience_level.lower()

    conflicting = {
        'entry':  ['senior', 'sr.', ' sr ', 'lead', 'principal', 'staff',
                   'director', 'head of', 'vp ', 'manager', 'architect',
                   'mid-level', 'mid level', 'experienced'],
        'junior': ['senior', 'sr.', ' sr ', 'lead', 'principal', 'staff',
                   'director', 'head of', 'vp ', 'manager', 'architect'],
        'mid':    ['senior', 'sr.', ' sr ', 'lead', 'principal', 'staff',
                   'director', 'head of', 'vp ', 'manager', 'architect',
                   'junior', ' jr ', 'intern', 'trainee', 'graduate', 'entry'],
        'senior': ['junior', ' jr ', 'intern', 'trainee', 'graduate', 'entry'],
        'lead':   ['junior', ' jr ', 'intern', 'trainee', 'graduate', 'entry'],
    }

    title_match = {
        'entry':  ['entry', 'junior', 'jr.', 'intern', 'trainee',
                   'graduate', 'fresher', 'associate', 'beginner'],
        'junior': ['junior', ' jr ', 'jr.', 'entry', 'associate'],
        'mid':    ['mid-level', 'mid level', 'intermediate', 'midlevel'],
        'senior': ['senior', 'sr.', ' sr '],
        'lead':   ['lead', 'principal', 'staff engineer', 'architect',
                   'head of', 'director', ' vp'],
    }

    jd_match = {
        'entry':  ['entry level', 'entry-level', '0-1 year', '0-2 year',
                   '0 to 1', '0 to 2', 'no experience required',
                   'fresh graduate', 'new grad', 'recent graduate',
                   'junior', 'intern', 'trainee', 'beginner'],
        'junior': ['junior', '0-2 year', '1-3 year', '0 to 2', '1 to 3',
                   'entry level', 'entry-level', 'some experience'],
        'mid':    ['mid-level', 'mid level', 'intermediate', '2-5 year',
                   '3-5 year', '2 to 5', '3 to 5', 'several years'],
        'senior': ['senior', '5+ year', '6+ year', '7+ year', '5 to',
                   'extensive experience', 'expert', 'seasoned'],
        'lead':   ['lead', 'principal', 'architect', 'head of', 'director',
                   '8+ year', '10+ year', 'management experience',
                   'team lead', 'people management'],
    }

    conf  = conflicting.get(level, [])
    t_inc = title_match.get(level, [])
    d_inc = jd_match.get(level, [])

    kept      = []
    ambiguous = []

    for job in jobs:
        title = job.get('title', '').lower()
        desc  = job.get('description', '').lower()

        if any(kw in title for kw in conf):
            continue                          # step 1: hard skip

        if any(kw in title for kw in t_inc):
            kept.append(job)                  # step 2: title match
            continue

        if any(kw in desc for kw in d_inc):
            kept.append(job)                  # step 3: JD keyword match
            continue

        ambiguous.append(job)                 # step 4: classify by YOE regex

    yoe_kept = _classify_by_yoe(ambiguous, level)

    # For senior/lead: only keep ambiguous jobs that passed YOE check
    # For entry/junior/mid: give benefit of doubt to jobs with no YOE signal
    if level in ('senior', 'lead'):
        kept.extend(yoe_kept)
    else:
        # Keep jobs that passed YOE + jobs with no YOE signal (already handled inside _classify_by_yoe)
        kept.extend(yoe_kept)

    return kept


def _parse_posted_date(value):
    """Parse a job's posted date into a datetime, or return None if unparseable."""
    if not value:
        return None

    # Unix timestamp (RemoteOK returns integers or digit strings)
    try:
        ts = int(str(value).strip())
        if 1_000_000_000 <= ts <= 9_999_999_999:
            return datetime.fromtimestamp(ts)
    except (ValueError, TypeError):
        pass

    s = str(value).strip()

    # RFC 2822 format from RSS feeds: "Mon, 15 Jan 2024 00:00:00 +0000"
    # Only attempt this if the string looks like RFC 2822 (contains a month abbreviation)
    if re.search(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b', s):
        try:
            from email.utils import parsedate
            t = parsedate(s)
            if t and t[0] and t[0] > 1900:
                return datetime(*t[:6])
        except Exception:
            pass

    # Strip timezone suffixes for ISO-style formats
    s = re.sub(r'[Zz]$', '', s)
    s = re.sub(r'\s*[+-]\d{2}:\d{2}$', '', s)
    s = re.sub(r'\s*[+-]\d{4}$', '', s)
    s = s[:19]

    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _extract_date_from_description(description):
    """Extract a posting date from relative phrases or ISO dates in the description text."""
    if not description:
        return None
    text = re.sub(r'<[^>]+>', ' ', description).lower()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    m = re.search(r'(\d+)\s*hours?\s+ago', text)
    if m: return now - timedelta(hours=int(m.group(1)))

    if re.search(r'\btoday\b|\bjust\s+now\b', text):
        return now

    if re.search(r'\byesterday\b', text):
        return now - timedelta(days=1)

    m = re.search(r'(\d+)\s*days?\s+ago', text)
    if m: return now - timedelta(days=int(m.group(1)))

    m = re.search(r'(\d+)\s*weeks?\s+ago', text)
    if m: return now - timedelta(weeks=int(m.group(1)))

    m = re.search(r'(\d+)\s*months?\s+ago', text)
    if m: return now - timedelta(days=int(m.group(1)) * 30)

    # ISO date in description: 2024-01-15
    m = re.search(r'\b(20\d{2})[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])\b', text)
    if m:
        try: return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception: pass

    return None


def _groq_extract_dates(jobs):
    """Ask Groq LLaMA to find posting dates from job description snippets.
    Returns a list of datetime|None in the same order as input jobs.
    """
    api_key = os.getenv('GROQ_API_KEY')
    results = [None] * len(jobs)
    if not api_key or not jobs:
        return results
    try:
        from groq import Groq
        client = Groq(api_key=api_key)

        lines = []
        indices = []
        for i, job in enumerate(jobs):
            snippet = re.sub(r'<[^>]+>', ' ', job.get('description', ''))[:600].strip()
            if snippet:
                lines.append(f'{len(lines)+1}. Title: {job.get("title","")} | Text: {snippet}')
                indices.append(i)

        if not lines:
            return results

        today = datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d')
        prompt = (
            f'Today is {today}. Each item is a job listing. '
            'Look for any posted/published date mentioned. '
            'Reply ONLY with a JSON array — no prose.\n'
            'Format: [{"id":1,"date":"2024-01-15"}] — use null if no date found.\n\n'
            + '\n'.join(lines)
        )
        resp = client.chat.completions.create(
            model='llama-3.1-8b-instant',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0,
            max_tokens=300,
        )
        raw = resp.choices[0].message.content.strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            return results
        for item in json.loads(match.group()):
            idx_in_lines = item.get('id', 0) - 1
            if 0 <= idx_in_lines < len(indices):
                dt = _parse_posted_date(item.get('date'))
                if dt:
                    results[indices[idx_in_lines]] = dt
    except Exception:
        pass
    return results


def filter_by_date(jobs, days_old):
    """Keep only jobs posted within days_old days.

    Date resolution order:
      1. Parse the job's 'posted' field.
      2. Scan description for relative phrases ("3 days ago") or ISO dates.
      3. Batch-send remaining jobs with descriptions to Groq for date extraction.
      4. Jobs with no detectable date are excluded (user set a filter — unknown = excluded).
    """
    if not days_old:
        return jobs
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_old)

    kept = []
    no_date = []

    for job in jobs:
        dt = _parse_posted_date(job.get('posted'))
        if dt is None:
            dt = _extract_date_from_description(job.get('description', ''))
        if dt is not None:
            if dt >= cutoff:
                kept.append(job)
        else:
            no_date.append(job)

    # Groq pass for jobs that still have no date
    if no_date:
        groq_dates = _groq_extract_dates(no_date)
        for job, dt in zip(no_date, groq_dates):
            if dt is not None and dt >= cutoff:
                kept.append(job)
            # no date found at all → excluded

    return kept


def filter_by_country(jobs, country_code):
    """Keep only jobs in the selected country.
    Remote/worldwide jobs always pass through regardless of country selection.
    Adzuna jobs already carry the correct country code.
    Other sources are matched by location keywords.
    """
    if not country_code:
        return jobs

    result = []
    for job in jobs:
        # Remote jobs are always included (can be done from anywhere)
        if _is_remote_job(job):
            result.append(job)
            continue

        job_country = (job.get('country') or '').lower()

        # Adzuna sets country to the ISO code — exact match
        if job_country == country_code.lower():
            result.append(job)
            continue

        # For other sources, check location text
        location = (job.get('location') or '').lower()
        if _location_matches_country(location, country_code):
            result.append(job)

    return result


def filter_by_work_mode(jobs, work_mode):
    """Filter jobs by work mode: remote, onsite, or hybrid."""
    if not work_mode:
        return jobs

    mode = work_mode.lower()
    filtered = []

    for job in jobs:
        location = (job.get('location') or '').lower()
        description = (job.get('description') or '').lower()
        country = (job.get('country') or '').lower()

        if mode == 'remote':
            if (country == 'remote'
                    or 'remote' in location
                    or 'worldwide' in location
                    or 'anywhere' in location):
                filtered.append(job)

        elif mode == 'onsite':
            if (country not in ('remote', 'global')
                    and 'remote' not in location
                    and 'worldwide' not in location
                    and 'anywhere' not in location):
                filtered.append(job)

        elif mode == 'hybrid':
            if 'hybrid' in location or 'hybrid' in description:
                filtered.append(job)

    return filtered


def deduplicate_jobs(jobs):
    """Remove duplicates by URL"""
    seen_urls = set()
    unique = []
    
    for job in jobs:
        url = job.get('url')
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique.append(job)
    
    return unique


def calculate_match_score(job, search_title):
    """Calculate match score based on title similarity"""
    job_title = job.get('title', '').lower()
    search_words = set(search_title.lower().split())
    job_words = set(job_title.split())
    
    matches = len(search_words.intersection(job_words))
    total = len(search_words)
    
    if total == 0:
        return 0
    
    score = int((matches / total) * 100)
    return score


# ========================================
# MAIN FUNCTION
# ========================================
def main():
    print("=" * 80)
    print("🎯 JOBRADAR - Multi-Source Job Aggregator (9 Sources)")
    print("=" * 80)
    
    if not COUNTRY:
        search_scope = "All Europe 🇪🇺 (9 countries)"
    else:
        search_scope = EUROPEAN_COUNTRIES.get(COUNTRY, COUNTRY.upper())
    
    print(f"\n🔍 Search Parameters:")
    print(f"   Job Title: {JOB_TITLE}")
    print(f"   Country: {search_scope}")
    print(f"   Location: {'All locations' if not LOCATION else LOCATION}")
    print(f"   Experience: {'All levels' if not EXPERIENCE_LEVEL else EXPERIENCE_LEVEL.capitalize()}")
    print(f"   Posted: Last {DAYS_OLD} days")
    print(f"\n📡 Searching across 9 sources...")
    
    all_jobs = []
    
    # Source 1: Adzuna
    print(f"\n   [1/9] Adzuna...", end=' ')
    if COUNTRY:
        adzuna_jobs = search_adzuna(JOB_TITLE, COUNTRY, LOCATION, DAYS_OLD, MAX_RESULTS_PER_SOURCE)
        print(f"✅ {len(adzuna_jobs)} jobs")
    else:
        adzuna_jobs = search_adzuna_all_europe(JOB_TITLE, LOCATION, DAYS_OLD, MAX_RESULTS_PER_SOURCE)
        print(f"   ✅ Total Adzuna: {len(adzuna_jobs)} jobs")
    all_jobs.extend(adzuna_jobs)
    
    # Source 2: Remotive
    print(f"\n   [2/9] Remotive...", end=' ')
    remotive_jobs = search_remotive(JOB_TITLE, MAX_RESULTS_PER_SOURCE)
    all_jobs.extend(remotive_jobs)
    print(f"✅ {len(remotive_jobs)} jobs")
    
    # Source 3: Arbeitnow
    print(f"   [3/9] Arbeitnow...", end=' ')
    arbeitnow_jobs = search_arbeitnow(JOB_TITLE, LOCATION, MAX_RESULTS_PER_SOURCE)
    all_jobs.extend(arbeitnow_jobs)
    print(f"✅ {len(arbeitnow_jobs)} jobs")
    
    # Source 4: RemoteOK
    print(f"   [4/9] RemoteOK...", end=' ')
    remoteok_jobs = search_remoteok(JOB_TITLE, MAX_RESULTS_PER_SOURCE)
    all_jobs.extend(remoteok_jobs)
    print(f"✅ {len(remoteok_jobs)} jobs")
    
    # Source 5: Jobicy
    print(f"   [5/9] Jobicy...", end=' ')
    jobicy_jobs = search_jobicy(JOB_TITLE, MAX_RESULTS_PER_SOURCE)
    all_jobs.extend(jobicy_jobs)
    print(f"✅ {len(jobicy_jobs)} jobs")
    
    # Source 6: Hacker News
    print(f"   [6/9] Hacker News...", end=' ')
    hn_jobs = search_hackernews(JOB_TITLE, MAX_RESULTS_PER_SOURCE)
    all_jobs.extend(hn_jobs)
    print(f"✅ {len(hn_jobs)} jobs")
    
    # Source 7: WeWorkRemotely
    print(f"   [7/9] WeWorkRemotely...", end=' ')
    wwr_jobs = search_weworkremotely(JOB_TITLE, MAX_RESULTS_PER_SOURCE)
    all_jobs.extend(wwr_jobs)
    print(f"✅ {len(wwr_jobs)} jobs")
    
    # Source 8: GraphQL Jobs
    print(f"   [8/9] GraphQL Jobs...", end=' ')
    graphql_jobs = search_graphql_jobs(JOB_TITLE, MAX_RESULTS_PER_SOURCE)
    all_jobs.extend(graphql_jobs)
    print(f"✅ {len(graphql_jobs)} jobs")
    
    # Source 9: Working Nomads
    print(f"   [9/9] Working Nomads...", end=' ')
    wn_jobs = search_workingnomads(JOB_TITLE, MAX_RESULTS_PER_SOURCE)
    all_jobs.extend(wn_jobs)
    print(f"✅ {len(wn_jobs)} jobs")
    
    # Filter by experience
    if EXPERIENCE_LEVEL:
        all_jobs = filter_by_experience(all_jobs, EXPERIENCE_LEVEL)
    
    # Deduplicate
    unique_jobs = deduplicate_jobs(all_jobs)
    
    # Calculate match scores
    for job in unique_jobs:
        job['match_score'] = calculate_match_score(job, JOB_TITLE)
    
    # Sort by match score
    unique_jobs.sort(key=lambda x: x['match_score'], reverse=True)
    
    # Display results
    print(f"\n" + "=" * 80)
    print(f"📊 RESULTS SUMMARY")
    print("=" * 80)
    print(f"   Total found: {len(all_jobs)}")
    print(f"   Unique jobs: {len(unique_jobs)}")
    print(f"   Duplicates removed: {len(all_jobs) - len(unique_jobs)}")
    
    # Count by source
    print(f"\n📈 Jobs by Source:")
    sources = {}
    for job in unique_jobs:
        source = job['source']
        sources[source] = sources.get(source, 0) + 1
    
    for source, count in sorted(sources.items(), key=lambda x: x[1], reverse=True):
        print(f"   {source}: {count}")
    
    print(f"\n" + "=" * 80)
    print(f"🏆 TOP MATCHING JOBS (sorted by relevance)")
    print("=" * 80 + "\n")
    
    # Display top 30 jobs
    for i, job in enumerate(unique_jobs[:30], 1):
        match = job['match_score']
        
        if match >= 80:
            indicator = "🟢 EXCELLENT"
        elif match >= 60:
            indicator = "🟡 GOOD"
        else:
            indicator = "🟠 FAIR"
        
        print(f"{i}. {job['title']}")
        print(f"   Match: {match}% {indicator}")
        print(f"   Company: {job['company']}")
        print(f"   Location: {job['location']}")
        print(f"   Source: {job['source']}")
        print(f"   Posted: {job['posted']}")
        
        if job.get('salary_max'):
            print(f"   Salary: €{job['salary_min']:,.0f} - €{job['salary_max']:,.0f}")
        
        print(f"   URL: {job['url']}\n")
    
    if len(unique_jobs) > 30:
        print(f"... and {len(unique_jobs) - 30} more jobs")
    
    print(f"\n{'=' * 80}")
    print(f"✅ JobRadar found {len(unique_jobs)} unique opportunities across 9 platforms!")
    print(f"{'=' * 80}\n")


if __name__ == "__main__":
    main()