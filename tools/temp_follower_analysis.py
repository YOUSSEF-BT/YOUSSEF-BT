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
from typing import Any

TARGET = "YOUSSEF-BT"
BATCH_SIZE = 10
OUT = Path("output")
OUT.mkdir(exist_ok=True)
TOKEN = os.environ["GH_TOKEN"]
REST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "follower-profile-analysis",
}
GRAPHQL_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "follower-profile-analysis",
}

AI_TERMS = [
    "artificial intelligence", "machine learning", "deep learning", "data science",
    "computer vision", "natural language processing", "nlp", "large language model",
    "llm", "retrieval augmented generation", "rag", "generative ai", "pytorch",
    "tensorflow", "scikit", "yolo", "langchain", "hugging face", "mlops",
    "data engineering", "analytics", "big data", "spark", "airflow", "mlflow",
    "transformer", "agentic ai", "computer-vision", "machine-learning", "data-science",
]
AI_PATTERN = re.compile("|".join(re.escape(term) for term in AI_TERMS), re.I)


def request_json(url: str, *, method: str = "GET", headers: dict[str, str], payload: dict | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(6):
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if error.code in (403, 429, 502, 503, 504) and attempt < 5:
                reset = int(error.headers.get("X-RateLimit-Reset", "0") or 0)
                wait = max(2, min(45, reset - int(time.time()) + 2))
                if error.code >= 500:
                    wait = min(20, 2 ** attempt)
                print(f"Retrying {url} after HTTP {error.code} in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {error.code} for {url}: {body[:1000]}") from error
        except (TimeoutError, urllib.error.URLError) as error:
            if attempt < 5:
                wait = min(20, 2 ** attempt)
                print(f"Retrying {url} after network error in {wait}s: {error}", flush=True)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Unable to retrieve {url}")


def list_followers(username: str) -> list[str]:
    followers: list[str] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        data = request_json(
            f"https://api.github.com/users/{username}/followers?{query}",
            headers=REST_HEADERS,
        )
        followers.extend(item["login"] for item in data if item.get("login"))
        if len(data) < 100:
            return followers
        page += 1


def graphql(query: str) -> dict[str, Any]:
    result = request_json(
        "https://api.github.com/graphql",
        method="POST",
        headers=GRAPHQL_HEADERS,
        payload={"query": query},
    )
    if result.get("errors"):
        messages = "; ".join(error.get("message", "Unknown GraphQL error") for error in result["errors"])
        print(f"GraphQL partial errors: {messages}", flush=True)
    return result.get("data") or {}


def build_batch_query(usernames: list[str]) -> str:
    blocks = []
    for index, username in enumerate(usernames):
        login = json.dumps(username)
        blocks.append(
            f"""
            u{index}: user(login: {login}) {{
              login
              name
              bio
              company
              location
              websiteUrl
              isHireable
              createdAt
              followers {{ totalCount }}
              following {{ totalCount }}
              contributionsCollection {{
                contributionCalendar {{ totalContributions }}
              }}
              repositories(
                first: 100
                ownerAffiliations: OWNER
                privacy: PUBLIC
                orderBy: {{ field: UPDATED_AT, direction: DESC }}
              ) {{
                totalCount
                nodes {{
                  name
                  description
                  url
                  isFork
                  isArchived
                  stargazerCount
                  forkCount
                  pushedAt
                  homepageUrl
                  primaryLanguage {{ name }}
                  licenseInfo {{ spdxId }}
                  repositoryTopics(first: 20) {{
                    nodes {{ topic {{ name }} }}
                  }}
                }}
              }}
            }}
            """
        )
    return "query FollowerBatch {\n" + "\n".join(blocks) + "\n}"


def safe_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def analyze_node(node: dict[str, Any]) -> dict[str, Any]:
    repository_connection = node.get("repositories") or {}
    repositories = [repo for repo in (repository_connection.get("nodes") or []) if repo]
    active_originals = [repo for repo in repositories if not repo.get("isFork") and not repo.get("isArchived")]
    original_repositories = [repo for repo in repositories if not repo.get("isFork")]
    fork_repositories = [repo for repo in repositories if repo.get("isFork")]

    stars = sum(int(repo.get("stargazerCount") or 0) for repo in active_originals)
    forks_received = sum(int(repo.get("forkCount") or 0) for repo in active_originals)
    latest_push = max((repo.get("pushedAt") or "" for repo in active_originals), default="")
    top = sorted(
        active_originals,
        key=lambda repo: (int(repo.get("stargazerCount") or 0), repo.get("pushedAt") or ""),
        reverse=True,
    )

    languages: Counter[str] = Counter()
    ai_repo_count = 0
    described_count = 0
    licensed_count = 0
    deployed_count = 0
    topic_count = 0
    text_parts = [node.get("bio") or "", node.get("company") or ""]

    for repo in active_originals:
        language = ((repo.get("primaryLanguage") or {}).get("name"))
        if language:
            languages[language] += 1
        description = repo.get("description") or ""
        topics = [
            ((topic_node or {}).get("topic") or {}).get("name") or ""
            for topic_node in ((repo.get("repositoryTopics") or {}).get("nodes") or [])
        ]
        text = " ".join([repo.get("name") or "", description, " ".join(topics)])
        text_parts.append(text)
        if AI_PATTERN.search(text):
            ai_repo_count += 1
        if description:
            described_count += 1
        if repo.get("licenseInfo"):
            licensed_count += 1
        if repo.get("homepageUrl"):
            deployed_count += 1
        if topics:
            topic_count += 1

    all_text = " ".join(text_parts)
    ai_keyword_hits = len({match.group(0).lower() for match in AI_PATTERN.finditer(all_text)})
    profile_fields = [
        node.get("name"), node.get("bio"), node.get("company"),
        node.get("location"), node.get("websiteUrl"),
    ]
    completeness = sum(bool(value) for value in profile_fields) / len(profile_fields)

    created = safe_datetime(node.get("createdAt"))
    account_age = ((datetime.now(timezone.utc) - created).days / 365.25) if created else None
    pushed = safe_datetime(latest_push)
    days_since_push = (datetime.now(timezone.utc) - pushed).days if pushed else 9999

    language_total = sum(languages.values()) or 1
    pythonish = sum(languages.get(name, 0) for name in ("Python", "Jupyter Notebook", "R"))
    top_text = " ".join([top[0].get("name") or "", top[0].get("description") or ""]) if top else ""
    ai_fit = min(
        100,
        round(
            (20 if AI_PATTERN.search(node.get("bio") or "") else 0)
            + min(45, ai_repo_count * 5)
            + min(20, 20 * pythonish / language_total)
            + (10 if AI_PATTERN.search(top_text) else 0)
            + min(5, ai_keyword_hits),
            2,
        ),
    )

    active_count = len(active_originals)
    total_owned = int(repository_connection.get("totalCount") or len(repositories))
    return {
        "login": node.get("login") or "",
        "name": node.get("name") or "",
        "bio": node.get("bio") or "",
        "location": node.get("location") or "",
        "company": node.get("company") or "",
        "blog": node.get("websiteUrl") or "",
        "hireable": bool(node.get("isHireable")),
        "followers": int(((node.get("followers") or {}).get("totalCount")) or 0),
        "following": int(((node.get("following") or {}).get("totalCount")) or 0),
        "contributions_last_year": int(
            ((((node.get("contributionsCollection") or {}).get("contributionCalendar") or {}).get("totalContributions"))) or 0
        ),
        "owned_public_repos": total_owned,
        "repos_returned": len(repositories),
        "active_original_repos": active_count,
        "original_repos_returned": len(original_repositories),
        "fork_repos_returned": len(fork_repositories),
        "original_ratio": round(len(original_repositories) / len(repositories), 4) if repositories else 0,
        "stars_received": stars,
        "forks_received": forks_received,
        "top_repo": top[0].get("name", "") if top else "",
        "top_repo_stars": int(top[0].get("stargazerCount") or 0) if top else 0,
        "top_repo_url": top[0].get("url", "") if top else "",
        "top_repo_deployed": bool(top and top[0].get("homepageUrl")),
        "latest_push": latest_push,
        "days_since_latest_push": days_since_push,
        "account_age_years": round(account_age, 2) if account_age is not None else None,
        "profile_completeness": round(completeness, 4),
        "described_repo_ratio": round(described_count / active_count, 4) if active_count else 0,
        "licensed_repo_ratio": round(licensed_count / active_count, 4) if active_count else 0,
        "deployed_repo_ratio": round(deployed_count / active_count, 4) if active_count else 0,
        "topic_repo_ratio": round(topic_count / active_count, 4) if active_count else 0,
        "ai_repo_count": ai_repo_count,
        "ai_keyword_hits": ai_keyword_hits,
        "ai_data_fit_score": ai_fit,
        "main_languages": ", ".join(name for name, _ in languages.most_common(5)),
        "profile_url": f"https://github.com/{node.get('login') or ''}",
    }


def recency_score(days: int) -> int:
    if days <= 30:
        return 10
    if days <= 90:
        return 8
    if days <= 180:
        return 6
    if days <= 365:
        return 4
    if days <= 730:
        return 2
    return 0


def add_ranks(rows: list[dict[str, Any]], key: str, prefix: str) -> None:
    ordered = sorted(
        rows,
        key=lambda item: (item[key], item["stars_received"], item["followers"]),
        reverse=True,
    )
    for rank, item in enumerate(ordered, 1):
        item[f"{prefix}_rank"] = rank
        item[f"{prefix}_percentile"] = round(
            100 * (len(ordered) - rank) / max(1, len(ordered) - 1), 2
        )


followers = list_followers(TARGET)
usernames = [TARGET] + [username for username in followers if username.lower() != TARGET.lower()]
rows: list[dict[str, Any]] = []
failures: list[dict[str, str]] = []

for offset in range(0, len(usernames), BATCH_SIZE):
    batch = usernames[offset : offset + BATCH_SIZE]
    print(f"Fetching batch {offset + 1}-{offset + len(batch)} of {len(usernames)}", flush=True)
    data = graphql(build_batch_query(batch))
    for index, username in enumerate(batch):
        node = data.get(f"u{index}")
        if not node:
            failures.append({"login": username, "error": "GraphQL user result was null"})
            continue
        try:
            rows.append(analyze_node(node))
        except Exception as error:
            failures.append({"login": username, "error": repr(error)})

if not rows:
    raise RuntimeError("No profiles were analyzed")

max_followers = max(max(row["followers"] for row in rows), 1)
max_stars = max(max(row["stars_received"] for row in rows), 1)
max_contributions = max(max(row["contributions_last_year"] for row in rows), 1)

for row in rows:
    social = 15 * math.log1p(row["followers"]) / math.log1p(max_followers)
    stars = 20 * math.log1p(row["stars_received"]) / math.log1p(max_stars)
    repositories = 15 * min(1, row["active_original_repos"] / 20)
    contributions = 15 * math.log1p(row["contributions_last_year"]) / math.log1p(max_contributions)
    profile = 10 * row["profile_completeness"]
    originality = 5 * row["original_ratio"]
    professionalism = 10 * min(
        1,
        0.30 * row["described_repo_ratio"]
        + 0.20 * row["licensed_repo_ratio"]
        + 0.25 * row["deployed_repo_ratio"]
        + 0.25 * row["topic_repo_ratio"],
    )
    strength = round(
        social + stars + repositories + contributions + profile + originality
        + professionalism + recency_score(row["days_since_latest_push"]),
        2,
    )
    row["github_visible_strength_score"] = strength
    row["ai_competitor_score"] = round(0.70 * strength + 0.30 * row["ai_data_fit_score"], 2)

add_ranks(rows, "github_visible_strength_score", "strength")
add_ranks(rows, "ai_competitor_score", "ai")

target = next(row for row in rows if row["login"].lower() == TARGET.lower())
relevant = [row for row in rows if row["ai_data_fit_score"] >= 30]
relevant.sort(
    key=lambda item: (item["ai_competitor_score"], item["stars_received"], item["followers"]),
    reverse=True,
)
target["ai_relevant_rank"] = next(
    (index for index, item in enumerate(relevant, 1) if item["login"].lower() == TARGET.lower()),
    None,
)
target["ai_relevant_population"] = len(relevant)
rows.sort(
    key=lambda item: (item["github_visible_strength_score"], item["stars_received"]),
    reverse=True,
)

fieldnames: list[str] = []
for row in rows:
    for key in row:
        if key not in fieldnames:
            fieldnames.append(key)

with (OUT / "followers_analysis.csv").open("w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

payload = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "methodology": {
        "scope": "Public GitHub profile and public repository metadata only",
        "strength_score": "Heuristic visible-profile score, not a measurement of engineering intelligence or employability",
        "ai_subset_threshold": 30,
    },
    "target": TARGET,
    "followers_found": len(followers),
    "profiles_analyzed": len(rows),
    "failures": failures,
    "target_metrics": target,
    "top_strength": rows[:40],
    "top_ai_data": relevant[:40],
    "profiles": rows,
}
(OUT / "followers_analysis.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
)

lines = [
    f"# GitHub follower analysis for {TARGET}",
    "",
    f"- Followers returned by the public API: **{len(followers)}**",
    f"- Profiles analyzed including the target: **{len(rows)}**",
    f"- Generic visible-strength rank: **{target['strength_rank']}/{len(rows)}**",
    f"- Generic percentile: **{target['strength_percentile']}%**",
    f"- AI/Data rank across all analyzed profiles: **{target['ai_rank']}/{len(rows)}**",
    f"- AI/Data-relevant subset rank: **{target.get('ai_relevant_rank')}/{target.get('ai_relevant_population')}**",
    f"- Visible-strength score: **{target['github_visible_strength_score']}/100**",
    f"- AI/Data fit score: **{target['ai_data_fit_score']}/100**",
    f"- Failures: **{len(failures)}**",
    "",
    "> Scores are heuristic and use only public, machine-readable GitHub metadata. They do not prove who is the better engineer.",
    "",
    "## Top 25 overall",
    "",
    "| Rank | User | Score | Followers | Contributions | Stars | Original repos |",
    "|---:|---|---:|---:|---:|---:|---:|",
]
for item in rows[:25]:
    lines.append(
        f"| {item['strength_rank']} | [{item['login']}]({item['profile_url']}) | "
        f"{item['github_visible_strength_score']} | {item['followers']} | "
        f"{item['contributions_last_year']} | {item['stars_received']} | "
        f"{item['active_original_repos']} |"
    )

lines += [
    "",
    "## Top 25 AI/Data-relevant",
    "",
    "| Rank | User | Combined | AI fit | Strength | Followers | Stars |",
    "|---:|---|---:|---:|---:|---:|---:|",
]
for index, item in enumerate(relevant[:25], 1):
    lines.append(
        f"| {index} | [{item['login']}]({item['profile_url']}) | "
        f"{item['ai_competitor_score']} | {item['ai_data_fit_score']} | "
        f"{item['github_visible_strength_score']} | {item['followers']} | "
        f"{item['stars_received']} |"
    )

(OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
print(json.dumps({
    "followers_found": len(followers),
    "profiles_analyzed": len(rows),
    "failures": len(failures),
    "target": target,
}, ensure_ascii=False, indent=2), flush=True)
