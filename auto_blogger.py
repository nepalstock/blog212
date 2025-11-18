import os
import json
import requests
import feedparser
import base64
import pickle
import google.generativeai as genai
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from time import sleep
# --- NEW SERVICE ACCOUNT IMPORTS ---
from google.oauth2 import service_account 
from google_auth_oauthlib.flow import InstalledAppFlow # Kept for backward compatibility check

# --- CONFIGURATION (YOUR BLOGGER ID IS SET) ---
BLOG_ID = '2756397129078048447' 
RSS_URL = 'https://nepsestock.com/feed'
JSON_URL = 'https://bajarkochirfar.com/wp-json/api/v1/short-news'
MAX_POSTS_PER_RUN = 999 
DB_FILE = 'posted_ids.json'
# --- SERVICE ACCOUNT KEY IS READ FROM GITHUB SECRETS ---
SERVICE_ACCOUNT_KEY = os.environ.get('SERVICE_ACCOUNT_JSON') 

# --- BLOGGER AUTH FUNCTIONS (NOW USES SERVICE ACCOUNT) ---

def get_service():
    """Authenticates using the Service Account JSON key from GitHub Secrets."""
    # -----------------------------------------------------------
    # 1. AUTHENTICATE USING SERVICE ACCOUNT (Primary Method)
    # -----------------------------------------------------------
    if SERVICE_ACCOUNT_KEY:
        try:
            # Decode the Base64 key string back into a JSON object
            key_data = json.loads(base64.b64decode(SERVICE_ACCOUNT_KEY).decode('utf-8'))
            
            credentials = service_account.Credentials.from_service_account_info(
                key_data,
                scopes=['https://www.googleapis.com/auth/blogger']
            )
            print("Authentication method: Service Account.")
            return build('blogger', 'v3', credentials=credentials)
        except Exception as e:
            print(f"FATAL ERROR: Service Account setup failed. Check SERVICE_ACCOUNT_JSON secret format: {e}")
            return None
    
    # -----------------------------------------------------------
    # 2. FALLBACK TO LOCAL TOKEN (For initial local testing only)
    # -----------------------------------------------------------
    creds = None
    if os.path.exists('token.pickle'):
        try:
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
            return build('blogger', 'v3', credentials=creds)
        except Exception:
            pass # Ignore if token is bad, let it proceed to token generation
            
    # 3. FALLBACK TO LOCAL AUTHORIZATION (If token.pickle is missing or bad)
    CLIENT_SECRET_FILE = 'client_secret.json'
    if os.path.exists(CLIENT_SECRET_FILE):
        print("Falling back to local browser authorization...")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_FILE, ['https://www.googleapis.com/auth/blogger'])
            creds = flow.run_local_server(port=0)
            
            with open('token.pickle', 'wb') as token:
                pickle.dump(creds, token)
            print("Authorization successful. token.pickle created.")
            return build('blogger', 'v3', credentials=creds)
        except Exception as e:
            print(f"FATAL ERROR: Local browser authorization failed. Check client_secret.json permissions/name. Error: {e}")
            return None
            
    print("FATAL ERROR: No valid authentication method found (Service Account or local token).")
    return None

def create_post(service, title, content):
    """Posts the content to Blogger."""
    body = {
        'kind': 'blogger#post',
        'blog': {'id': BLOG_ID},
        'title': title,
        'content': content,
        'labels': ['AI-Edited', 'Finance']
    }
    try:
        # Implicitly accepts the blog invitation if the Service Account was just invited
        try:
            service.users().get(userId='self').execute()
        except Exception:
            pass 

        result = service.posts().insert(blogId=BLOG_ID, body=body).execute()
        print(f"Successfully posted: {result['url']}")
        return True
    except Exception as e:
        print(f"Error posting to Blogger: {e}")
        return False

# --- GEMINI AI FUNCTION ---

