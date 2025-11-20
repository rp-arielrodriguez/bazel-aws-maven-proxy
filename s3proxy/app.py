import os
import time
import logging
import threading
import mimetypes
from pathlib import Path
from functools import wraps
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from flask import Flask, send_file, abort, request, Response, jsonify

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('s3-proxy')

# Flask application
app = Flask(__name__)

# Configuration from environment variables
S3_BUCKET_NAME = os.environ.get('S3_BUCKET_NAME')
AWS_PROFILE = os.environ.get('AWS_PROFILE', 'default')
AWS_REGION = os.environ.get('AWS_REGION', 'us-west-2')
CACHE_DIR = os.environ.get('CACHE_DIR', '/data')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'info').upper()
REFRESH_INTERVAL = int(os.environ.get('REFRESH_INTERVAL', '300'))  # 5 minutes by default

# Set log level based on environment variable
logger.setLevel(getattr(logging, LOG_LEVEL))

# Credentials lock to prevent simultaneous refresh operations
credentials_lock = threading.Lock()
# Last credentials refresh time
last_credentials_check = 0
# S3 client (will be initialized later)
s3_client = None

def create_cache_dir_if_not_exists():
    """Ensure the cache directory exists."""
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    # Create health check endpoint
    health_dir = Path(CACHE_DIR) / 'healthz'
    health_dir.mkdir(parents=True, exist_ok=True)
    with open(health_dir / 'index.html', 'w') as f:
        f.write('OK')

def get_s3_client():
    """
    Get a boto3 S3 client with current credentials.
    Uses AWS_PROFILE for authentication without attempting token refresh.
    """
    global s3_client
    
    # Initialize or refresh the client only if needed
    current_time = time.time()
    global last_credentials_check
    
    if s3_client is None or (current_time - last_credentials_check) > REFRESH_INTERVAL:
        with credentials_lock:
            try:
                # Create a session from profile
                session = boto3.Session(profile_name=AWS_PROFILE)
                
                # Extract current credentials without triggering refresh
                creds = session.get_credentials()
                if creds is None:
                    raise Exception("No credentials found for profile")
                
                # Explicitly create a client with the extracted credentials
                # This bypasses boto3's token refresh mechanism
                s3_client = boto3.client(
                    's3',
                    region_name=AWS_REGION,
                    aws_access_key_id=creds.access_key,
                    aws_secret_access_key=creds.secret_key,
                    aws_session_token=creds.token
                )
                
                # Test the client with a simple operation
                s3_client.list_buckets()
                
                last_credentials_check = current_time
                logger.info("Successfully initialized S3 client with current credentials")
            except Exception as e:
                logger.error(f"Error initializing S3 client: {str(e)}")
                if s3_client is None:
                    raise
    
    return s3_client

