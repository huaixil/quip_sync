import os
import sys
import argparse
import time
import urllib.error
import requests
from quip import QuipClient
import hashlib
import json

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




def sync_file(client, file_path, quip_folder_id, cache):
    """Sync a single file to Quip"""
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
    
    # If no cached ID or it's invalid, search in the folder
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
    
    # Check if we need to update the document
    need_update = True
    if file_hash == cached_hash and doc_id:
        # File hasn't changed locally, so no need to update
        print(f"File {file_path} unchanged, skipping...")
        need_update = False
    
    if need_update:
        if doc_id:
            print(f"Updating existing document: {name_without_ext}")
            # First get the existing document to check if we need to preserve comments
            try:
                existing_thread = retry_api_call(client.get_thread, doc_id)
                # First delete all content by finding all section IDs
                html = existing_thread.get('html', '')
                import re
                section_ids = re.findall(r'id=[\'"]([^\'"]+)[\'"]', html)
                    
                # If we found sections, delete them one by one
                if section_ids:
                    print(f"Deleting {len(section_ids)} sections from document")
                    # Delete each section one by one
                    for section_id in section_ids:
                        try:
                            retry_api_call(
                                client.edit_document,
                                thread_id=doc_id,
                                section_id=section_id,
                                content=" ",  # Empty content to delete the section
                                location=5  # DELETE_SECTION
                            )
                        except Exception as e:
                            # Continue with other sections if one fails
                            print(f"Error deleting section {section_id}: {e}")
                            continue
                    
                # Now add the new content
                retry_api_call(
                        client.edit_document,
                        thread_id=doc_id,
                        content=content,
                        format='markdown',
                        location=1  # PREPEND (add to beginning of now-empty document)
                    )
            except Exception as e:
                print(f"Error updating document: {e}")
                return cache
        else:
            print(f"Creating new document: {name_without_ext}")
            result = retry_api_call(
                client.new_document,
                content=content,
                format='markdown',
                title=name_without_ext,
                member_ids=[quip_folder_id]
            )
            doc_id = result.get('thread', {}).get('id')
            if not doc_id:
                print(f"Error: Failed to get document ID for new document")
                return cache

    # Store file hash and document ID in cache
    cache[file_path] = {
        'hash': file_hash,
        'doc_id': doc_id
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
                clear_quip_folder(client, subfolder_id) # Currently, Quip doesn't provide API to remove a folder
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