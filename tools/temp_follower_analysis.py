from __future__ import annotations

import csv
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

TARGET = "YOUSSEF-BT"
OUT = Path("output")
OUT.mkdir(exist_ok=True)
TOKEN = os.environ["GH_TOKEN"]
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "follower-profile-analysis",
}


def api(path: str, params: dict | None = None, allow_404: bool = False):
    url = "https://api.github.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(6):
        request = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode()), dict(response.headers)
        except urllib.error.HTTPError as error:
            if allow_404 and error.code == 404:
                return None, dict(error.headers)
            if error.code in (403, 429) and attempt < 5:
                reset = int(error.headers.get("X-RateLimit-Reset", "0") or 0)
                wait = max(3, min(90, reset - int(time.time()) + 2))
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Unable to retrieve {url}")


def paged(path: str, params: dict | None = None):
    query = dict(params or {})
    query["per_page"] = 100
    page = 1
    results = []
    while True:
        query["page"] = page
        data, _ = api(path, query)
        results.extend(data)
        if len(data) < 100:
            return results
        page += 1


AI_TERMS = [
    "artificial intelligence", "machine learning", "deep learning", "data science",
    "computer vision", "natural language processing", "nlp", "large language model",
    "llm", "retrieval augmented generation", "rag", "generative ai", "pytorch",
    "tensorflow", "scikit", "yolo", "langchain", "hugging face", "mlops",
    "data engineering", "analytics", "big data", "spark", "airflow", "mlflow",
]
AI_PATTERN = re.compile("|".join(re.escape(term) for term in AI_TERMS), re.I)
TEST_NAMES = {"test", "tests", "testing", "__tests__", "spec", "specs"}
DEPENDENCY_NAMES = {
    "requirements.txt", "pyproject.toml", "poetry.lock", "package.json",
    "environment.yml", "pom.xml", "build.gradle",
}


def inspect_root(full_name: str) -> dict:
    data, _ = api(f"/repos/{full_name}/contents", allow_404=True)
    if not isinstance(data, list):
        return {}
    names = {str(item.get("name", "")).lower() for item in data}
    return {
        "readme": any(name.startswith("readme") for name in names),
        "license": any(name.startswith(("license", "licence")) for name in names),
        "tests": bool(names & TEST_NAMES),
        "ci": ".github" in names,
        "docker": bool(names & {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}),
        "docs": bool(names & {"docs", "documentation"}),
        "deps": bool(names & DEPENDENCY_NAMES),
    }


