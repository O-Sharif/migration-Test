"""
WordPress Migration Checker
Compares old live site vs Kinsta staging for:
 - Pages & URLs existence
 - Content/text similarity
 - Images & media files
"""

import requests
import time
import re
import json
import csv
import hashlib
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from collections import defaultdict
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─────────────────────────────────────────────
# CONFIGURATION — Edit these before running
# ─────────────────────────────────────────────
CONFIG = {
    "live_url":    "https://www.rivervieworthodontics.com/",     # ← Old live site
    "staging_url": "https://rivervieworthodontics.kinsta.cloud/",     # ← Kinsta staging URL

    # Crawl settings
    "max_pages":          200,     # Max pages to crawl from live site
    "crawl_delay":        0.5,     # Seconds between requests (be polite)
    "request_timeout":    15,      # Seconds before timeout

    # Comparison thresholds
    "content_similarity_threshold": 1.0,   # 0.0–1.0 (0.75 = 75% similar is OK)
    "image_check":        True,             # Check images on each page

    # Output
    "output_dir":         ".",
    "report_filename":    "migration_report.html",
    "json_filename":      "migration_results.json",
    "csv_filename":       "migration_results.csv",

    # Auth (if staging is password protected)
    "staging_auth":       None,    # e.g. ("username", "password") or None

    # Threads
    "max_workers":        5,
}
# ─────────────────────────────────────────────


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WP-Migration-Checker/1.0)"
}

session = requests.Session()
session.headers.update(HEADERS)


# ══════════════════════════════════════════════
# STEP 1: CRAWLER — Discover all URLs on live site
# ══════════════════════════════════════════════

def crawl_site(base_url: str) -> list[str]:
    """Crawl live site and return all internal page URLs."""
    visited = set()
    to_visit = [base_url]
    found_urls = []
    domain = urlparse(base_url).netloc

    print(f"\n[CRAWL] Starting crawl of: {base_url}")
    print(f"        Max pages: {CONFIG['max_pages']}")

    while to_visit and len(found_urls) < CONFIG["max_pages"]:
        url = to_visit.pop(0)
        norm_url = url.rstrip("/")
        if norm_url in visited:
            continue
        visited.add(norm_url)

        try:
            resp = session.get(url, timeout=CONFIG["request_timeout"], verify=False)
            if resp.status_code != 200:
                continue
            ct = resp.headers.get("Content-Type", "")
            if "text/html" not in ct:
                continue

            found_urls.append(url)
            print(f"  [{len(found_urls):>3}] {url}")

            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup.find_all("a", href=True):
                href = tag["href"].strip()
                abs_url = urljoin(base_url, href)
                parsed = urlparse(abs_url)

                # Only follow same-domain, non-fragment, non-query HTML links
                if (parsed.netloc == domain
                        and parsed.scheme in ("http", "https")
                        and not parsed.fragment
                        and abs_url.rstrip("/") not in visited
                        and not any(abs_url.lower().endswith(ext) for ext in
                                    [".pdf", ".zip", ".jpg", ".png", ".gif",
                                     ".mp4", ".mp3", ".xml", ".txt"])):
                    to_visit.append(abs_url)

            time.sleep(CONFIG["crawl_delay"])

        except Exception as e:
            print(f"  [WARN] Skipping {url}: {e}")

    print(f"\n[CRAWL] Done. Found {len(found_urls)} pages.\n")
    return found_urls


# ══════════════════════════════════════════════
# STEP 2: URL CHECK — Does staging have the same pages?
# ══════════════════════════════════════════════

def live_to_staging_url(live_url: str) -> str:
    """Replace live domain with staging domain."""
    live_base = CONFIG["live_url"].rstrip("/")
    staging_base = CONFIG["staging_url"].rstrip("/")
    return live_url.replace(live_base, staging_base, 1)


