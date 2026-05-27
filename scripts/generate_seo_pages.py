#!/usr/bin/env python3
"""Generate static SEO pages from content.json.

Runs after aggregate_news.py in the GitHub Action. For each article it writes
a self-contained static HTML page at /artikel/<slug>.html with proper canonical,
Open Graph, Twitter Card, and NewsArticle JSON-LD metadata, plus the article
body pre-rendered so crawlers (including AI engines that don't execute JS) see
content immediately.

Also regenerates:
  - sitemap.xml (homepage + archive + every article)
  - <noscript> fallback blocks in index.html / archiv.html (between sentinels)
  - _redirects so legacy ?slug=X URLs 301 to the static pages

Deterministic — same content.json produces the same output. Idempotent — safe to
re-run on every workflow execution. Stale article files are pruned.
"""
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "content.json"
ARTIKEL_TEMPLATE = ROOT / "artikel.html"
ARTIKEL_DIR = ROOT / "artikel"
SITEMAP = ROOT / "sitemap.xml"
INDEX_HTML = ROOT / "index.html"
ARCHIV_HTML = ROOT / "archiv.html"
REDIRECTS = ROOT / "_redirects"

SITE = "https://workuvision.de"

CATEGORY_LABEL = {
    "tactics": "Taktik",
    "reaction": "Reaktion",
    "transfer": "Transfer",
}

NOSCRIPT_BEGIN = "<!-- AUTO:NOSCRIPT_ARTICLES:BEGIN -->"
NOSCRIPT_END = "<!-- AUTO:NOSCRIPT_ARTICLES:END -->"

GERMAN_MONTHS = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def fmt_date(d: str) -> str:
    try:
        y, m, day = d.split("-")
        return f"{int(day)}. {GERMAN_MONTHS[int(m)]} {y}"
    except Exception:
        return d


def esc(s) -> str:
    return html.escape(s or "", quote=True)


def article_url(slug: str) -> str:
    return f"{SITE}/artikel/{slug}.html"


def render_body(body: str) -> str:
    out = []
    for p in (body or "").split("\n\n"):
        p = p.strip()
        if not p:
            continue
        out.append(f"<p>{esc(p).replace(chr(10), '<br>')}</p>")
    return "\n          ".join(out)


