import os
import sys
import argparse
import time
import urllib.error
import requests
from quip import QuipClient
import hashlib
import json
import re
import mimetypes

def get_domain_from_link(url):
    """Extract domain from a Quip URL"""
    if not url:
        return None
    if '://' in url:
        url = url.split('://', 1)[1]
    return url.split('/', 1)[0]

def extract_folder_id_from_url(url):
    """Extract folder ID from a Quip URL"""
    parts = url.strip('/').split('/')
    for part in parts:
        if part and not part.startswith('http') and not part.endswith('.com'):
            return part
    return None

def load_cache(cache_file):
    """Load the sync cache from file"""
    try:
        with open(cache_file, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_cache(cache_file, cache):
    """Save the sync cache to file"""
    with open(cache_file, 'w') as f:
        json.dump(cache, f)

def get_file_hash(file_path):
    """Calculate MD5 hash of file content"""
    try:
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return None

def retry_api_call(func, *args, max_retries=3, rate_limit=0.5, **kwargs):
    """Retry API calls with exponential backoff and rate limiting"""
    retries = 0
    while retries < max_retries:
        try:
            result = func(*args, **kwargs)
            # Add rate limiting delay after successful call
            time.sleep(rate_limit)
            return result
        except urllib.error.HTTPError as e:
            if e.code == 504:  # Gateway Timeout
                wait_time = 2 ** retries
                retries += 1
                if retries < max_retries:
                    print(f"Gateway timeout, retrying in {wait_time} seconds... (Attempt {retries}/{max_retries})")
                    time.sleep(wait_time)
                else:
                    print(f"Failed after {max_retries} attempts: {e}")
                    raise
            else:
                print(f"HTTP Error: {e}")
                raise
        except Exception as e:
            print(f"Error: {e}")
            raise

def create_folder_structure(client, folder_path, parent_id):
    """Create folder structure in Quip"""
    folders = folder_path.split(os.sep)
    current_folder_id = parent_id

    for folder in folders:
        if not folder:
            continue
        
        # Check if folder exists at current level
        folder_list = retry_api_call(client.get_folder, current_folder_id)
        folder_found = False
        
        for child in folder_list['children']:
            if 'folder_id' in child:
                # Get folder details to check the title
                folder_data = retry_api_call(client.get_folder, child['folder_id'])
                folder_title = folder_data.get('folder', {}).get('title', '')
                if folder_title == folder:
                    current_folder_id = child['folder_id']
                    folder_found = True
                    break
        
        if not folder_found:
            new_folder = retry_api_call(client.new_folder, title=folder, parent_id=current_folder_id)
            print(f"Created folder: {folder}")
            current_folder_id = new_folder['folder']['id']

    return current_folder_id

def upload_image_to_quip(client, image_path, thread_id=None):
    """Upload an image to Quip and return the blob ID"""
    if not os.path.exists(image_path):
        print(f"Error: Image file not found: {image_path}")
        return None
    
    if not thread_id:
        print("Error: No thread ID provided for image upload")
        return None
    
    try:
        # Get image filename
        image_name = os.path.basename(image_path)
        
        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(image_path)
        if not mime_type:
            # Default to JPEG if can't determine
            mime_type = 'image/jpeg'
        
        # Open image file
        with open(image_path, 'rb') as f:
            # Upload blob to the thread
            print(f"Uploading image: {image_path} to thread: {thread_id}")
            response = retry_api_call(
                client.put_blob,
                thread_id,
                f,
                name=image_name
            )
            
            if 'id' in response:
                print(f"Image uploaded successfully, blob ID: {response['id']}")
                print(f"Image URL: {response['url']}")
                return response['id']
            else:
                print(f"Error uploading image: {response}")
                return None
    except Exception as e:
        print(f"Error uploading image {image_path}: {e}")
        return None

def preprocess_markdown_for_images(content, base_dir):
    """Preprocess markdown content to replace image references with a special pattern
    
    This function:
    1. Finds all markdown image references: ![alt_text](image_path)
    2. Replaces them with: ####image path: (image_path)
    3. Returns the modified content
    """
    # Find all markdown image references
    image_pattern = r'!\[(.*?)\]\((.*?)\)'
    
    def replace_image(match):
        image_path = match.group(2)
        # Create the special pattern
        return f"####image path: ({image_path})"
    
    # Replace all image references
    processed_content = re.sub(image_pattern, replace_image, content)
    return processed_content

def process_images_after_upload(client, thread_id, base_dir):
    """Process images in a Quip document after uploading markdown
    
    This function:
    1. Gets the HTML content of the document
    2. Finds special image patterns in the HTML
    3. Uploads each image to Quip as a blob
    4. Updates the document with the image blobs
    """
    if not thread_id:
        print("Error: No thread ID provided for image processing")
        return False
    
    try:
        # Get the document HTML
        thread_data = retry_api_call(client.get_thread, thread_id)
        html = thread_data.get('html', '')
        
        if not html:
            print("Warning: Document has no HTML content")
            return True
        
        # Find our special image pattern in the HTML
        pattern = r'image path:\s*\(([^)]+)\)'
        image_matches = re.findall(pattern, html)
        
        if not image_matches:
            print("No image patterns found in document")
            return True
        
        print(f"Found {len(image_matches)} image patterns in document")
        
        # Process each image
        processed_images = 0
        
        for image_path in image_matches:
            # Handle relative paths
            if not os.path.isabs(image_path):
                full_image_path = os.path.join(base_dir, image_path)
            else:
                full_image_path = image_path
                
            if not os.path.exists(full_image_path):
                print(f"Warning: Image file not found: {full_image_path}")
                continue
                
            # Upload the image to Quip
            blob_id = upload_image_to_quip(client, full_image_path, thread_id)
            if not blob_id:
                print(f"Warning: Failed to upload image: {full_image_path}")
                continue

            content = f"<img src=/blob/{thread_id}/{blob_id}>"
            try: 
                retry_api_call(
                    client.edit_document,
                    thread_id=thread_id,
                    content=content,
                    document_range=f"image path: ({image_path})",
                    location=7
                )
            except Exception as e:
                print(f"Error adding the image {image_path}: {e}")
            processed_images += 1
            print(f"Added image: {image_path} with blob: {blob_id}")
        
        return True
    except Exception as e:
        print(f"Error processing images after upload: {e}")
        return False

def sync_file(client, file_path, quip_folder_id, cache):
    """Sync a single file to Quip"""
    ##########################################
    ##### Check whether need to update #######
    ##########################################
        
    # Check if we need to update the document
    need_update = True

    file_hash = get_file_hash(file_path)
    if file_hash is None:
        return cache
    
    # Get cached info (might be hash string or dict with hash and doc_id)
    cached_info = cache.get(file_path, {})
    if isinstance(cached_info, str):  # Handle old cache format
        cached_hash = cached_info
        cached_doc_id = None
    else:
        cached_hash = cached_info.get('hash')
        cached_doc_id = cached_info.get('doc_id')
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Preprocess markdown content to replace image references with special pattern
        base_dir = os.path.dirname(file_path)
        content = preprocess_markdown_for_images(content, base_dir)
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return cache

    file_name = os.path.basename(file_path)
    name_without_ext = os.path.splitext(file_name)[0]

    # First check if we have a cached document ID and verify it still exists
    doc_id = cached_doc_id
    if doc_id:
        try:
            # Verify the document still exists
            thread_data = retry_api_call(client.get_thread, doc_id)
            if 'error' in thread_data or not thread_data.get('thread'):
                print(f"Cached document ID {doc_id} no longer exists, searching for document...")
                doc_id = None
        except Exception as e:
            print(f"Error accessing cached document ID {doc_id}: {e}")
            doc_id = None
    
    # If no cached ID or it's invalid, search in the folder (TODO: might be redundant due to feature update)
    if not doc_id:
        folder_content = retry_api_call(client.get_folder, quip_folder_id)
        for child in folder_content['children']:
            if 'thread_id' in child:
                try:
                    thread_data = retry_api_call(client.get_thread, child['thread_id'])
                    thread_title = thread_data.get('thread', {}).get('title', '')
                    if thread_title == name_without_ext:
                        doc_id = child['thread_id']
                        break
                except Exception:
                    # Skip this thread if we can't access it
                    continue
    
    # Get sync status from cache
    sync_success = cached_info.get('sync_success', False)
    
    if file_hash == cached_hash and doc_id and sync_success:
        # File hasn't changed locally and last sync was successful, so no need to update
        print(f"File {file_path} unchanged and previously synced successfully, skipping...")
        need_update = False
    elif file_hash == cached_hash and doc_id and not sync_success:
        # File hasn't changed but last sync failed, so we need to try again
        print(f"File {file_path} unchanged but previous sync failed, retrying...")
        need_update = True
    
    ##########################################
    ######## Begin to sync the file. #########
    ##########################################
    sync_success = False
    if need_update:
        if doc_id:
            print(f"Updating existing document: {name_without_ext}")
            try:
                # First check if the document has any content
                existing_thread = retry_api_call(client.get_thread, doc_id)
                html = existing_thread.get('html', '')
                
                if html and '<h1' in html:
                    # Document has content with headings, use document_range to delete all content
                    # Find the first heading
                    first_heading_match = re.search(r'<h1[^>]*>(.*?)</h1>', html)
                    
                    if first_heading_match:
                        first_heading = first_heading_match.group(1)
                        print(f"Deleting all content under heading: {first_heading}")
                        
                        # Delete all content using document_range
                        try:
                            retry_api_call(
                                client.edit_document,
                                thread_id=doc_id,
                                document_range=first_heading,
                                content="",  # Empty content to delete
                                location=9  # DELETE_DOCUMENT_RANGE
                            )
                        except Exception as e:
                            print(f"Error deleting document range: {e}")
                
                # Now add the new content (either the document was emptied or had no headings)
                retry_api_call(
                    client.edit_document,
                    thread_id=doc_id,
                    content=content,
                    format='markdown',
                    location=1  # PREPEND (add to beginning of now-empty document)
                )
                
                # Process images after uploading the markdown content
                base_dir = os.path.dirname(file_path)
                sync_success=True
                if process_images_after_upload(client, doc_id, base_dir):
                    sync_success = True
                else:
                    print("Warning: Image processing failed, but document was updated")
                    sync_success = False  # Still consider it a success since the document was updated
                print(f"#Successfully Sync existing document: {name_without_ext}")
            except Exception as e:
                print(f"Error updating document: {e}")
                # Don't return here, update cache with sync_success=False
        else:
            print(f"Creating new document: {name_without_ext}")
            try:
                # Create the document with the markdown content
                result = retry_api_call(
                    client.new_document,
                    content=content,
                    format='markdown',
                    title=name_without_ext,
                    member_ids=[quip_folder_id]
                )
                doc_id = result.get('thread', {}).get('id')
                
                if doc_id:
                    # Process images after uploading the markdown content
                    base_dir = os.path.dirname(file_path)
                    if process_images_after_upload(client, doc_id, base_dir):
                        sync_success = True
                    else:
                        print("Warning: Image processing failed, but document was created")
                        sync_success = False  # Still consider it a success since the document was created
                    print(f"#Successfully Sync existing document: {name_without_ext}")
                else:
                    print(f"Error: Failed to get document ID for new document")
            except Exception as e:
                print(f"Error creating document: {e}")
    else:
        # If no update needed, consider it a successful sync
        sync_success = True

    # Store file hash, document ID, and sync status in cache
    cache[file_path] = {
        'hash': file_hash,
        'doc_id': doc_id,
        'last_sync': time.time(),
        'sync_success': sync_success
    }
    return cache

def detect_deleted_files(local_path, cache):
    """Detect files that exist in cache but not in local filesystem"""
    deleted_files = []
    for file_path in list(cache.keys()):
        if not os.path.exists(file_path):
            deleted_files.append(file_path)
    return deleted_files

def delete_quip_document(client, file_path, cache):
    """Delete a Quip document for a deleted markdown file"""
    cached_info = cache.get(file_path, {})
    if isinstance(cached_info, str):  # Handle old cache format
        return cache  # Can't delete if we don't have the doc_id
    
    doc_id = cached_info.get('doc_id')
    if not doc_id:
        return cache
    
    try:
        print(f"Deleting document for removed file: {file_path}")
        retry_api_call(client.delete_thread, thread_id=doc_id)
        del cache[file_path]
    except Exception as e:
        print(f"Error deleting document: {e}")
    
    return cache

def clear_quip_folder(client, folder_id):
    """Clear all documents and subfolders from a Quip folder"""
    try:
        folder_content = retry_api_call(client.get_folder, folder_id)
        for child in folder_content.get('children', []):
            if 'thread_id' in child:
                thread_id = child['thread_id']
                # Get thread details to show the title
                thread_data = retry_api_call(client.get_thread, thread_id)
                thread_title = thread_data.get('thread', {}).get('title', 'Unknown_Document')
                print(f"Deleting document: {thread_title}")
                retry_api_call(client.delete_thread, thread_id=thread_id)
            elif 'folder_id' in child:
                subfolder_id = child['folder_id']
                # Get folder details to show the title
                subfolder_data = retry_api_call(client.get_folder, subfolder_id)
                subfolder_title = subfolder_data.get('folder', {}).get('title', 'Unknown_Folder')
                print(f"Clearing subfolder: {subfolder_title}")
                clear_quip_folder(client, subfolder_id) # TODO: Currently, Quip doesn't provide API to remove a folder
        return True
    except Exception as e:
        print(f"Error clearing folder: {e}")
        return False

def sync_directory(client, local_path, root_folder_id, cache_file, clean_sync=False):
    """Sync entire directory structure to Quip"""
    cache = load_cache(cache_file)
    
    # If clean sync is requested, clear the Quip folder first
    if clean_sync:
        print(f"Performing clean sync - clearing Quip folder {root_folder_id}...")
        if clear_quip_folder(client, root_folder_id):
            # Reset cache since all documents are now gone
            cache = {}
            print("Quip folder cleared successfully")
        else:
            print("Failed to clear Quip folder completely")
    
    # Handle deleted files (using cache to detect them)
    if not clean_sync:
        deleted_files = detect_deleted_files(local_path, cache)
        for file_path in deleted_files:
            cache = delete_quip_document(client, file_path, cache)
    
    # First collect all markdown files to sync
    markdown_files = []
    for root, dirs, files in os.walk(local_path):
        for file in files:
            if file.endswith('.md'):
                file_path = os.path.join(root, file)
                markdown_files.append(file_path)
    
    # Group files by directory to minimize folder creation
    files_by_dir = {}
    for file_path in markdown_files:
        dir_path = os.path.dirname(file_path)
        if dir_path not in files_by_dir:
            files_by_dir[dir_path] = []
        files_by_dir[dir_path].append(file_path)
    
    # Process directories in sorted order for more predictable behavior
    for dir_path in sorted(files_by_dir.keys()):
        rel_path = os.path.relpath(dir_path, local_path)
        
        # Get or create the Quip folder
        if rel_path == '.':
            current_quip_folder = root_folder_id
        else:
            current_quip_folder = create_folder_structure(client, rel_path, root_folder_id)
        
        # Sync all files in this directory
        for file_path in files_by_dir[dir_path]:
            print(f"Syncing {file_path}...")
            cache = sync_file(client, file_path, current_quip_folder, cache)

    save_cache(cache_file, cache)

def main():
    parser = argparse.ArgumentParser(description='Sync markdown files to Quip')
    parser.add_argument('local_path', help='Path to local folder containing markdown files')
    parser.add_argument('quip_url', help='URL of the Quip folder')
    parser.add_argument('--clean', action='store_true', help='Clear Quip folder before syncing')

    
    args = parser.parse_args()
    
    folder_id = extract_folder_id_from_url(args.quip_url)
    print("**************************************")
    print(f"Extracted folder ID: {folder_id}")
    if not folder_id:
        print("Error: Could not extract folder ID from the provided Quip URL")
        sys.exit(1)
    
    access_token = os.environ.get("QUIP_API_TOKEN")
    if not access_token:
        # Prompt for access token if not found in environment
        access_token = input("Enter your Amazon Quip access token (from https://quip-amazon.com/dev/token): ").strip()
        if not access_token:
            print("Error: No access token provided")
            sys.exit(1)
    
    domain = get_domain_from_link(args.quip_url)
    base_url = f"https://platform.{domain}" if domain else "https://platform.quip-amazon.com"
    
    print(f"Syncing {args.local_path} to Quip folder ID: {folder_id}")
    print(f"Using API URL: {base_url}")
    print("**************************************")
    
    client = QuipClient(access_token=access_token, base_url=base_url)
    cache_file = os.path.join(args.local_path, ".quip_sync_cache.json")
    
    sync_directory(client, args.local_path, folder_id, cache_file, args.clean)

if __name__ == "__main__":
    main()