def get_text_diff(live_text: str, staging_text: str) -> dict:
    """Find exact words/phrases only in live and only in staging."""
    live_words    = set(re.findall(r'\b\w{4,}\b', live_text))
    staging_words = set(re.findall(r'\b\w{4,}\b', staging_text))

    only_in_live    = sorted(live_words - staging_words)
    only_in_staging = sorted(staging_words - live_words)

    # Also grab changed sentences (lines that differ)
    live_sentences    = [s.strip() for s in re.split(r'[.!?]', live_text)    if len(s.strip()) > 30]
    staging_sentences = [s.strip() for s in re.split(r'[.!?]', staging_text) if len(s.strip()) > 30]

    changed_live    = [s for s in live_sentences    if s not in staging_sentences][:5]
    changed_staging = [s for s in staging_sentences if s not in live_sentences][:5]

    return {
        "words_only_in_live":    only_in_live[:30],
        "words_only_in_staging": only_in_staging[:30],
        "sentences_only_in_live":    changed_live,
        "sentences_only_in_staging": changed_staging,
    }


def check_url(live_url: str) -> dict:
    """Check a single URL on both live and staging."""
    staging_url = live_to_staging_url(live_url)
    result = {
        "live_url":           live_url,
        "staging_url":        staging_url,
        "live_status":        None,
        "staging_status":     None,
        "url_ok":             False,
        "content_similarity": 0.0,
        "content_ok":         False,
        # Exact text diff
        "text_diff": {
            "words_only_in_live":        [],
            "words_only_in_staging":     [],
            "sentences_only_in_live":    [],
            "sentences_only_in_staging": [],
        },
        # Images
        "missing_images":  [],
        "broken_images":   [],
        "images_ok":       True,
        # All live images vs staging images (for exact comparison)
        "live_images":     [],
        "staging_images":  [],
        "error":           None,
    }

    auth = CONFIG.get("staging_auth")

    try:
        # Fetch live page
        live_resp = session.get(live_url, timeout=CONFIG["request_timeout"], verify=False)
        result["live_status"] = live_resp.status_code

        # Fetch staging page
        staging_resp = session.get(
            staging_url, timeout=CONFIG["request_timeout"],
            verify=False, auth=auth
        )
        result["staging_status"] = staging_resp.status_code
        result["url_ok"] = (staging_resp.status_code == 200)

        if result["url_ok"] and "text/html" in staging_resp.headers.get("Content-Type", ""):
            # Content comparison
            live_text    = extract_text(live_resp.text)
            staging_text = extract_text(staging_resp.text)
            sim = SequenceMatcher(None, live_text, staging_text).ratio()
            result["content_similarity"] = round(sim, 4)
            result["content_ok"] = sim >= CONFIG["content_similarity_threshold"]

            # Exact text diff — what's different?
            if not result["content_ok"]:
                result["text_diff"] = get_text_diff(live_text, staging_text)

            # Image check
            if CONFIG["image_check"]:
                live_imgs   = extract_image_srcs(live_resp.text,    CONFIG["live_url"])
                staging_imgs = extract_image_srcs(staging_resp.text, CONFIG["staging_url"])
                result["live_images"]    = sorted({urlparse(i).path for i in live_imgs})
                result["staging_images"] = sorted({urlparse(i).path for i in staging_imgs})

                result["missing_images"], result["broken_images"] = check_images(
                    live_resp.text, staging_resp.text, staging_url
                )
                result["images_ok"] = (
                    len(result["missing_images"]) == 0 and
                    len(result["broken_images"]) == 0
                )

    except Exception as e:
        result["error"] = str(e)

    return result