def with_s3_client(f):
    """Decorator to provide a function with a refreshed S3 client."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            # Get (potentially refreshed) S3 client
            client = get_s3_client()
            # Pass the client to the function
            return f(client, *args, **kwargs)
        except (ClientError, NoCredentialsError) as e:
            logger.error(f"S3 access error: {str(e)}")
            # Return an appropriate error response
            return jsonify(error=str(e)), 500
    return decorated_function

def get_cached_file_path(path):
    """Convert a request path to a local file path in the cache."""
    # Remove leading slash if present
    if path.startswith('/'):
        path = path[1:]
    
    # Combine with cache directory
    return os.path.join(CACHE_DIR, path)

def ensure_parent_dir_exists(file_path):
    """Ensure the parent directory of a file exists."""
    parent_dir = os.path.dirname(file_path)
    Path(parent_dir).mkdir(parents=True, exist_ok=True)

def fetch_from_s3(s3_client, path):
    """
    Fetch a file from S3 and store it in the local cache.
    Returns the local file path if successful, None otherwise.
    """
    # Remove leading slash if present
    s3_key = path
    if s3_key.startswith('/'):
        s3_key = s3_key[1:]
    
    local_path = get_cached_file_path(path)
    
    # Ensure the parent directory exists
    ensure_parent_dir_exists(local_path)
    
    try:
        logger.info(f"Fetching from S3: {S3_BUCKET_NAME}/{s3_key}")
        s3_client.download_file(S3_BUCKET_NAME, s3_key, local_path)
        logger.info(f"Successfully cached: {local_path}")
        return local_path
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            logger.warning(f"File not found in S3: {S3_BUCKET_NAME}/{s3_key}")
        else:
            logger.error(f"Error fetching from S3: {str(e)}")
        return None

@app.route('/healthz')
def health_check():
    """Health check endpoint."""
    # If we get here, the server is running
    # We also check if we can connect to S3
    try:
        get_s3_client()
        return "OK", 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return str(e), 500

@app.route('/<path:file_path>')
@app.route('/', defaults={'file_path': ''})
@with_s3_client
def get_file(s3_client, file_path):
    """
    Main handler for file requests.
    Tries to serve from cache, fetches from S3 if not found.
    """
    # For empty paths, return a directory listing
    if not file_path:
        return directory_listing(s3_client, file_path)
    
    # Check if file exists in cache
    local_path = get_cached_file_path(file_path)
    
    if os.path.isdir(local_path):
        # If it's a directory, return a listing
        return directory_listing(s3_client, file_path)
    
    if not os.path.exists(local_path):
        logger.info(f"Cache miss: {file_path}")
        # Not in cache, try to fetch from S3
        local_path = fetch_from_s3(s3_client, file_path)
        
        if not local_path:
            # File not found in S3
            abort(404)
    else:
        logger.debug(f"Cache hit: {file_path}")
    
    # Guess mime type
    mimetype = mimetypes.guess_type(local_path)[0]
    
    # Serve the file from the cache
    return send_file(local_path, mimetype=mimetype)

def directory_listing(s3_client, prefix):
    """Generate a directory listing for a given prefix."""
    # Normalize prefix for S3
    if prefix:
        s3_prefix = prefix
        if not s3_prefix.endswith('/'):
            s3_prefix += '/'
    else:
        s3_prefix = ''
    
    if s3_prefix.startswith('/'):
        s3_prefix = s3_prefix[1:]
    
    # Check local cache first
    local_dir = get_cached_file_path(prefix)
    local_entries = []
    
    if os.path.isdir(local_dir):
        # Get entries from local cache
        for entry in os.listdir(local_dir):
            full_path = os.path.join(local_dir, entry)
            entry_type = 'directory' if os.path.isdir(full_path) else 'file'
            size = os.path.getsize(full_path) if entry_type == 'file' else 0
            modified = datetime.fromtimestamp(os.path.getmtime(full_path))
            local_entries.append({
                'name': entry,
                'type': entry_type,
                'size': size,
                'modified': modified,
                'source': 'cache'
            })
    
    # Also check S3 for additional entries
    s3_entries = []
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=s3_prefix, Delimiter='/'):
            # Common prefixes represent "directories"
            if 'CommonPrefixes' in page:
                for prefix_obj in page['CommonPrefixes']:
                    dir_name = prefix_obj['Prefix'][len(s3_prefix):]
                    if dir_name.endswith('/'):
                        dir_name = dir_name[:-1]
                    s3_entries.append({
                        'name': dir_name,
                        'type': 'directory',
                        'size': 0,
                        'modified': None,
                        'source': 's3'
                    })
            
            # Contents represent files
            if 'Contents' in page:
                for obj in page['Contents']:
                    # Skip if this is the directory itself
                    if obj['Key'] == s3_prefix:
                        continue
                    
                    # Skip entries that represent "directories"
                    if obj['Key'].endswith('/'):
                        continue
                    
                    # Get just the filename part
                    file_name = obj['Key'][len(s3_prefix):]
                    
                    # Skip if file name contains a slash (it's in a subdirectory)
                    if '/' in file_name:
                        continue
                    
                    s3_entries.append({
                        'name': file_name,
                        'type': 'file',
                        'size': obj['Size'],
                        'modified': obj['LastModified'],
                        'source': 's3'
                    })
    except ClientError as e:
        logger.error(f"Error listing S3 objects: {str(e)}")
    
    # Merge entries, preferring local cache versions
    all_entries = {}
    for entry in local_entries + s3_entries:
        # Only include an entry once, preferring local cache
        if entry['name'] not in all_entries or entry['source'] == 'cache':
            all_entries[entry['name']] = entry
    
    # Sort entries by name
    sorted_entries = sorted(all_entries.values(), key=lambda e: e['name'])
    
    # Generate HTML for directory listing
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Maven Repository: {prefix}</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            h1 {{ margin-bottom: 20px; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #ddd; }}
            th {{ background-color: #f2f2f2; }}
            tr:hover {{ background-color: #f5f5f5; }}
            .directory {{ font-weight: bold; }}
            .size {{ text-align: right; }}
            .date {{ width: 200px; }}
            .source {{ width: 80px; }}
        </style>
    </head>
    <body>
        <h1>Maven Repository: {prefix}</h1>
        <table>
            <tr>
                <th>Name</th>
                <th class="size">Size</th>
                <th class="date">Last Modified</th>
                <th class="source">Source</th>
            </tr>
            {entries}
        </table>
    </body>
    </html>
    """
    
    # Generate table rows
    entry_rows = []
    
    # Add parent directory link if not at root
    if prefix:
        parent = os.path.dirname(prefix.rstrip('/'))
        parent_url = f"/{parent}" if parent else "/"
        entry_rows.append(f"""
            <tr>
                <td class="directory"><a href="{parent_url}">..</a></td>
                <td class="size">-</td>
                <td class="date">-</td>
                <td class="source">-</td>
            </tr>
        """)
    
    # Add all other entries
    for entry in sorted_entries:
        name = entry['name']
        entry_url = f"{request.path}/{name}" if request.path.endswith('/') else f"{request.path}/{name}"
        entry_url = entry_url.replace('//', '/')
        
        # Format size and date
        size = f"{entry['size']:,} bytes" if entry['type'] == 'file' else "-"
        modified = entry['modified'].strftime('%Y-%m-%d %H:%M:%S') if entry['modified'] else "-"
        
        # Apply directory styling if needed
        name_class = "directory" if entry['type'] == 'directory' else ""
        name_display = f"{name}/" if entry['type'] == 'directory' else name
        
        entry_rows.append(f"""
            <tr>
                <td class="{name_class}"><a href="{entry_url}">{name_display}</a></td>
                <td class="size">{size}</td>
                <td class="date">{modified}</td>
                <td class="source">{entry['source']}</td>
            </tr>
        """)
    
    # Complete the HTML
    full_html = html.format(
        prefix=f"/{prefix}" if prefix else "/",
        entries="\n".join(entry_rows)
    )
    
    return Response(full_html, mimetype='text/html')

