"""Single place to tweak site branding and feed list."""

SITE_NAME = "HEADLINE REPORT"
SITE_TAGLINE = "AGGREGATED HEADLINES"
SITE_URL = "https://headlinereport.net/"
OUTPUT_PATH = "public/index.html"

# User-Agent for polite feed fetching
USER_AGENT = "HeadlineReport/1.0 (+local static aggregator; contact: local)"

# Fetch timeout seconds
FETCH_TIMEOUT = 15

# How many stories in each section
TOP_TEASER_COUNT = 4          # short list above the main headline
COLUMN_COUNT = 3
PER_COLUMN = 20               # ~15-25
RED_EMPHASIS_COUNT = 8        # how many non-lead stories get red text
MIN_CLUSTER_FOR_LEAD = 1

# Interest keywords (case-insensitive substring match) — Drudge-flavored boost
INTEREST_KEYWORDS = [
    "scandal", "breaking", "crash", "storm", "shock", "war", "markets",
    "ai", "artificial intelligence", "impeach", "resign", "resigns",
    "explosion", "bomb", "attack", "terror", "hostage", "missile",
    "nuclear", "election", "indict", "arrest", "dead", "killed", "dies",
    "hurricane", "earthquake", "wildfire", "shooting", "massacre",
    "collapse", "crisis", "emergency", "invade", "invasion", "ceasefire",
    "tariff", "recession", "stock", "bitcoin", "crypto", "leak",
    "exclusive", "reveal", "reveals", "secret", "probe", "investigation",
    "lawsuit", "verdict", "convicted", "guilty", "fbi", "cia", "pentagon",
    "white house", "congress", "senate", "trump", "xi", "putin", "nato",
]

# Feeds to attempt. Failed ones are dropped at build time.
# name = display name for source footer; url = RSS/Atom endpoint
FEEDS = [
    # Wire / global
    {"name": "AP (via Google News)", "url": "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en", "category": "wire"},
    {"name": "Reuters (via Google News)", "url": "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en", "category": "wire"},
    {"name": "BBC", "url": "https://feeds.bbci.co.uk/news/rss.xml", "category": "world"},
    {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml", "category": "world"},
    {"name": "NPR", "url": "https://feeds.npr.org/1001/rss.xml", "category": "us"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/us-news/rss", "category": "us"},
    {"name": "Guardian World", "url": "https://www.theguardian.com/world/rss", "category": "world"},
    {"name": "NYT", "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "category": "us"},
    {"name": "NYT World", "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "category": "world"},
    {"name": "Fox News", "url": "https://moxie.foxnews.com/google-publisher/latest.xml", "category": "us"},
    {"name": "Fox Politics", "url": "https://moxie.foxnews.com/google-publisher/politics.xml", "category": "politics"},
    {"name": "CNN US", "url": "http://rss.cnn.com/rss/cnn_us.rss", "category": "us"},
    {"name": "CNN World", "url": "http://rss.cnn.com/rss/cnn_world.rss", "category": "world"},
    {"name": "Politico", "url": "https://rss.politico.com/politics-news.xml", "category": "politics"},
    {"name": "Politico Picks", "url": "https://www.politico.com/rss/politicopicks.xml", "category": "politics"},
    {"name": "The Hill", "url": "https://thehill.com/news/feed/", "category": "politics"},
    {"name": "Axios", "url": "https://api.axios.com/feed/", "category": "politics"},
    {"name": "Bloomberg Politics", "url": "https://feeds.bloomberg.com/politics/news.rss", "category": "business"},
    {"name": "Bloomberg Markets", "url": "https://feeds.bloomberg.com/markets/news.rss", "category": "business"},
    {"name": "CNBC", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "category": "business"},
    {"name": "Washington Post", "url": "https://feeds.washingtonpost.com/rss/national", "category": "us"},
    {"name": "WaPo Politics", "url": "https://feeds.washingtonpost.com/rss/politics", "category": "politics"},
    {"name": "NBC News", "url": "https://feeds.nbcnews.com/feeds/topstories", "category": "us"},
    {"name": "CBS News", "url": "https://www.cbsnews.com/latest/rss/main", "category": "us"},
    {"name": "ABC News", "url": "https://abcnews.go.com/abcnews/topstories", "category": "us"},
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "world"},
    {"name": "Sky News", "url": "https://feeds.skynews.com/feeds/rss/home.xml", "category": "world"},
    {"name": "UPI", "url": "https://www.upi.com/rss/Top_News/", "category": "wire"},
    # Tech
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage", "category": "tech"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "category": "tech"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "category": "tech"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/", "category": "tech"},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss", "category": "tech"},
    # Offbeat
    {"name": "NY Post", "url": "https://nypost.com/feed/", "category": "offbeat"},
    {"name": "Oddity Central", "url": "https://www.odditycentral.com/feed", "category": "offbeat"},
]
