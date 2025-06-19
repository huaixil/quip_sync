# Quip Markdown Sync

A tool to synchronize local markdown files with Quip documents.

## Setup

1. Install the required dependencies:
   ```
   pip install quip-api requests
   ```

2. Set up your Quip access token (one of the following methods):
   - Set the `QUIP_API_TOKEN` environment variable
   - Enter the token when prompted during script execution

## Usage

### Basic Usage

```
python quip_sync.py <local_folder_path> <quip_folder_url>
```

Example:
```
python quip_sync.py ~/Documents/notes https://quip-amazon.com/abc123/MyNotes
```

### Clean Sync Mode

To completely clear the Quip folder and sync it with all markdown files in the repository:

```
python quip_sync.py <local_folder_path> <quip_folder_url> --clean
```

This will:
- Delete all documents and subfolders in the Quip folder
- Create a fresh sync with all markdown files in the local folder
- Reset the cache file

## Features

- Syncs markdown files to Quip documents
- Maintains folder structure
- Only updates files that have changed (using file hash comparison)
- Creates new documents for new files
- Updates existing documents when content changes
- Deletes Quip documents when corresponding markdown files are deleted
- Clean sync mode to ensure Quip folder exactly matches local repository
- Automatically detects the appropriate API URL based on the domain in the Quip folder URL
- Properly handles document updates by completely replacing content
- Robust error handling with automatic retries for API timeouts
- Rate limiting to prevent API throttling

## Cache

The script maintains a cache file (`.quip_sync_cache.json`) in your local folder to track:
- Which files have been synced
- Their content hashes (to detect changes)
- Document IDs in Quip (to prevent duplicates and track deletions)

The cache allows the script to:
1. Skip unchanged files for faster syncing
2. Update the correct documents when files change
3. Delete Quip documents when local files are deleted

## Implementation Details

- Uses MD5 hashing to detect file changes
- Compares only local file hashes to determine if updates are needed
- Handles API rate limits with configurable delays
- Implements exponential backoff for handling timeouts
- Properly deletes all sections before adding new content to ensure clean updates