def rewrite_with_gemini(original_title, original_content, original_link, source_name, date=None):
    """
    Translates, rewrites, and formats content using Gemini.
    """
    try:
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    except KeyError:
        print("Warning: GEMINI_API_KEY is not set in environment. AI features will fail.")
        return None

    model = genai.GenerativeModel('gemini-2.0-flash') 
    
    # System instruction defines the AI's role and hard rules
    system_instruction = f"""
    You are a content translator for a simple-English blog focused on Nepali finance and stock markets. 
    Your task is to translate the provided Nepali text into simple, easy-to-read English, ensuring the tone is extremely human and achieves a 0% AI-generated feel.

    ***STRICT RULES:***
    1.  **DO NOT** translate or change Nepali financial terms like 'अर्ब', 'खर्ब', 'करोड', 'लाख'. Keep them exactly as they are in the final body.
    2.  **DO NOT** translate the Nepali date (if provided, e.g., '२०८२ मंसिर २ गते'). Keep the date as a separate line if possible, or omit if not applicable.
    3.  Create a clear and engaging English title.
    4.  Return ONLY the JSON structure. Do not include any text, notes, or markdown formatting outside the JSON object.
    """

    content_with_date = f"Nepali Date: {date}\nTitle: {original_title}\nContent: {original_content}" if date else f"Title: {original_title}\nContent: {original_content}"

    prompt = f"""
    Translate and rewrite the following Nepali content:
    ---
    {content_with_date}
    ---
    
    Please provide the rewritten content in the required JSON format: {{"title": "Rewritten English Title", "body": "Rewritten English Content goes here."}}
    """
    
    try:
        response = model.generate_content(
            prompt, 
            config=genai.types.GenerateContentConfig(
                system_instruction=system_instruction
            )
        )
        
        # Clean up response to ensure valid JSON (removes leading/trailing text like ```json)
        clean_json = response.text.strip().replace('```json', '').replace('```', '')
        ai_result = json.loads(clean_json)

        # Build the final post content, including the source citation
        final_source_citation = f"<br/><br/>Read full news at: <a href='{original_link}'>{source_name}</a>"
        final_date_tag = f"*Nepali Date: {date}*<br/>" if date else ""
        
        final_body = f"{ai_result['body']}<br/><br/>{final_date_tag}{final_source_citation}"
        
        return {"title": ai_result['title'], 'content': final_body}
        
    except Exception as e:
        print(f"Gemini Translation Error (Check API Key and response format): {e}")
        return None 


# --- DATA FETCHING FUNCTIONS ---

def fetch_json_news(posted_ids):
    """Filters news from Bajarkochirfar API for 'Share Market' and 'Economy' categories."""
    print("Fetching articles from JSON API...")
    try:
        response = requests.get(JSON_URL, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"Error fetching data from JSON API: {e}")
        return []

    if not data.get('success'):
        print("JSON API did not return a success response.")
        return []

    articles = []
    required_categories = ["सेयर बजार", "अर्थतन्त्र"]

    for item in data.get('data', []):
        unique_id = f"json_{item.get('id')}"
        if item.get('category_name') in required_categories and unique_id not in posted_ids:
            articles.append({
                'unique_id': unique_id,
                'title': item.get('title'),
                'content': item.get('content'),
                'link': item.get('original_news_link'),
                'date': item.get('date'),
                'source': 'bajarkochirfar.com'
            })
    return articles

def fetch_rss_news(posted_ids):
    """Fetches all posts from Nepsestock RSS Feed."""
    print("Fetching articles from RSS Feed...")
    articles = []
    
    try:
        feed = feedparser.parse(RSS_URL)
    except Exception as e:
        print(f"Error fetching data from RSS Feed: {e}")
        return []

    for entry in feed.entries:
        unique_id = f"rss_{entry.link}"
        if unique_id not in posted_ids:
            
            content = entry.summary if hasattr(entry, 'summary') else entry.content[0].value
            
            articles.append({
                'unique_id': unique_id,
                'title': entry.title,
                'content': content,
                'link': entry.link,
                'date': None,
                'source': 'nepsestock.com'
            })
    return articles

# --- DATABASE (POSTED ID) FUNCTIONS ---

def get_posted_ids():
    """Loads article IDs already posted from the JSON file."""
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_posted_id(post_id, current_ids):
    """Saves a new article ID to the JSON file."""
    current_ids.append(post_id)
    if len(current_ids) > 200:
        current_ids = current_ids[-200:]
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(current_ids, f, ensure_ascii=False, indent=2)

# --- MAIN EXECUTION ---

def run():
    print("--- Blogger Auto-Poster Starting ---")
    
    # 1. Initialize services and load IDs
    service = get_service()
    
    if service is None:
        print("FATAL ERROR: Cannot continue without Blogger Service authentication.")
        return

    posted_ids = get_posted_ids()
    
    # 2. Fetch data from both sources
    json_articles = fetch_json_news(posted_ids)
    rss_articles = fetch_rss_news(posted_ids)
    all_articles = json_articles + rss_articles
    
    print(f"Total new articles fetched: {len(all_articles)}")
    
    count = 0
    
    # 3. Process, post, and save
    for article in all_articles:
        if count >= MAX_POSTS_PER_RUN:
            print("Maximum processing limit reached.")
            break
            
        print(f"\nProcessing article: {article['title']}")
        
        # Call Gemini for translation and rewrite
        ai_result = rewrite_with_gemini(
            original_title=article['title'],
            original_content=article['content'],
            original_link=article['link'],
            source_name=article['source'],
            date=article['date']
        )
        
        if ai_result:
            success = create_post(service, ai_result['title'], ai_result['content'])
            
            if success:
                save_posted_id(article['unique_id'], posted_ids)
                count += 1
                sleep(10) # 10 seconds delay between posts to prevent rate limiting
        else:
            print(f"Skipped post due to Gemini error: {article['title']}")

    print(f"\n--- This run created {count} new posts. ---")

if __name__ == '__main__':
    run()