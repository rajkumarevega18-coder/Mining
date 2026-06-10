import os
import json
import gzip
import requests
import time
import shutil

API_ROOT = "http://139.84.134.18:8002"
# DATABASE = "frontiersin"
DATABASE = "sagepub"
BATCH_SIZE = 4000

def post_article_links_with_data(database, combined_data):
    url = f"{API_ROOT}/{database}/add/article_links_with_data"
    payload = gzip.compress(json.dumps(combined_data).encode("utf-8"))
    headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                print(f"✅ Uploaded {len(combined_data)} articles successfully.")
                return True
            else:
                print(f"❌ Attempt {attempt}: Status {response.status_code}, Error: {response.text}")
        except Exception as e:
            print(f"⚠️ Attempt {attempt} error: {e}")
        if attempt < MAX_RETRIES:
            wait = attempt * 30  # 30s, 60s between retries
            print(f"   Retrying in {wait}s...")
            time.sleep(wait)
    return False

# Publishers that have article-by-URL data in C:/{db}_article_data_offline_uploads/offline_uploads
# Add any new publisher (wiley, oup, sage, etc.) here to push their article_data batches.
ARTICLE_DATA_DATABASES = ["wiley", "oup"]

# Drive/path prefix for offline folders (same as upload_saved_batches2)
OFFLINE_DRIVE = "C:"

# Temp cleanup: system temp only (Windows %TEMP%, Python temp). Data files in offline_uploads are never touched here;
# they are deleted only after successful upload in upload_saved_batches / upload_article_data_by_url_batches.
TEMP_CLEANUP_AGE_HOURS = 1


def clear_system_temp_dirs():
    """Delete only system temp: Windows %TEMP%, %TMP%, Python temp dir. Items older than TEMP_CLEANUP_AGE_HOURS. Does not touch any data/upload folders."""
    import tempfile
    age_sec = TEMP_CLEANUP_AGE_HOURS * 3600
    now = time.time()
    seen = set()
    dirs = [
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        tempfile.gettempdir(),
    ]
    deleted_files, deleted_dirs = 0, 0
    for d in dirs:
        if not d or not os.path.isdir(d) or d in seen:
            continue
        seen.add(os.path.normpath(d))
        try:
            for name in os.listdir(d):
                path = os.path.join(d, name)
                try:
                    if os.path.getmtime(path) < now - age_sec:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                            deleted_dirs += 1
                        else:
                            os.remove(path)
                            deleted_files += 1
                except (OSError, PermissionError):
                    pass
        except (OSError, PermissionError):
            pass
    if deleted_files or deleted_dirs:
        print(f"🧹 System temp cleanup: removed {deleted_files} file(s), {deleted_dirs} folder(s) (older than {TEMP_CLEANUP_AGE_HOURS}h)")


CHUNK_SIZE   = 20        # files per batch (reduced to avoid overwhelming server with 14 systems)
WAIT_MINS    = 60      # minutes to wait when no files remain
REQUEST_TIMEOUT = 180  # seconds before giving up on a single POST
MAX_RETRIES  = 3       # retry failed uploads before moving on


def upload_saved_batches(database):
    folder_name = f"{database}_offline_uploads"
    folder = f"C:/{folder_name}/offline_uploads"

    if not os.path.exists(folder):
        print("📂 No offline folder found.")
        return

    files = sorted(os.listdir(folder))
    if not files:
        print(f"⏳ No files found. Waiting {WAIT_MINS} mins...")
        return

    print(f"📦 Found {len(files)} file(s) → uploading in chunks of {CHUNK_SIZE}")

    total_uploaded = 0
    total_failed   = 0

    for i in range(0, len(files), CHUNK_SIZE):
        chunk = files[i : i + CHUNK_SIZE]
        print(f"\n  ── Chunk {i // CHUNK_SIZE + 1} ({len(chunk)} files) ──")

        all_records = []
        for file in chunk:
            filepath = os.path.join(folder, file)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                print(f"  📥 Loaded: {file} ({len(data)} records)")
                all_records.extend(data)
            except Exception as e:
                print(f"  ⚠️ Could not read {file}: {e}")

        if not all_records:
            print("  ⚠️ No records in this chunk — skipping.")
            continue

        print(f"  ⬆ Uploading {len(all_records)} records...")
        if post_article_links_with_data(database, all_records):
            for file in chunk:
                filepath = os.path.join(folder, file)
                try:
                    os.remove(filepath)
                    print(f"  🧹 Deleted: {file}")
                except Exception as e:
                    print(f"  ⚠️ Could not delete {file}: {e}")
            total_uploaded += len(chunk)
        else:
            print("  ❌ Upload failed — files kept.")
            total_failed += len(chunk)

    print(f"\n✅ Done  uploaded_chunks={total_uploaded}  failed_chunks={total_failed}")


while True:
    clear_system_temp_dirs()
    upload_saved_batches(DATABASE)
    upload_saved_batches("rsc")
    print(f"⏰ Waiting {WAIT_MINS} mins before next check...")
    time.sleep(WAIT_MINS * 60)
    # python wiley.py check "https://advanced.onlinelibrary.wiley.com/doi/10.1002/jbmr.1234" jbmr
