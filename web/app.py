from flask import Flask, request, render_template, flash, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
import os
import webbrowser
import requests
import json
import tempfile
import base64
from io import BytesIO

from PIL import Image
import imagehash

app = Flask(__name__)
app.secret_key = "darkeye-investigation-supersecretkey"

# Application and directory paths (use absolute paths relative to this app)
APP_ROOT = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(APP_ROOT, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

STATIC_ROOT = os.path.join(APP_ROOT, "static")
DATASET_DIR = os.path.join(STATIC_ROOT, "dataset")
os.makedirs(DATASET_DIR, exist_ok=True)
INDEX_PATH = os.path.join(APP_ROOT, "celebrity_db", "image_index.json")

# Allowed image extensions
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------- Local dataset search helpers ----------

def _iter_dataset_images():
    for root, _, files in os.walk(DATASET_DIR):
        for fname in files:
            ext = fname.rsplit(".", 1)[-1].lower()
            if ext in ALLOWED_EXTENSIONS:
                abs_path = os.path.join(root, fname)
                # rel path under static/ for url_for("static")
                rel_under_static = os.path.relpath(abs_path, start=STATIC_ROOT).replace("\\", "/")
                yield abs_path, rel_under_static


def _dataset_image_count():
    return sum(1 for _ in _iter_dataset_images())


def build_image_index():
    """Scan dataset dir and build a composite perceptual-hash index (pHash + dHash).
    Returns: count of indexed images.
    Writes to INDEX_PATH as json.
    """
    images = []
    count = 0
    for abs_path, rel in _iter_dataset_images():
        try:
            with Image.open(abs_path) as im:
                im = im.convert("RGB")
                ph = imagehash.phash(im)
                dh = imagehash.dhash(im)
                images.append({
                    "rel_path": rel,  # e.g., 'dataset/foo.jpg'
                    "phash": str(ph),  # 16-char hex string, 64-bit hash
                    "dhash": str(dh),  # gradient/edge difference hash
                })
                count += 1
        except Exception as e:
            print(f"Indexing error for {abs_path}: {e}")
            continue

    index = {"version": 2, "images": images}
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return count


def load_image_index():
    # Auto-build if missing and dataset has images
    if not os.path.exists(INDEX_PATH):
        if _dataset_image_count() > 0:
            try:
                build_image_index()
            except Exception as e:
                print("Auto index build failed:", e)
        else:
            return {"version": 2, "images": []}
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "images" not in data:
                return {"version": 2, "images": []}
            return data
    except Exception as e:
        print("Failed to load index:", e)
        return {"version": 2, "images": []}


def search_local_dataset(pil_image, max_distance=12, top_k=24):
    """Search local dataset using composite perceptual hash distance (pHash + dHash).
    STRICT FILTER: Any candidate with distance > max_distance is REJECTED.
    Returns: tuple (results_list, query_hash_hex)
    """
    try:
        pil_image = pil_image.convert("RGB")
    except Exception:
        pass
    query_phash = imagehash.phash(pil_image)
    query_dhash = imagehash.dhash(pil_image)
    index = load_image_index()
    
    # If index empty but dataset has images, try rebuilding once
    if not index.get("images") and _dataset_image_count() > 0:
        try:
            build_image_index()
            index = load_image_index()
        except Exception as e:
            print("Rebuild on empty index failed:", e)

    results = []
    for item in index.get("images", []):
        try:
            db_phash = imagehash.hex_to_hash(item.get("phash", ""))
            dist_p = int(query_phash - db_phash)  # pHash Hamming distance
            
            # Check dHash if available in the index
            if "dhash" in item and item["dhash"]:
                db_dhash = imagehash.hex_to_hash(item["dhash"])
                dist_d = int(query_dhash - db_dhash)
                # Composite distance: pHash 60%, dHash 40%
                effective_dist = int(round(dist_p * 0.6 + dist_d * 0.4))
            else:
                effective_dist = dist_p

            # STRICT CUTOFF: Skip unrelated images completely!
            if effective_dist > max_distance:
                continue

            similarity = max(0.0, (64 - effective_dist) / 64.0)
            similarity_pct = round(similarity * 100, 1)
            rel = item.get("rel_path")
            
            # Confidence categorization
            if effective_dist == 0:
                tag = "EXACT MATCH"
                tag_color = "emerald"
            elif effective_dist <= 4:
                tag = "EXTREMELY HIGH"
                tag_color = "emerald"
            elif effective_dist <= 8:
                tag = "HIGH CONFIDENCE"
                tag_color = "cyan"
            else:
                tag = "POSSIBLE VARIANT"
                tag_color = "amber"

            results.append({
                "rel_path": rel,
                "filename": os.path.basename(rel),
                "phash": item.get("phash", ""),
                "url": url_for("static", filename=rel, _external=False),
                "distance": effective_dist,
                "similarity": round(similarity, 4),
                "similarity_pct": similarity_pct,
                "tag": tag,
                "tag_color": tag_color,
            })
        except Exception as e:
            print("Search compare error:", e)
            continue

    results.sort(key=lambda x: (x["distance"], -x["similarity"]))
    return results[:top_k], str(query_phash)


import urllib.parse

# --- High-Reliability Public Image Hosting (FreeImage.host -> Catbox.moe) ---
def upload_to_public_cdn(filepath):
    """Uploads query probe to fast public CDN nodes for reverse search engines.
    Returns direct public URL or None.
    """
    # 1. Primary: FreeImage.host (Fast global CDN iili.io)
    try:
        with open(filepath, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")
        r = requests.post(
            "https://freeimage.host/api/1/upload",
            data={
                "key": "6d207e02198a847aa98d0a2a901485a5",
                "action": "upload",
                "source": b64_img,
                "format": "json"
            },
            timeout=8
        )
        if r.status_code == 200:
            url = r.json().get("image", {}).get("url")
            if url and url.startswith("http"):
                return url
    except Exception as e:
        print("FreeImage CDN node error:", e)

    # 2. Fallback: Catbox.moe
    try:
        with open(filepath, "rb") as f:
            r = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": f},
                timeout=8
            )
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception as e:
        print("Catbox CDN node error:", e)

    return None


@app.route("/", methods=["GET", "POST"])
def upload_file():
    public_url = None
    search_urls = {}
    local_results = []
    query_preview = None
    query_phash = None
    dataset_count = _dataset_image_count()
    search_type = request.form.get("search_type") if request.method == "POST" else "local"
    
    # Matching sensitivity threshold:
    # 8 = Strict (Exact / Near identical), 12 = Balanced (Default), 16 = Permissive
    try:
        threshold = int(request.form.get("threshold", 12))
    except (ValueError, TypeError):
        threshold = 12

    if request.method == "POST":
        file = request.files.get("file")
        image_url_input = request.form.get("image_url").strip() if request.form.get("image_url") else None

        if (not file or file.filename == "") and not image_url_input:
            flash("Please upload an image file or enter an image URL to investigate!", "warning")
            return render_template(
                "upload.html",
                search_type=search_type,
                dataset_count=dataset_count,
                threshold=threshold
            )

        # Branch: local dataset forensics
        if search_type == "local":
            if dataset_count == 0:
                flash("⚠️ Your local dataset is empty. Add images to 'web/static/dataset/' and click Reindex.", "warning")
                return render_template(
                    "upload.html",
                    search_type=search_type,
                    dataset_count=dataset_count,
                    threshold=threshold
                )
            pil_image = None
            temp_path = None
            try:
                if file and file.filename != "":
                    if not allowed_file(file.filename):
                        flash("❌ Only image files (PNG, JPG, JPEG, WEBP, GIF, BMP) are allowed!", "error")
                        return render_template(
                            "upload.html",
                            search_type=search_type,
                            dataset_count=dataset_count,
                            threshold=threshold
                        )
                    filename = secure_filename(file.filename)
                    suffix = os.path.splitext(filename)[1]
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        temp_path = tmp.name
                        file.save(temp_path)
                    pil_image = Image.open(temp_path)
                else:
                    headers = {"User-Agent": "Mozilla/5.0"}
                    resp = requests.get(image_url_input, headers=headers, timeout=15)
                    resp.raise_for_status()
                    pil_image = Image.open(BytesIO(resp.content))

                # Generate base64 thumbnail for inquiry dossier
                thumb_buffer = BytesIO()
                preview_im = pil_image.copy()
                preview_im.thumbnail((450, 450))
                preview_im.convert("RGB").save(thumb_buffer, format="JPEG", quality=85)
                query_preview = "data:image/jpeg;base64," + base64.b64encode(thumb_buffer.getvalue()).decode()

                local_results, query_phash = search_local_dataset(pil_image, max_distance=threshold)
                if not local_results:
                    min_sim = round((64 - threshold) / 64 * 100, 1)
                    flash(f"🔍 Scan complete: 0 matches found in local dataset. (Threshold: Δ ≤ {threshold} bits / ≥ {min_sim}% similarity required).", "info")
                else:
                    exact_count = sum(1 for r in local_results if r["distance"] == 0)
                    if exact_count > 0:
                        flash(f"Forensic scan complete: {exact_count} EXACT MATCH verified! ({len(local_results)} candidate(s) above threshold).", "success")
                    else:
                        flash(f"Forensic scan complete: {len(local_results)} match candidate(s) verified within confidence threshold.", "success")
            except Exception as e:
                print("Local search error:", e)
                flash(f"❌ Forensic analysis failed: {str(e)}", "error")
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

        else:
            # Branch: web reverse image search
            if file and file.filename != "":
                if not allowed_file(file.filename):
                    flash("❌ Only image files are allowed for OSINT search!", "error")
                    return render_template(
                        "upload.html",
                        search_type=search_type,
                        dataset_count=dataset_count,
                        threshold=threshold
                    )
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                file.save(filepath)

                # Generate thumbnail preview
                try:
                    with Image.open(filepath) as im:
                        thumb_buffer = BytesIO()
                        preview_im = im.copy()
                        preview_im.thumbnail((450, 450))
                        preview_im.convert("RGB").save(thumb_buffer, format="JPEG", quality=85)
                        query_preview = "data:image/jpeg;base64," + base64.b64encode(thumb_buffer.getvalue()).decode()
                except Exception:
                    pass

                public_url = upload_to_public_cdn(filepath)
                try:
                    os.remove(filepath)  # cleanup
                except Exception:
                    pass
            else:
                public_url = image_url_input
                query_preview = image_url_input

            if public_url:
                encoded_url = urllib.parse.quote_plus(public_url)
                search_urls = {
                    "Google Lens": f"https://lens.google.com/uploadbyurl?url={encoded_url}",
                    "Bing Visual Search": f"https://www.bing.com/images/searchbyimage?cbir=sbi&imgurl={encoded_url}",
                    "Yandex Images": f"https://yandex.com/images/search?rpt=imageview&url={encoded_url}",
                    "TinEye Reverse Search": f"https://tineye.com/search?url={encoded_url}"
                }

                # Attempt to open primary Google Lens search
                try:
                    webbrowser.open_new_tab(search_urls["Google Lens"])
                except Exception:
                    pass

                flash("Target uploaded to public CDN! OSINT intelligence dossiers generated.", "success")
            else:
                flash("❌ Public upload failed on hosting nodes. Please check connection or paste an image URL directly.", "error")

    return render_template(
        "upload.html",
        public_url=public_url,
        search_urls=search_urls,
        local_results=local_results,
        search_type=search_type,
        query_preview=query_preview,
        query_phash=query_phash,
        dataset_count=dataset_count,
        threshold=threshold
    )


@app.route("/reindex", methods=["GET", "POST"])
def reindex():
    count = build_image_index()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.args.get("format") == "json":
        return jsonify({"success": True, "count": count, "message": f"Index rebuilt with {count} images."})
    flash(f"✅ Local database reindexed successfully! {count} target files indexed.", "success")
    return redirect(url_for("upload_file"))


if __name__ == "__main__":
    app.run(debug=True)