def extract_text(html: str) -> str:
    """Extract visible text from HTML, strip nav/footer noise."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "meta", "link"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip().lower()


def check_images(live_html: str, staging_html: str, staging_base: str) -> tuple[list, list]:
    """Compare image src lists between live and staging pages."""
    live_imgs    = extract_image_srcs(live_html, CONFIG["live_url"])
    staging_imgs = extract_image_srcs(staging_html, CONFIG["staging_url"])

    # Normalize: strip domain, keep path only
    live_paths    = {urlparse(img).path for img in live_imgs}
    staging_paths = {urlparse(img).path for img in staging_imgs}
    missing_paths = live_paths - staging_paths

    # Check if missing images are actually broken on staging
    missing_images = []
    broken_images  = []
    auth = CONFIG.get("staging_auth")

    for path in missing_paths:
        staging_img_url = CONFIG["staging_url"].rstrip("/") + path
        try:
            r = session.head(staging_img_url, timeout=8, verify=False, auth=auth)
            if r.status_code == 200:
                pass  # exists but different path reference
            else:
                missing_images.append(path)
        except Exception:
            broken_images.append(path)

    return missing_images, broken_images


def extract_image_srcs(html: str, base_url: str) -> list[str]:
    """Extract all img src attributes from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    srcs = []
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if src.startswith("data:"):
            continue
        srcs.append(urljoin(base_url, src))
    return srcs


# ══════════════════════════════════════════════
# STEP 3: RUN ALL CHECKS IN PARALLEL
# ══════════════════════════════════════════════

def run_checks(urls: list[str]) -> list[dict]:
    results = []
    total = len(urls)
    print(f"[CHECK] Checking {total} URLs (threads: {CONFIG['max_workers']})...\n")

    with ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as executor:
        futures = {executor.submit(check_url, url): url for url in urls}
        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()
            results.append(result)

            status_icon = "✓" if (result["url_ok"] and result["content_ok"] and result["images_ok"]) else "✗"
            print(f"  [{done:>3}/{total}] {status_icon} {result['live_url']}")

            if not result["url_ok"]:
                print(f"         → ❌ URL MISSING on staging  (HTTP {result['staging_status']})")
                print(f"            Live:    {result['live_url']}")
                print(f"            Staging: {result['staging_url']}")

            elif not result["content_ok"]:
                pct = result['content_similarity'] * 100
                print(f"         → ⚠  TEXT MISMATCH  ({pct:.1f}% similar)")
                diff = result["text_diff"]
                if diff["words_only_in_live"]:
                    print(f"            Words in LIVE but NOT staging : {', '.join(diff['words_only_in_live'][:15])}")
                if diff["words_only_in_staging"]:
                    print(f"            Words in STAGING but NOT live : {', '.join(diff['words_only_in_staging'][:15])}")
                if diff["sentences_only_in_live"]:
                    print(f"            Sentences only in LIVE:")
                    for s in diff["sentences_only_in_live"][:2]:
                        print(f"              → \"{s[:120]}\"")
                if diff["sentences_only_in_staging"]:
                    print(f"            Sentences only in STAGING:")
                    for s in diff["sentences_only_in_staging"][:2]:
                        print(f"              → \"{s[:120]}\"")

            if result["missing_images"]:
                print(f"         → 🖼  MISSING IMAGES ({len(result['missing_images'])}):")
                for img in result["missing_images"]:
                    print(f"              Live had   : {CONFIG['live_url'].rstrip('/')}{img}")
                    print(f"              Not found  : {CONFIG['staging_url'].rstrip('/')}{img}")
            if result["broken_images"]:
                print(f"         → ❌ BROKEN IMAGES ({len(result['broken_images'])}):")
                for img in result["broken_images"]:
                    print(f"              Broken URL : {CONFIG['staging_url'].rstrip('/')}{img}")

    return results


# ══════════════════════════════════════════════
# STEP 4: GENERATE REPORTS
# ══════════════════════════════════════════════