def analyze_user(username: str) -> dict:
    profile, _ = api(f"/users/{username}")
    repos = paged(f"/users/{username}/repos", {"type": "owner", "sort": "updated"})
    originals = [repo for repo in repos if not repo.get("fork") and not repo.get("archived")]
    forks = [repo for repo in repos if repo.get("fork")]
    stars = sum(int(repo.get("stargazers_count") or 0) for repo in originals)
    forks_received = sum(int(repo.get("forks_count") or 0) for repo in originals)
    latest = max((repo.get("pushed_at") or "" for repo in originals), default="")
    top = sorted(
        originals,
        key=lambda repo: (int(repo.get("stargazers_count") or 0), repo.get("pushed_at") or ""),
        reverse=True,
    )[:2]

    quality = Counter()
    for repository in top:
        quality.update({key: int(bool(value)) for key, value in inspect_root(repository["full_name"]).items()})

    text_parts = [profile.get("bio") or "", profile.get("company") or ""]
    languages = Counter()
    ai_repo_count = 0
    for repo in originals:
        if repo.get("language"):
            languages[repo["language"]] += 1
        text = " ".join([
            repo.get("name") or "", repo.get("description") or "", " ".join(repo.get("topics") or [])
        ])
        text_parts.append(text)
        if AI_PATTERN.search(text):
            ai_repo_count += 1

    all_text = " ".join(text_parts)
    ai_hits = len({match.group(0).lower() for match in AI_PATTERN.finditer(all_text)})
    profile_fields = [profile.get(key) for key in ("name", "bio", "company", "location", "blog", "email")]
    completeness = sum(bool(value) for value in profile_fields) / len(profile_fields)
    created = datetime.fromisoformat(profile["created_at"].replace("Z", "+00:00"))
    account_age = (datetime.now(timezone.utc) - created).days / 365.25
    if latest:
        pushed = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        days_since_push = (datetime.now(timezone.utc) - pushed).days
    else:
        days_since_push = 9999

    pythonish = sum(languages.get(name, 0) for name in ("Python", "Jupyter Notebook", "R"))
    language_total = sum(languages.values()) or 1
    top_text = " ".join([top[0].get("name") or "", top[0].get("description") or ""]) if top else ""
    ai_fit = min(100, round(
        (20 if AI_PATTERN.search(profile.get("bio") or "") else 0)
        + min(50, ai_repo_count * 5)
        + min(20, 20 * pythonish / language_total)
        + (10 if AI_PATTERN.search(top_text) else 0),
        2,
    ))

    return {
        "login": username,
        "name": profile.get("name") or "",
        "type": profile.get("type") or "",
        "bio": profile.get("bio") or "",
        "location": profile.get("location") or "",
        "company": profile.get("company") or "",
        "blog": profile.get("blog") or "",
        "hireable": bool(profile.get("hireable")),
        "followers": int(profile.get("followers") or 0),
        "following": int(profile.get("following") or 0),
        "public_repos": int(profile.get("public_repos") or 0),
        "owned_repos_fetched": len(repos),
        "original_repos": len(originals),
        "fork_repos": len(forks),
        "original_ratio": round(len(originals) / len(repos), 4) if repos else 0,
        "stars_received": stars,
        "forks_received": forks_received,
        "top_repo": top[0]["name"] if top else "",
        "top_repo_stars": int(top[0].get("stargazers_count") or 0) if top else 0,
        "top_repo_url": top[0].get("html_url") if top else "",
        "top_repo_deployed": bool(top and top[0].get("homepage")),
        "latest_push": latest,
        "days_since_latest_push": days_since_push,
        "account_age_years": round(account_age, 2),
        "profile_completeness": round(completeness, 4),
        "readme_top2": quality["readme"],
        "license_top2": quality["license"],
        "tests_top2": quality["tests"],
        "ci_top2": quality["ci"],
        "docker_top2": quality["docker"],
        "docs_top2": quality["docs"],
        "dependency_files_top2": quality["deps"],
        "ai_repo_count": ai_repo_count,
        "ai_keyword_hits": ai_hits,
        "ai_data_fit_score": ai_fit,
        "main_languages": ", ".join(name for name, _ in languages.most_common(5)),
        "profile_url": profile.get("html_url") or f"https://github.com/{username}",
    }


def recency_score(days: int) -> int:
    if days <= 30: return 15
    if days <= 90: return 12
    if days <= 180: return 9
    if days <= 365: return 6
    if days <= 730: return 3
    return 0


followers = paged(f"/users/{TARGET}/followers")
usernames = [TARGET] + [item["login"] for item in followers if item.get("login") != TARGET]
rows, failures = [], []
for index, username in enumerate(usernames, 1):
    try:
        rows.append(analyze_user(username))
    except Exception as error:
        failures.append({"login": username, "error": repr(error)})
    print(f"{index}/{len(usernames)} {username}", flush=True)

if not rows:
    raise RuntimeError("No profiles were analyzed")