def newsarticle_jsonld(article: dict) -> str:
    slug = article["slug"]
    url = article_url(slug)
    img = f"{SITE}/{article.get('thumbnail', 'img/og-image.jpg')}"
    date_iso = f"{article['date']}T12:00:00+00:00"
    schema = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "headline": article["title"][:110],
        "description": article.get("excerpt", ""),
        "image": [img],
        "datePublished": date_iso,
        "dateModified": date_iso,
        "author": {
            "@type": "Person",
            "name": "Abdel Worku",
            "url": SITE,
            "sameAs": [
                "https://www.tiktok.com/@workuvision",
                "https://www.instagram.com/workuvision",
                "https://www.youtube.com/@workuvision",
            ],
        },
        "publisher": {
            "@type": "Organization",
            "name": "Workuvision",
            "logo": {
                "@type": "ImageObject",
                "url": f"{SITE}/img/icon-192.png",
                "width": 192,
                "height": 192,
            },
        },
        "articleSection": CATEGORY_LABEL.get(article.get("category", ""), "FC Bayern"),
        "inLanguage": "de-DE",
    }
    if article.get("sourceUrl"):
        schema["isBasedOn"] = article["sourceUrl"]
        schema["citation"] = {
            "@type": "CreativeWork",
            "name": article.get("sourceName", article["sourceUrl"]),
            "url": article["sourceUrl"],
        }
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def build_article_page(article: dict, all_articles: list, template: str) -> str:
    slug = article["slug"]
    title = article["title"]
    excerpt = article.get("excerpt") or title
    body_html = render_body(article.get("body", ""))
    thumb = article.get("thumbnail", "")
    thumb_alt = article.get("thumbnailAlt", title)
    thumb_url = f"{SITE}/{thumb}" if thumb else f"{SITE}/img/og-image.jpg"
    badge = article.get("badge", "")
    badge_color = article.get("badgeColor", "bx")
    readtime = article.get("readtime", "")
    category = article.get("category", "")
    canonical = article_url(slug)

    source_box = ""
    if article.get("sourceUrl"):
        source_box = f"""
        <div class="source-box">
          <strong style="color:var(--white);display:block;margin-bottom:.4rem">Quelle</strong>
          <a href="{esc(article['sourceUrl'])}" target="_blank" rel="noopener nofollow">{esc(article.get('sourceName') or article['sourceUrl'])}</a>
        </div>"""

    related = [a for a in all_articles if a["slug"] != slug and a.get("category") == category][:3]
    related_html = ""
    if related:
        cards = "\n            ".join(
            f"""<a class="related-card" href="/artikel/{esc(r['slug'])}.html">
              <div class="d">{esc(fmt_date(r['date']))}</div>
              <div class="t">{esc(r['title'])}</div>
            </a>"""
            for r in related
        )
        related_html = f"""
        <section class="related">
          <h2>Mehr aus {esc(badge or CATEGORY_LABEL.get(category, 'dieser Kategorie'))}</h2>
          <div class="related-grid">
            {cards}
          </div>
        </section>"""

    article_inner = f"""
        <a class="back" href="/archiv.html">← Zurück zum Archiv</a>
        <div class="meta">
          <span class="badge {esc(badge_color)}">{esc(badge)}</span>
          <span>{esc(fmt_date(article['date']))}</span>
          <span>·</span>
          <span>{esc(readtime)}</span>
        </div>
        <h1>{esc(title)}</h1>
        <img class="hero-img" src="/{esc(thumb)}" alt="{esc(thumb_alt)}">
        <div class="body-text">
          {body_html}
        </div>{source_box}{related_html}"""

    seo_head = f"""<meta name="description" content="{esc(excerpt)}">
  <meta name="author" content="Abdel Worku">
  <meta name="robots" content="index, follow, max-image-preview:large">
  <link rel="canonical" href="{esc(canonical)}">
  <meta property="og:type" content="article">
  <meta property="og:url" content="{esc(canonical)}">
  <meta property="og:title" content="{esc(title)}">
  <meta property="og:description" content="{esc(excerpt)}">
  <meta property="og:image" content="{esc(thumb_url)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:locale" content="de_DE">
  <meta property="og:site_name" content="Workuvision">
  <meta property="article:published_time" content="{esc(article['date'])}T12:00:00+00:00">
  <meta property="article:modified_time" content="{esc(article['date'])}T12:00:00+00:00">
  <meta property="article:author" content="Abdel Worku">
  <meta property="article:section" content="{esc(CATEGORY_LABEL.get(category, 'FC Bayern'))}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{esc(title)}">
  <meta name="twitter:description" content="{esc(excerpt)}">
  <meta name="twitter:image" content="{esc(thumb_url)}">
  <title>{esc(title)} — Workuvision</title>
  <script type="application/ld+json">{newsarticle_jsonld(article)}</script>"""

    new_html = template

    # 1. Replace the article-specific head block (from meta description through title)
    new_html = re.sub(
        r'<meta name="description".*?</title>',
        seo_head,
        new_html,
        count=1,
        flags=re.DOTALL,
    )

    # 2. Inject pre-rendered article into the placeholder
    new_html = re.sub(
        r'<div id="article-content">.*?</div>',
        f'<div id="article-content" data-prerendered="true">{article_inner}\n      </div>',
        new_html,
        count=1,
        flags=re.DOTALL,
    )

    # 3. Fix relative asset paths (file is at /artikel/<slug>.html, two levels deep visually)
    new_html = re.sub(
        r'(src=|href=)"(fonts/|img/|favicon\.ico|content\.json|lineup-override\.json)',
        r'\1"/\2',
        new_html,
    )

    # 4. Disable loadArticle() — content is already rendered, don't let JS overwrite it
    new_html = new_html.replace(
        "loadArticle();",
        "/* loadArticle disabled — page is pre-rendered */",
    )

    # 5. Topbar links: artikel.html → keep as legacy, archiv.html and / use absolute paths
    new_html = new_html.replace('href="archiv.html"', 'href="/archiv.html"')
    new_html = new_html.replace('href="index.html"', 'href="/"')

    return new_html