def mirror_popular_artifacts():
    """
    Background task to periodically mirror popular artifacts from S3.
    This ensures that commonly accessed artifacts are already cached
    before they are requested.
    """
    logger.info("Starting background mirroring task")
    
    while True:
        try:
            # Get the current S3 client
            s3 = get_s3_client()
            
            # TODO: Implement logic to determine which artifacts are "popular"
            # and should be pre-cached. For now, this is just a placeholder.
            # This could be based on access logs, a predefined list, etc.
            
            # Sleep for the refresh interval
            time.sleep(REFRESH_INTERVAL)
        except Exception as e:
            logger.error(f"Error in background mirroring task: {str(e)}")
            # Sleep for a shorter time on error
            time.sleep(60)

def start_background_tasks():
    """Start background tasks in separate threads."""
    # Start the mirroring task
    mirror_thread = threading.Thread(target=mirror_popular_artifacts, daemon=True)
    mirror_thread.start()

if __name__ == '__main__':
    # Ensure cache directory exists
    create_cache_dir_if_not_exists()
    
    # Start background tasks
    start_background_tasks()
    
    # Log startup
    logger.info(f"Starting S3 proxy for bucket: {S3_BUCKET_NAME}")
    logger.info(f"Using AWS profile: {AWS_PROFILE}")
    logger.info(f"Using AWS region: {AWS_REGION}")
    logger.info(f"Cache directory: {CACHE_DIR}")
    
    # Run the Flask app
    app.run(host='0.0.0.0', port=int(os.environ.get('PROXY_PORT', 9000)))
