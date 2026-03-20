import os
import json
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from flask import Flask, render_template, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

ODDS_API_KEY = os.getenv("ODDS_API_KEY")

SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
]

REGIONS = "eu"
MARKETS = "h2h"
ODDS_FORMAT = "decimal"

STATE_FILE = "state.json"

MIN_DROP_PERCENT = 8.0
MIN_BOOKMAKERS = 2
MIN_MINUTES_TO_MATCH = 30
DEFAULT_MIN_SCORE = 70


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def make_key(event_id: str, bookmaker: str, outcome: str) -> str:
    return f"{event_id}|{bookmaker}|{outcome}"


def calc_score(drop_pct: float, bookmakers_count: int, minutes_to_match: float) -> int:
    score = 0
    score += min(drop_pct * 3, 45)
    score += min(bookmakers_count * 12, 36)

    if minutes_to_match >= 180:
        score += 15
    elif minutes_to_match >= 90:
        score += 10
    elif minutes_to_match >= 45:
        score += 5

    return min(int(score), 100)


def get_recommendation(outcome_name: str) -> str:
    if outcome_name.lower() == "draw":
        return "Краще X / X2, а не чисту нічию"
    return f"{outcome_name} або safer: фора (0)"


async def fetch_odds_for_sport(client: httpx.AsyncClient, sport: str):
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
    }
    response = await client.get(url, params=params, timeout=30.0)
    response.raise_for_status()
    return response.json()


async def scan_market(min_score: int):
    prev_state = load_state()
    new_state = {}
    grouped_moves = defaultdict(list)

    now = datetime.now(timezone.utc)
    all_events = []

    async with httpx.AsyncClient() as client:
        for sport in SPORTS:
            try:
                events = await fetch_odds_for_sport(client, sport)
                all_events.extend(events)
            except Exception:
                continue

    for event in all_events:
        event_id = event.get("id")
        home_team = event.get("home_team")
        teams = event.get("teams", [])
        away_team = next((t for t in teams if t != home_team), "Unknown")
        match_name = f"{home_team} vs {away_team}"

        commence_time = event.get("commence_time")
        if not commence_time:
            continue

        start_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        minutes_to_match = (start_dt - now).total_seconds() / 60

        if minutes_to_match < MIN_MINUTES_TO_MATCH:
            continue

        for bookmaker in event.get("bookmakers", []):
            bookmaker_key = bookmaker.get("key", "unknown")

            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue

                for outcome in market.get("outcomes", []):
                    outcome_name = outcome.get("name")
                    current_price = outcome.get("price")

                    if not outcome_name or current_price is None:
                        continue

                    state_key = make_key(event_id, bookmaker_key, outcome_name)
                    new_state[state_key] = {
                        "match_name": match_name,
                        "bookmaker": bookmaker_key,
                        "outcome": outcome_name,
                        "price": current_price,
                        "minutes_to_match": round(minutes_to_match, 1),
                    }

                    old_entry = prev_state.get(state_key)
                    if not old_entry:
                        continue

                    old_price = old_entry.get("price")
                    if not old_price or old_price <= 0:
                        continue

                    if current_price < old_price:
                        drop_pct = ((old_price - current_price) / old_price) * 100
                        if drop_pct >= MIN_DROP_PERCENT:
                            group_key = f"{event_id}|{outcome_name}"
                            grouped_moves[group_key].append({
                                "match_name": match_name,
                                "outcome": outcome_name,
                                "bookmaker": bookmaker_key,
                                "old_price": old_price,
                                "new_price": current_price,
                                "drop_pct": round(drop_pct, 2),
                                "minutes_to_match": round(minutes_to_match, 1),
                            })

    signals = []

    for _, moves in grouped_moves.items():
        if len(moves) < MIN_BOOKMAKERS:
            continue

        moves = sorted(moves, key=lambda x: x["drop_pct"], reverse=True)
        best = moves[0]
        score = calc_score(best["drop_pct"], len(moves), best["minutes_to_match"])

        if score < min_score:
            continue

        signals.append({
            "match_name": best["match_name"],
            "outcome": best["outcome"],
            "old_price": best["old_price"],
            "new_price": best["new_price"],
            "drop_pct": best["drop_pct"],
            "bookmakers_count": len(moves),
            "bookmakers": ", ".join(m["bookmaker"] for m in moves[:6]),
            "score": score,
            "minutes_to_match": best["minutes_to_match"],
            "recommendation": get_recommendation(best["outcome"]),
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    save_state(new_state)
    return signals


@app.route("/")
async def index():
    score_filter = request.args.get("score", str(DEFAULT_MIN_SCORE))
    try:
        min_score = max(0, min(int(score_filter), 100))
    except ValueError:
        min_score = DEFAULT_MIN_SCORE

    signals = await scan_market(min_score=min_score)
    top_signals = [s for s in signals if s["score"] >= 85][:3]

    return render_template(
        "index.html",
        signals=signals,
        top_signals=top_signals,
        current_score=min_score,
        updated_at=datetime.now().strftime("%H:%M:%S"),
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