def build_sitemap(articles: list) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    static = [
        (f"{SITE}/", today, "1.0", "daily"),
        (f"{SITE}/archiv.html", today, "0.8", "daily"),
    ]
    article_entries = sorted(
        ((article_url(a["slug"]), a.get("date", today), "0.7", "weekly") for a in articles),
        key=lambda x: x[1],
        reverse=True,
    )
    all_entries = static + list(article_entries)
    rows = "\n".join(
        f"  <url>\n    <loc>{esc(u)}</loc>\n    <lastmod>{esc(d)}</lastmod>\n    <changefreq>{cf}</changefreq>\n    <priority>{p}</priority>\n  </url>"
        for (u, d, p, cf) in all_entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{rows}
</urlset>
"""


def update_noscript_block(html_path: Path, articles: list, max_n: int = 30) -> bool:
    if not html_path.exists():
        return False
    txt = html_path.read_text(encoding="utf-8")
    if NOSCRIPT_BEGIN not in txt or NOSCRIPT_END not in txt:
        return False
    ordered = sorted(articles, key=lambda a: a.get("date", ""), reverse=True)[:max_n]
    items = "\n        ".join(
        f'<li style="margin-bottom:.6rem"><a href="/artikel/{esc(a["slug"])}.html" style="color:inherit"><strong style="color:var(--red)">{esc(a.get("badge",""))}</strong> · {esc(fmt_date(a["date"]))} — {esc(a["title"])}</a></li>'
        for a in ordered
    )
    block = f"""{NOSCRIPT_BEGIN}
  <noscript>
    <section style="padding:5rem 2rem;max-width:880px;margin:0 auto">
      <h2 style="font-family:'Oswald',sans-serif;letter-spacing:2px;text-transform:uppercase;margin-bottom:1.5rem;font-size:1.4rem">Aktuelle Artikel</h2>
      <ul style="list-style:none;padding:0;line-height:1.6">
        {items}
      </ul>
    </section>
  </noscript>
  {NOSCRIPT_END}"""
    pattern = re.compile(re.escape(NOSCRIPT_BEGIN) + r".*?" + re.escape(NOSCRIPT_END), flags=re.DOTALL)
    new_txt = pattern.sub(block, txt, count=1)
    if new_txt != txt:
        html_path.write_text(new_txt, encoding="utf-8")
    return True


def write_redirects() -> None:
    lines = [
        "# Auto-generated by scripts/generate_seo_pages.py — do not edit by hand.",
        "/artikel.html  slug=:slug  /artikel/:slug.html  301",
        "",
    ]
    REDIRECTS.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    data = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
    articles = data.get("articles", [])
    if not articles:
        print("⚠️  No articles in content.json — skipping SEO page generation.")
        return

    template = ARTIKEL_TEMPLATE.read_text(encoding="utf-8")
    ARTIKEL_DIR.mkdir(parents=True, exist_ok=True)

    current_slugs = {a["slug"] for a in articles}
    for f in ARTIKEL_DIR.glob("*.html"):
        if f.stem not in current_slugs:
            f.unlink()
            print(f"  – pruned stale {f.name}")

    written = 0
    for article in articles:
        page = build_article_page(article, articles, template)
        target = ARTIKEL_DIR / f"{article['slug']}.html"
        target.write_text(page, encoding="utf-8")
        written += 1

    SITEMAP.write_text(build_sitemap(articles), encoding="utf-8")
    write_redirects()

    noscript_updated = []
    for path in (INDEX_HTML, ARCHIV_HTML):
        if update_noscript_block(path, articles):
            noscript_updated.append(path.name)

    print(f"✅ {written} article pages → {ARTIKEL_DIR}/")
    print(f"✅ Sitemap with {len(articles) + 2} URLs → {SITEMAP.name}")
    print(f"✅ Redirects → {REDIRECTS.name}")
    if noscript_updated:
        print(f"✅ Noscript block updated in: {', '.join(noscript_updated)}")
    else:
        print("ℹ️  Noscript sentinels not found in index.html/archiv.html — add AUTO:NOSCRIPT_ARTICLES:BEGIN/END markers to enable.")


if __name__ == "__main__":
    main()