max_followers = max(max(row["followers"] for row in rows), 1)
max_stars = max(max(row["stars_received"] for row in rows), 1)
for row in rows:
    social = 15 * math.log1p(row["followers"]) / math.log1p(max_followers)
    stars = 20 * math.log1p(row["stars_received"]) / math.log1p(max_stars)
    repositories = 15 * min(1, row["original_repos"] / 20)
    quality = (
        4 * min(1, row["readme_top2"] / 2)
        + 3 * min(1, row["tests_top2"])
        + 3 * min(1, row["ci_top2"])
        + 2 * min(1, row["docker_top2"])
        + 2 * min(1, row["license_top2"])
        + 2 * min(1, row["docs_top2"])
        + 2 * min(1, row["dependency_files_top2"])
        + (2 if row["top_repo_deployed"] else 0)
    )
    profile = 10 * row["profile_completeness"]
    originality = 5 * row["original_ratio"]
    strength = round(
        social + stars + repositories + quality + profile + originality
        + recency_score(row["days_since_latest_push"]),
        2,
    )
    row["github_strength_score"] = strength
    row["ai_competitor_score"] = round(0.72 * strength + 0.28 * row["ai_data_fit_score"], 2)


def add_ranks(key: str, prefix: str):
    ordered = sorted(rows, key=lambda item: (item[key], item["stars_received"], item["followers"]), reverse=True)
    for rank, item in enumerate(ordered, 1):
        item[f"{prefix}_rank"] = rank
        item[f"{prefix}_percentile"] = round(100 * (len(ordered) - rank) / max(1, len(ordered) - 1), 2)


add_ranks("github_strength_score", "strength")
add_ranks("ai_competitor_score", "ai")
target = next(row for row in rows if row["login"].lower() == TARGET.lower())
relevant = [row for row in rows if row["ai_data_fit_score"] >= 30]
relevant.sort(key=lambda item: (item["ai_competitor_score"], item["stars_received"]), reverse=True)
target["ai_relevant_rank"] = next(
    (index for index, item in enumerate(relevant, 1) if item["login"].lower() == TARGET.lower()), None
)
target["ai_relevant_population"] = len(relevant)
rows.sort(key=lambda item: (item["github_strength_score"], item["stars_received"]), reverse=True)

with (OUT / "followers_analysis.csv").open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "target": TARGET,
    "followers_found": len(followers),
    "profiles_analyzed": len(rows),
    "failures": failures,
    "target_metrics": target,
    "top_strength": rows[:30],
    "top_ai_data": relevant[:30],
    "profiles": rows,
}
(OUT / "followers_analysis.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

lines = [
    f"# GitHub follower analysis for {TARGET}", "",
    f"- Followers returned by API: **{len(followers)}**",
    f"- Profiles analyzed including the target: **{len(rows)}**",
    f"- Generic strength rank: **{target['strength_rank']}/{len(rows)}**",
    f"- Generic percentile: **{target['strength_percentile']}%**",
    f"- AI/Data rank across all profiles: **{target['ai_rank']}/{len(rows)}**",
    f"- AI/Data-relevant subset rank: **{target.get('ai_relevant_rank')}/{target.get('ai_relevant_population')}**",
    f"- GitHub strength score: **{target['github_strength_score']}/100**",
    f"- AI/Data fit score: **{target['ai_data_fit_score']}/100**", "",
    "## Top 20 overall", "",
    "| Rank | User | Score | Followers | Stars | Original repos |",
    "|---:|---|---:|---:|---:|---:|",
]
for item in rows[:20]:
    lines.append(
        f"| {item['strength_rank']} | [{item['login']}]({item['profile_url']}) | "
        f"{item['github_strength_score']} | {item['followers']} | {item['stars_received']} | {item['original_repos']} |"
    )
lines += ["", "## Top 20 AI/Data-relevant", "", "| Rank | User | Combined | AI fit | Strength |", "|---:|---|---:|---:|---:|"]
for index, item in enumerate(relevant[:20], 1):
    lines.append(
        f"| {index} | [{item['login']}]({item['profile_url']}) | {item['ai_competitor_score']} | "
        f"{item['ai_data_fit_score']} | {item['github_strength_score']} |"
    )
(OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