def generate_summary(results: list[dict]) -> dict:
    total   = len(results)
    url_ok  = sum(1 for r in results if r["url_ok"])
    cnt_ok  = sum(1 for r in results if r["content_ok"])
    img_ok  = sum(1 for r in results if r["images_ok"])
    errors  = sum(1 for r in results if r["error"])
    all_ok  = sum(1 for r in results if r["url_ok"] and r["content_ok"] and r["images_ok"])

    return {
        "total_pages":         total,
        "urls_ok":             url_ok,
        "urls_missing":        total - url_ok,
        "content_ok":          cnt_ok,
        "content_issues":      total - cnt_ok,
        "images_ok":           img_ok,
        "image_issues":        total - img_ok,
        "errors":              errors,
        "fully_ok":            all_ok,
        "success_rate":        round(all_ok / total * 100, 1) if total else 0,
        "generated_at":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_json(results, summary):
    path = CONFIG["json_filename"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, indent=2, ensure_ascii=False)
    print(f"[SAVE] JSON → {path}")


def save_csv(results):
    path = CONFIG["csv_filename"]
    fields = ["live_url", "staging_url", "live_status", "staging_status",
              "url_ok", "content_similarity", "content_ok",
              "images_ok", "missing_images", "broken_images", "error"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = {k: r.get(k, "") for k in fields}
            row["missing_images"] = "; ".join(r.get("missing_images", []))
            row["broken_images"]  = "; ".join(r.get("broken_images", []))
            w.writerow(row)
    print(f"[SAVE] CSV  → {path}")


def save_html_report(results, summary):
    path = CONFIG["report_filename"]
    s = summary
    sr_color = "#22c55e" if s["success_rate"] >= 90 else ("#f59e0b" if s["success_rate"] >= 70 else "#ef4444")

    rows = ""
    for r in results:
        ok = r["url_ok"] and r["content_ok"] and r["images_ok"]
        row_class = "ok" if ok else "fail"
        sim_pct   = f"{r['content_similarity']*100:.0f}%"

        # ── Build detail panel ──────────────────────────────
        detail_html = ""

        # URL mismatch
        if not r["url_ok"]:
            detail_html += f"""
            <div class="detail-block">
              <div class="detail-title">❌ URL Missing on Staging</div>
              <div class="diff-row">
                <div class="diff-col live"><div class="col-label">LIVE ({r['live_status']})</div>
                  <div class="diff-item ok-item">{r['live_url']}</div></div>
                <div class="diff-col staging"><div class="col-label">STAGING ({r['staging_status'] or 'ERROR'})</div>
                  <div class="diff-item bad-item">{r['staging_url']}</div></div>
              </div>
            </div>"""

        # Text mismatch
        if not r["content_ok"] and r["url_ok"]:
            diff = r.get("text_diff", {})
            wl = diff.get("words_only_in_live", [])
            ws = diff.get("words_only_in_staging", [])
            sl = diff.get("sentences_only_in_live", [])
            ss = diff.get("sentences_only_in_staging", [])

            words_html = ""
            if wl or ws:
                wl_tags = " ".join(f'<span class="word-tag live-tag">{w}</span>' for w in wl[:20])
                ws_tags = " ".join(f'<span class="word-tag staging-tag">{w}</span>' for w in ws[:20])
                words_html = f"""
                <div class="diff-row">
                  <div class="diff-col live">
                    <div class="col-label">Words only in LIVE</div>
                    <div>{wl_tags if wl_tags else '<span class="none-tag">none</span>'}</div>
                  </div>
                  <div class="diff-col staging">
                    <div class="col-label">Words only in STAGING</div>
                    <div>{ws_tags if ws_tags else '<span class="none-tag">none</span>'}</div>
                  </div>
                </div>"""

            sent_html = ""
            if sl or ss:
                sl_items = "".join(f'<div class="diff-item live-item">"{s[:200]}"</div>' for s in sl)
                ss_items = "".join(f'<div class="diff-item staging-item">"{s[:200]}"</div>' for s in ss)
                sent_html = f"""
                <div class="diff-row">
                  <div class="diff-col live">
                    <div class="col-label">Sentences only in LIVE</div>
                    {sl_items if sl_items else '<div class="none-tag">none</div>'}
                  </div>
                  <div class="diff-col staging">
                    <div class="col-label">Sentences only in STAGING</div>
                    {ss_items if ss_items else '<div class="none-tag">none</div>'}
                  </div>
                </div>"""

            detail_html += f"""
            <div class="detail-block">
              <div class="detail-title">⚠ Text Mismatch — {sim_pct} similar</div>
              {words_html}{sent_html}
            </div>"""

        # Image mismatch
        if not r["images_ok"]:
            img_rows = ""
            for img in r.get("missing_images", []):
                live_full    = CONFIG["live_url"].rstrip("/") + img
                staging_full = CONFIG["staging_url"].rstrip("/") + img
                img_rows += f"""
                <div class="diff-row">
                  <div class="diff-col live">
                    <div class="col-label">LIVE (exists ✓)</div>
                    <div class="diff-item ok-item"><a href="{live_full}" target="_blank">{live_full}</a></div>
                  </div>
                  <div class="diff-col staging">
                    <div class="col-label">STAGING (missing ✗)</div>
                    <div class="diff-item bad-item"><a href="{staging_full}" target="_blank">{staging_full}</a></div>
                  </div>
                </div>"""
            for img in r.get("broken_images", []):
                staging_full = CONFIG["staging_url"].rstrip("/") + img
                img_rows += f"""
                <div class="diff-row">
                  <div class="diff-col live"><div class="col-label">Image path</div>
                    <div class="diff-item ok-item">{img}</div></div>
                  <div class="diff-col staging"><div class="col-label">STAGING (broken ✗)</div>
                    <div class="diff-item bad-item"><a href="{staging_full}" target="_blank">{staging_full}</a></div>
                  </div>
                </div>"""

            detail_html += f"""
            <div class="detail-block">
              <div class="detail-title">🖼 Image Mismatches</div>
              {img_rows}
            </div>"""

        # Build summary badges for the row
        url_badge  = '<span class="badge b-ok">200 OK</span>'  if r["url_ok"]    else f'<span class="badge b-fail">HTTP {r["staging_status"] or "ERR"}</span>'
        text_badge = f'<span class="badge b-ok">{sim_pct}</span>' if r["content_ok"] else f'<span class="badge b-warn">{sim_pct}</span>'
        img_total  = len(r.get("missing_images",[])) + len(r.get("broken_images",[]))
        img_badge  = '<span class="badge b-ok">✓ OK</span>' if r["images_ok"] else f'<span class="badge b-warn">⚠ {img_total} issue(s)</span>'

        uid = abs(hash(r["live_url"])) % 999999
        toggle = f'onclick="toggle({uid})"' if detail_html else ""
        arrow  = "▼" if detail_html else ""

        rows += f"""
        <tr class="{row_class}" {toggle} {'style="cursor:pointer"' if detail_html else ""}>
            <td><a href="{r['live_url']}" target="_blank" onclick="event.stopPropagation()">{r['live_url']}</a>
                {"<span class='arrow' id='arr-"+str(uid)+"'> "+arrow+"</span>" if detail_html else ""}</td>
            <td class="center">{url_badge}</td>
            <td class="center">{text_badge}</td>
            <td class="center">{img_badge}</td>
        </tr>
        {"<tr class='detail-row' id='det-"+str(uid)+"'><td colspan='4'><div class='detail-wrap'>"+detail_html+"</div></td></tr>" if detail_html else ""}"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>WP Migration Report — {s['generated_at']}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Sora:wght@400;600;700&display=swap');
  :root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --ok: #22c55e;
    --fail: #ef4444; --warn: #f59e0b; --accent: #58a6ff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Sora', sans-serif; padding: 2rem; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 0.25rem; }}
  .subtitle {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 2rem; font-family: 'JetBrains Mono', monospace; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem; }}
  .card .val {{ font-size: 2rem; font-weight: 700; }}
  .card .lbl {{ color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: .05em; margin-top: .25rem; }}
  .sr {{ font-size: 2.4rem !important; color: {sr_color}; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 10px; overflow: hidden; border: 1px solid var(--border); font-size: 0.82rem; }}
  th {{ background: #21262d; padding: .7rem 1rem; text-align: left; font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }}
  td {{ padding: .65rem 1rem; border-top: 1px solid var(--border); vertical-align: top; }}
  td.center {{ text-align: center; }}
  tr.ok td:first-child  {{ border-left: 3px solid var(--ok); }}
  tr.fail td:first-child {{ border-left: 3px solid var(--fail); }}
  tr.ok:hover, tr.fail:hover {{ background: #1c2128; }}
  td a {{ color: var(--accent); text-decoration: none; word-break: break-all; }}
  td a:hover {{ text-decoration: underline; }}
  .arrow {{ color: var(--muted); font-size: .7rem; margin-left: .3rem; }}
  /* Badges */
  .badge {{ display:inline-block; padding:.2rem .5rem; border-radius:5px; font-size:.72rem; font-weight:600; }}
  .b-ok   {{ background:#14532d; color:#4ade80; }}
  .b-fail {{ background:#450a0a; color:#f87171; }}
  .b-warn {{ background:#422006; color:#fb923c; }}
  /* Detail rows */
  .detail-row {{ display: none; }}
  .detail-row.open {{ display: table-row; }}
  .detail-row td {{ padding: 0; border-top: none; }}
  .detail-wrap {{ background: #0d1117; border-top: 1px solid var(--border); padding: 1rem 1.5rem 1.2rem; }}
  .detail-block {{ margin-bottom: 1.2rem; }}
  .detail-title {{ font-size: .78rem; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-bottom: .6rem; }}
  /* Side by side diff */
  .diff-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; margin-bottom: .5rem; }}
  .diff-col {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: .6rem .8rem; }}
  .col-label {{ font-size: .68rem; font-weight: 700; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); margin-bottom: .4rem; }}
  .diff-item {{ font-family: 'JetBrains Mono', monospace; font-size: .75rem; word-break: break-all; padding: .3rem .5rem; border-radius: 4px; margin-bottom: .25rem; }}
  .ok-item      {{ background: #14532d33; color: #4ade80; border-left: 3px solid #22c55e; }}
  .bad-item     {{ background: #450a0a33; color: #f87171; border-left: 3px solid #ef4444; }}
  .live-item    {{ background: #1e3a5f33; color: #93c5fd; border-left: 3px solid #3b82f6; }}
  .staging-item {{ background: #4a1d9633; color: #c4b5fd; border-left: 3px solid #8b5cf6; }}
  /* Word tags */
  .word-tag {{ display:inline-block; padding:.1rem .4rem; border-radius:4px; font-size:.7rem; font-family:'JetBrains Mono',monospace; margin:.1rem; }}
  .live-tag    {{ background:#1e3a5f; color:#93c5fd; }}
  .staging-tag {{ background:#3b0764; color:#c4b5fd; }}
  .none-tag {{ color: var(--muted); font-size: .75rem; font-style: italic; }}
  /* Filter */
  .filter-bar {{ display: flex; gap: .6rem; margin-bottom: 1rem; flex-wrap: wrap; }}
  .btn {{ padding: .4rem .9rem; border-radius: 6px; border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; font-size: .8rem; transition: .15s; }}
  .btn:hover, .btn.active {{ background: var(--accent); border-color: var(--accent); color: #000; }}
  .section-title {{ font-size: .75rem; font-weight: 600; margin: 1.5rem 0 .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .07em; }}
</style>
</head>
<body>
<h1>🚀 WordPress Migration Report</h1>
<div class="subtitle">
  Live: {CONFIG['live_url']} &nbsp;→&nbsp; Staging: {CONFIG['staging_url']}
  &nbsp;|&nbsp; Generated: {s['generated_at']}
</div>

<div class="cards">
  <div class="card"><div class="val sr">{s['success_rate']}%</div><div class="lbl">Success Rate</div></div>
  <div class="card"><div class="val">{s['total_pages']}</div><div class="lbl">Total Pages</div></div>
  <div class="card"><div class="val" style="color:var({'--ok' if s['urls_missing']==0 else '--fail'})">{s['urls_ok']}</div><div class="lbl">URLs OK</div></div>
  <div class="card"><div class="val" style="color:var({'--ok' if s['urls_missing']==0 else '--fail'})">{s['urls_missing']}</div><div class="lbl">URLs Missing</div></div>
  <div class="card"><div class="val" style="color:var({'--ok' if s['content_issues']==0 else '--warn'})">{s['content_ok']}</div><div class="lbl">Content OK</div></div>
  <div class="card"><div class="val" style="color:var({'--ok' if s['image_issues']==0 else '--warn'})">{s['images_ok']}</div><div class="lbl">Images OK</div></div>
  <div class="card"><div class="val" style="color:var({'--ok' if s['errors']==0 else '--fail'})">{s['errors']}</div><div class="lbl">Errors</div></div>
</div>

<div class="section-title">Page-by-Page Results — click a row to see exact mismatches</div>
<div class="filter-bar">
  <button class="btn active" onclick="filterRows('all')">All ({s['total_pages']})</button>
  <button class="btn" onclick="filterRows('fail')">Issues Only ({s['total_pages'] - s['fully_ok']})</button>
  <button class="btn" onclick="filterRows('ok')">Passing ({s['fully_ok']})</button>
</div>

<table id="results-table">
  <thead>
    <tr>
      <th>Live URL (click to expand)</th>
      <th>URL Status</th>
      <th>Text Match</th>
      <th>Images</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>

<script>
function toggle(uid) {{
  var det = document.getElementById('det-' + uid);
  var arr = document.getElementById('arr-' + uid);
  if (!det) return;
  det.classList.toggle('open');
  if (arr) arr.textContent = det.classList.contains('open') ? ' ▲' : ' ▼';
}}
function filterRows(filter) {{
  document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('#results-table tbody tr.ok, #results-table tbody tr.fail').forEach(tr => {{
    var uid = tr.getAttribute('onclick') ? tr.getAttribute('onclick').match(/\d+/) : null;
    var detRow = uid ? document.getElementById('det-' + uid[0]) : null;
    var show = filter === 'all'
      || (filter === 'ok'   && tr.classList.contains('ok'))
      || (filter === 'fail' && tr.classList.contains('fail'));
    tr.style.display = show ? '' : 'none';
    if (detRow) detRow.style.display = 'none';
    if (detRow && uid) document.getElementById('arr-' + uid[0]) && (document.getElementById('arr-' + uid[0]).textContent = ' ▼');
  }});
}}
</script>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[SAVE] HTML → {path}")


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  WordPress Migration Checker")
    print("=" * 60)
    print(f"  Live site:    {CONFIG['live_url']}")
    print(f"  Staging site: {CONFIG['staging_url']}")
    print("=" * 60)

    # Validate config
    if "your-old-live-site.com" in CONFIG["live_url"]:
        print("\n⚠  Please edit CONFIG in checker.py with your real URLs first!\n")
        return

    start = time.time()

    # 1. Crawl
    urls = crawl_site(CONFIG["live_url"])
    if not urls:
        print("[ERROR] No URLs found. Check your live_url and network.")
        return

    # 2. Check
    results = run_checks(urls)

    # 3. Summarise
    summary = generate_summary(results)

    # 4. Save reports
    print()
    save_json(results, summary)
    save_csv(results)
    save_html_report(results, summary)

    elapsed = round(time.time() - start, 1)
    print(f"\n{'='*60}")
    print(f"  ✅ Done in {elapsed}s")
    print(f"  📊 Success rate: {summary['success_rate']}%  ({summary['fully_ok']}/{summary['total_pages']} pages fully OK)")
    print(f"  ❌ URLs missing: {summary['urls_missing']}")
    print(f"  ⚠  Content issues: {summary['content_issues']}")
    print(f"  🖼  Image issues: {summary['image_issues']}")
    print(f"  📄 Open: {CONFIG['report_filename']}")
    print("=" * 60)


if __name__ == "__main__":
    main()